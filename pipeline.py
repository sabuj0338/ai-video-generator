"""Script -> storybook video pipeline, tuned for an 8GB Apple Silicon Mac.

Stages:
  1. split_script      script text  -> list of Scene(narration, visual)
  2. synthesize_voice  narration    -> audio file (macOS `say`, zero model RAM)
  3. ImageBackend      visual       -> PNG (local SD-Turbo on MPS, Pollinations API, or offline placeholder)
  4. render_scene      image+audio  -> MP4 clip with Ken Burns motion and optional captions
  5. concat_clips      clips        -> final MP4
"""

from __future__ import annotations

import gc
import json
import os
import re
import shutil
import subprocess
import textwrap
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FPS = 25
SCENE_TAIL_PAD = 0.35  # seconds of silence after each scene's narration

ASPECTS = {
    # label: (output video size, image generation size — multiples of 64, near SD-Turbo's native 512)
    "16:9 Landscape": ((1280, 720), (768, 448)),
    "9:16 Portrait (Shorts/Reels)": ((720, 1280), (448, 768)),
    "1:1 Square": ((1024, 1024), (512, 512)),
}

STYLES = {
    "Storybook illustration": "vibrant educational storybook illustration, clean digital art, soft lighting, highly detailed",
    "Flat vector": "flat vector illustration, bold simple shapes, clean lines, pastel colors, minimal",
    "Watercolor": "soft watercolor painting, gentle textures, children's book art",
    "3D animated film": "3d animated film still, cinematic lighting, expressive characters, highly detailed",
    "3D cartoon": "3d cartoon render, pixar style, cute, colorful, studio lighting",
    "Cinematic photo": "cinematic photograph, dramatic lighting, shallow depth of field, 35mm",
    "None (raw prompt)": "",
}

NEGATIVE_HINT = "text, watermark, letters, blurry, deformed"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


@dataclass
class Scene:
    narration: str
    visual: str


# ---------------------------------------------------------------- 1. script -> scenes

_VISUAL_TAG = re.compile(r"\[(?:visual|image|scene)\s*:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[\"'“A-Z0-9])")
_FILLER = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these",
    "those", "and", "or", "but", "so", "of", "to", "in", "on", "at", "for", "with", "as", "by", "we",
    "you", "they", "our", "your", "their", "can", "will", "just", "very", "really", "also", "then",
    "now", "here", "there", "let's", "lets", "do", "does", "did", "has", "have", "had", "which", "who",
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def auto_visual(narration: str, max_words: int = 28) -> str:
    """Turn narration into a compact image prompt (CLIP only reads ~77 tokens)."""
    words = re.findall(r"[A-Za-z0-9'\-]+", narration)
    keep = [w for w in words if w.lower() not in _FILLER]
    return " ".join(keep[:max_words]) or narration[:120]


_MD_NOISE = re.compile(r"[*_`#>]+")
_LABEL = re.compile(r"^([A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*){0,2})\s*:\s*(.*)$")
_SCENE_HEADER = re.compile(r"^scene\s*\d+\b", re.IGNORECASE)


def _unquote(text: str) -> str:
    """Remove one outer pair of quotes, keeping quotes inside the line balanced."""
    text = text.strip()
    if len(text) > 1 and text[0] in "\"“„" and text[-1] in "\"”":
        text = text[1:-1].strip()
    return text


def is_structured(script: str) -> bool:
    """Screenplay-style script: `Scene N` headers and/or `Narrator:` + `Visual Prompt:` labels."""
    plain = _MD_NOISE.sub("", script)
    has_prompt = re.search(r"^\s*visual(?: prompt)?\s*:", plain, re.IGNORECASE | re.MULTILINE)
    has_speaker = re.search(r"^\s*narrator\s*:", plain, re.IGNORECASE | re.MULTILINE)
    return bool(has_prompt and has_speaker)


def parse_structured(script: str) -> list[Scene]:
    """Parse a screenplay-style script.

    Each `Visual Prompt:` closes the spoken lines before it into one scene, so a scene block with two
    Narrator/Visual Prompt pairs becomes two video scenes. `Visual:` is used when no prompt is given.
    Non-narrator speakers are kept as `Speaker: text` lines so they can get a character voice.
    """
    scenes: list[Scene] = []
    lines: list[str] = []          # spoken lines waiting for their visual prompt
    block_visual = ""              # the block's `Visual:` description (fallback prompt)
    last_prompt = ""

    def flush(prompt: str):
        nonlocal lines, last_prompt
        if lines:
            scenes.append(Scene("\n".join(lines), prompt or block_visual or last_prompt
                                or auto_visual(" ".join(lines))))
            last_prompt = prompt or last_prompt
            lines = []

    for raw in script.replace("\r\n", "\n").split("\n"):
        line = _MD_NOISE.sub("", raw).strip()
        if not line or set(line) <= set("-—=_ "):
            continue
        if _SCENE_HEADER.match(line):
            flush("")
            block_visual = ""
            continue
        m = _LABEL.match(line)
        if not m:
            continue  # titles, stage directions, etc.
        label, text = m.group(1).strip(), m.group(2).strip()
        key = label.lower()
        if key == "visual prompt" or key in ("image", "image prompt", "prompt"):
            flush(text)
        elif key == "visual":
            block_visual = text
        elif text:
            spoken = _unquote(text)
            lines.append(spoken if key == "narrator" else f"{label}: {spoken}")
    flush("")
    return scenes


def parse_segments(narration: str) -> list[tuple[str, str]]:
    """'Child: One… two…' lines -> [(speaker, text)]. Unlabeled lines belong to the narrator."""
    segments = []
    for line in narration.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _LABEL.match(line)
        if m and m.group(1).lower() not in ("visual", "visual prompt"):
            speaker, text = m.group(1), m.group(2)
        else:
            speaker, text = "Narrator", line
        text = _unquote(text)
        if text:
            segments.append((speaker, text))
    return segments


def caption_text(narration: str) -> str:
    return " ".join(t if s == "Narrator" else f"“{t}”" for s, t in parse_segments(narration))


def split_script(script: str, sentences_per_scene: int = 2) -> list[Scene]:
    """Split a script into scenes.

    - Screenplay-style scripts (Scene N / Narrator: / Visual Prompt:) are parsed by `parse_structured`.
    - Otherwise blank lines separate paragraphs; each paragraph becomes one or more scenes.
    - Long paragraphs are chunked every `sentences_per_scene` sentences.
    - An inline `[visual: ...]` tag overrides the auto-generated image prompt for that paragraph.
    """
    script = script.replace("\r\n", "\n").strip()
    if not script:
        return []
    if is_structured(script):
        return parse_structured(script)
    scenes: list[Scene] = []
    for para in re.split(r"\n\s*\n", script):
        tag = _VISUAL_TAG.search(para)
        explicit_visual = tag.group(1).strip() if tag else None
        text = " ".join(_VISUAL_TAG.sub("", para).split())
        if not text and explicit_visual:
            # A visual with no narration: attach to the previous scene.
            if scenes:
                scenes[-1].visual = explicit_visual
            continue
        if not text:
            continue
        sents = _sentences(text) or [text]
        n = max(1, int(sentences_per_scene))
        chunks = [" ".join(sents[i:i + n]) for i in range(0, len(sents), n)]
        for i, chunk in enumerate(chunks):
            visual = explicit_visual if (explicit_visual and i == 0) else auto_visual(chunk)
            scenes.append(Scene(chunk, visual))
    return scenes


def build_prompt(visual: str, style_key: str, subject: str = "") -> str:
    parts = [visual.strip()]
    if subject.strip():
        parts.append(subject.strip())
    style = STYLES.get(style_key, "")
    if style:
        parts.append(style)
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------- 2. voice

def list_voices() -> list[str]:
    """English macOS voices (novelty voices removed)."""
    novelty = {"Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Wobble", "Good News",
               "Jester", "Organ", "Superstar", "Trinoids", "Whisper", "Zarvox", "Junior", "Ralph", "Fred",
               "Kathy"}
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ["Samantha"]
    voices = []
    for line in out.splitlines():
        m = re.match(r"^(.+?)\s+(\w\w_\w\w)\s+#", line)
        if m and m.group(2).startswith("en_") and m.group(1) not in novelty:
            voices.append(m.group(1).strip())
    return list(dict.fromkeys(voices)) or ["Samantha"]


TTS_SR = 24000
SEGMENT_GAP = 0.45       # pause between speakers inside a scene
_CHILD_WORDS = ("child", "kid", "boy", "girl", "baby")

# Kokoro-82M: small neural TTS, runs locally on CPU. First letter of the id = accent (a=US, b=UK).
KOKORO_VOICES = {
    "British male · George (documentary narrator)": "bm_george",
    "British male · Fable (storyteller)": "bm_fable",
    "British male · Lewis (deep, calm)": "bm_lewis",
    "British male · Daniel": "bm_daniel",
    "British female · Emma (warm lecturer)": "bf_emma",
    "British female · Isabella": "bf_isabella",
    "British female · Lily": "bf_lily",
    "American female · Heart (expressive)": "af_heart",
    "American female · Bella": "af_bella",
    "American female · Nicole (soft)": "af_nicole",
    "American male · Michael": "am_michael",
    "American male · Fenrir": "am_fenrir",
    "American male · Puck (bright, playful)": "am_puck",
}

# Microsoft Edge neural voices: free, very natural, but text is sent to Microsoft's servers.
EDGE_VOICES = {
    "British male · Ryan (documentary)": "en-GB-RyanNeural",
    "British male · Thomas": "en-GB-ThomasNeural",
    "British female · Sonia (lecturer)": "en-GB-SoniaNeural",
    "British female · Libby": "en-GB-LibbyNeural",
    "British female · Maisie (child)": "en-GB-MaisieNeural",
    "American male · Andrew (warm storyteller)": "en-US-AndrewNeural",
    "American male · Brian (calm narrator)": "en-US-BrianNeural",
    "American male · Christopher": "en-US-ChristopherNeural",
    "American female · Ava (expressive)": "en-US-AvaNeural",
    "American female · Emma": "en-US-EmmaNeural",
    "American female · Ana (child)": "en-US-AnaNeural",
}

TTS_ENGINES = {
    "Kokoro · neural, local (recommended)": "kokoro",
    "Edge · neural, free cloud (Microsoft)": "edge",
    "macOS say · basic, instant": "say",
}

DEFAULT_VOICES = {  # engine -> (narrator, character)
    "kokoro": ("British male · George (documentary narrator)", "American male · Puck (bright, playful)"),
    "edge": ("British male · Ryan (documentary)", "British female · Maisie (child)"),
    "say": (None, None),
}


def voices_for(engine_label: str) -> list[str]:
    engine = TTS_ENGINES[engine_label]
    if engine == "kokoro":
        return list(KOKORO_VOICES)
    if engine == "edge":
        return list(EDGE_VOICES)
    return list_voices()


def default_voices(engine_label: str) -> tuple[str, str]:
    engine = TTS_ENGINES[engine_label]
    narrator, character = DEFAULT_VOICES[engine]
    voices = voices_for(engine_label)
    if engine == "say":
        narrator = next((v for v in voices if v.startswith("Daniel")), voices[0])
        character = next((v for v in voices if v.startswith("Samantha")), voices[-1])
    return narrator, character


class _Kokoro:
    _pipes: dict = {}

    @classmethod
    def speak(cls, text: str, voice_id: str, speed: float):
        import numpy as np
        from kokoro import KPipeline

        lang = voice_id[0]
        if lang not in cls._pipes:
            print(f"[voice] loading Kokoro ({'British' if lang == 'b' else 'American'} English)…")
            cls._pipes[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M", device="cpu")
        chunks = [np.asarray(audio) for _, _, audio in cls._pipes[lang](text, voice=voice_id, speed=speed,
                                                                          split_pattern=r"\n+")]
        return np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)


def _to_wav(src: Path, dst: Path, pitch_up: bool = False):
    """Normalize any audio to 24kHz mono WAV; optionally raise pitch for a child character."""
    af = f"aresample={TTS_SR}"
    if pitch_up:
        af += f",asetrate={TTS_SR}*1.22,aresample={TTS_SR},atempo=0.88"
    _run_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-af", af,
                 "-ac", "1", str(dst)])


def _speak_segment(text: str, engine: str, voice_label: str, speed: float, child: bool, tmp: Path):
    import numpy as np
    import soundfile as sf

    pitch_up = child and "child" not in voice_label.lower()
    if engine == "kokoro":
        audio = _Kokoro.speak(text, KOKORO_VOICES[voice_label], speed)
        if not pitch_up:
            return audio.astype(np.float32)
        sf.write(tmp.with_suffix(".raw.wav"), audio, TTS_SR)
        src = tmp.with_suffix(".raw.wav")
    elif engine == "edge":
        import asyncio
        import edge_tts

        rate = f"{int(round((speed - 1) * 100)):+d}%"
        src = tmp.with_suffix(".mp3")
        asyncio.run(edge_tts.Communicate(text, EDGE_VOICES[voice_label], rate=rate).save(str(src)))
    else:
        txt = tmp.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        src = tmp.with_suffix(".aiff")
        subprocess.run(["say", "-v", voice_label, "-r", str(int(175 * speed)), "-o", str(src), "-f", str(txt)],
                       check=True, capture_output=True)
    wav = tmp.with_suffix(".wav")
    _to_wav(src, wav, pitch_up)
    data, _ = sf.read(wav, dtype="float32")
    return data


def synthesize_voice(narration: str, out_path: Path, engine_label: str, narrator_voice: str,
                     character_voice: str, speed: float) -> Path:
    """Narration (possibly multi-speaker) -> one WAV. Narrator lines use the narrator voice, any other
    speaker uses the character voice; child speakers are pitched up unless the voice is already a child."""
    import numpy as np
    import soundfile as sf

    engine = TTS_ENGINES[engine_label]
    out_path.with_suffix(".txt").write_text(narration, encoding="utf-8")
    pieces = []
    for k, (speaker, text) in enumerate(parse_segments(narration)):
        is_narrator = speaker.lower() == "narrator"
        voice = narrator_voice if is_narrator else character_voice
        child = not is_narrator and any(w in speaker.lower() for w in _CHILD_WORDS)
        if pieces:
            pieces.append(np.zeros(int(SEGMENT_GAP * TTS_SR), dtype=np.float32))
        pieces.append(_speak_segment(text, engine, voice, speed, child, out_path.with_name(f"{out_path.stem}_{k}")))
    wav = out_path.with_suffix(".wav")
    audio = np.concatenate(pieces) if pieces else np.zeros(TTS_SR, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) or 1.0
    sf.write(wav, (audio / peak * 0.89).astype(np.float32), TTS_SR)  # consistent loudness across engines
    for tmp in out_path.parent.glob(f"{out_path.stem}_*"):  # per-segment intermediates
        tmp.unlink()
    return wav


def media_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


# ---------------------------------------------------------------- 3. images

class ImageBackend:
    name = "base"

    def generate(self, prompt: str, size: tuple[int, int], seed: int, out_path: Path) -> Path:
        raise NotImplementedError


class LocalDiffusionBackend(ImageBackend):
    """SD-Turbo / SDXL-Turbo on Apple Silicon (MPS). Loaded once, reused across runs."""

    MODELS = {
        "Local · SD-Turbo (fast, ~2.5GB, best for 8GB RAM)": "stabilityai/sd-turbo",
        "Local · SDXL-Turbo (higher quality, ~7GB, slow on 8GB)": "stabilityai/sdxl-turbo",
    }
    _pipe = None
    _loaded_id = None

    def __init__(self, label: str):
        self.model_id = self.MODELS[label]
        self.name = label

    @classmethod
    def _load(cls, model_id: str):
        if cls._loaded_id == model_id and cls._pipe is not None:
            return cls._pipe
        import torch
        from diffusers import AutoPipelineForText2Image

        cls.unload()
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if device == "mps" else torch.float32
        print(f"[image] loading {model_id} on {device} ({dtype}) — first run downloads the weights…")
        pipe = AutoPipelineForText2Image.from_pretrained(model_id, torch_dtype=dtype, variant="fp16")
        pipe.to(device)
        pipe.enable_attention_slicing()  # lowers peak memory on 8GB unified RAM
        pipe.set_progress_bar_config(disable=True)
        cls._pipe, cls._loaded_id = pipe, model_id
        return pipe

    @classmethod
    def unload(cls):
        if cls._pipe is None:
            return
        cls._pipe, cls._loaded_id = None, None
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

    def generate(self, prompt, size, seed, out_path):
        import torch

        pipe = self._load(self.model_id)
        w, h = size
        if "sdxl" in self.model_id and max(w, h) < 768:
            w, h = w * 3 // 2 // 64 * 64, h * 3 // 2 // 64 * 64
        gen = torch.Generator("cpu").manual_seed(int(seed))
        image = pipe(prompt=prompt, num_inference_steps=2, guidance_scale=0.0,
                     width=w, height=h, generator=gen).images[0]
        image.save(out_path)
        return out_path


class PollinationsBackend(ImageBackend):
    """Free hosted API (no key). Sends the prompt text to image.pollinations.ai."""

    name = "Cloud · Pollinations (free API, saves RAM/battery)"

    def generate(self, prompt, size, seed, out_path):
        import requests

        w, h = size[0] * 2, size[1] * 2  # ask for a larger image; it's free and upscales better
        url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
        params = {"width": w, "height": h, "seed": int(seed), "nologo": "true", "model": "flux"}
        last_err = None
        for _ in range(3):
            try:
                r = requests.get(url, params=params, timeout=120)
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image"):
                    raise RuntimeError(f"unexpected response: {r.text[:200]}")
                out_path.write_bytes(r.content)
                Image.open(out_path).convert("RGB").save(out_path)
                return out_path
            except Exception as e:  # network hiccups / rate limits
                last_err = e
        raise RuntimeError(f"Pollinations failed: {last_err}")


class PlaceholderBackend(ImageBackend):
    """No AI: gradient title cards. Instant, offline — handy to test voice + timing."""

    name = "Offline · Placeholder cards (no AI, instant)"
    PALETTES = [((38, 70, 83), (42, 157, 143)), ((61, 64, 91), (129, 178, 154)), ((90, 24, 154), (224, 170, 255)),
                ((20, 33, 61), (252, 163, 17)), ((0, 48, 73), (214, 40, 40))]

    def generate(self, prompt, size, seed, out_path):
        w, h = size[0] * 2, size[1] * 2
        top, bottom = self.PALETTES[int(seed) % len(self.PALETTES)]
        img = Image.new("RGB", (w, h))
        px = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(1, h - 1)
            px.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
        font = _font(max(22, w // 26))
        lines = textwrap.wrap(prompt.split(",")[0], width=28)[:5]
        text = "\n".join(lines)
        bbox = px.multiline_textbbox((0, 0), text, font=font, align="center", spacing=10)
        px.multiline_text(((w - bbox[2]) / 2, (h - bbox[3]) / 2), text, font=font, fill="white",
                          align="center", spacing=10)
        img.save(out_path)
        return out_path


def image_backend_choices() -> list[str]:
    return list(LocalDiffusionBackend.MODELS) + [PollinationsBackend.name, PlaceholderBackend.name]


def make_backend(label: str) -> ImageBackend:
    if label in LocalDiffusionBackend.MODELS:
        return LocalDiffusionBackend(label)
    LocalDiffusionBackend.unload()  # free RAM if switching away from a local model
    if label == PollinationsBackend.name:
        return PollinationsBackend()
    return PlaceholderBackend()


# ---------------------------------------------------------------- 4. captions + scene clips

def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_caption(text: str, video_size: tuple[int, int], out_path: Path) -> Path:
    """Full-frame transparent PNG with the narration in a rounded box near the bottom."""
    W, H = video_size
    font_size = max(24, int(min(W, H) * 0.042))
    font = _font(font_size)
    chars_per_line = max(18, int(W * 0.86 / (font_size * 0.52)))
    lines = textwrap.wrap(text, width=chars_per_line)
    if len(lines) > 4:  # keep captions readable: shrink once, then truncate
        font_size = int(font_size * 0.82)
        font = _font(font_size)
        chars_per_line = max(18, int(W * 0.86 / (font_size * 0.52)))
        lines = textwrap.wrap(text, width=chars_per_line)
        if len(lines) > 5:
            lines = lines[:5]
            lines[-1] = lines[-1].rstrip(" ,.;") + "…"
    body = "\n".join(lines)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    spacing = int(font_size * 0.3)
    bbox = d.multiline_textbbox((0, 0), body, font=font, align="center", spacing=spacing)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = int(font_size * 0.8), int(font_size * 0.55)
    box_w, box_h = tw + 2 * pad_x, th + 2 * pad_y
    x0 = (W - box_w) // 2
    y0 = H - box_h - int(H * 0.06)
    d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=int(font_size * 0.6), fill=(0, 0, 0, 165))
    d.multiline_text((x0 + pad_x - bbox[0], y0 + pad_y - bbox[1]), body, font=font, fill=(255, 255, 255, 255),
                     align="center", spacing=spacing)
    img.save(out_path)
    return out_path


def _ken_burns(index: int, frames: int, W: int, H: int, strength: float) -> str:
    """Alternate zoom-in / zoom-out and pan direction per scene for variety."""
    z_end = 1.0 + strength
    step = (z_end - 1.0) / max(1, frames)
    if index % 2 == 0:
        zoom = f"min(1+{step:.6f}*on,{z_end:.4f})"
    else:
        zoom = f"max({z_end:.4f}-{step:.6f}*on,1)"
    pans = [
        ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                              # center
        ("(iw-iw/zoom)*on/{f}", "ih/2-(ih/zoom/2)"),                           # left -> right
        ("iw/2-(iw/zoom/2)", "(ih-ih/zoom)*on/{f}"),                           # top -> bottom
        ("(iw-iw/zoom)*(1-on/{f})", "ih/2-(ih/zoom/2)"),                       # right -> left
    ]
    x, y = (p.format(f=frames) for p in pans[index % len(pans)])
    # Upscale 4x before zoompan so the sub-pixel motion is smooth instead of jittery.
    return (f"scale={W * 4}:{H * 4}:force_original_aspect_ratio=increase,crop={W * 4}:{H * 4},"
            f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}")


def render_scene(index: int, image: Path, audio: Path, caption: Path | None, video_size: tuple[int, int],
                 out_path: Path, motion: float) -> Path:
    W, H = video_size
    duration = media_duration(audio) + SCENE_TAIL_PAD
    frames = int(round(duration * FPS))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-framerate", str(FPS), "-i", str(image), "-i", str(audio)]
    if motion > 0:
        vf = _ken_burns(index, frames, W, H, motion)
    else:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    if caption:
        cmd += ["-i", str(caption)]
        graph = f"[0:v]{vf},format=yuv420p[bg];[bg][2:v]overlay=0:0,format=yuv420p[v]"
    else:
        graph = f"[0:v]{vf},format=yuv420p[v]"
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a",
            "-af", f"apad=pad_dur={SCENE_TAIL_PAD}",
            "-t", f"{duration:.3f}", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", str(out_path)]
    _run_ffmpeg(cmd)
    return out_path


def concat_clips(clips: list[Path], out_path: Path) -> Path:
    listing = out_path.with_suffix(".txt")
    listing.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    _run_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out_path)])
    return out_path


def _run_ffmpeg(cmd: list[str]):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{res.stderr[-2000:]}")


def check_tools() -> list[str]:
    missing = [t for t in ("ffmpeg", "ffprobe", "say") if shutil.which(t) is None]
    return missing
