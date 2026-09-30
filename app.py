"""Local Mac script-to-video generator (Gradio UI).

Paste a script -> scenes are planned automatically -> each scene gets an AI image + voiceover
-> clips get Ken Burns motion and captions -> one MP4. Run: ./run.sh  (or: .venv/bin/python app.py)
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import gradio as gr

import pipeline as pl

OUTPUT_ROOT = Path(__file__).parent / "outputs"
DEFAULT_TTS = next(iter(pl.TTS_ENGINES))
SAMPLE_FILE = Path(__file__).parent / "samples" / "how_humans_discovered_numbers.md"

SAMPLE_SCRIPT = SAMPLE_FILE.read_text() if SAMPLE_FILE.exists() else """Have you ever wondered how a tiny seed becomes a giant tree? It all starts underground, where the seed soaks up water and wakes up.

A small root pushes down into the soil to find water and minerals. At the same time, a green shoot reaches up toward the light.
[visual: a sprouting seed underground with a root growing down and a green shoot growing up, cross-section view]

Once the shoot breaks through the surface, its leaves open wide. They catch sunlight and turn it into food through photosynthesis.

Year after year, the stem thickens into a strong trunk. Branches spread out, and one day that tiny seed becomes a tree tall enough to shade a whole family."""

SCENE_HEADERS = ["Narration (spoken + captioned)", "Visual prompt (what to draw)"]


def plan_scenes(script: str, sentences_per_scene: int):
    scenes = pl.split_script(script, sentences_per_scene)
    if not scenes:
        raise gr.Error("Paste a script first.")
    return [[s.narration, s.visual] for s in scenes]


def _rows_to_scenes(rows) -> list[pl.Scene]:
    if rows is None:
        return []
    if hasattr(rows, "values"):  # pandas DataFrame from gr.Dataframe
        rows = rows.values.tolist()
    scenes = []
    for row in rows:
        narration = str(row[0] or "").strip() if len(row) > 0 else ""
        visual = str(row[1] or "").strip() if len(row) > 1 else ""
        if narration:
            scenes.append(pl.Scene(narration, visual or pl.auto_visual(narration)))
    return scenes


def on_engine_change(engine_label):
    voices = pl.voices_for(engine_label)
    narrator, character = pl.default_voices(engine_label)
    return gr.update(choices=voices, value=narrator), gr.update(choices=voices, value=character)


def preview_voice(script, scene_rows, engine_label, narrator, character, speed):
    scenes = _rows_to_scenes(scene_rows) or pl.split_script(script)
    text = scenes[0].narration if scenes else "Long, long ago, humans had no numbers."
    if not any(s != "Narrator" for s, _ in pl.parse_segments(text)):
        text += "\nChild: One… two… three!"
    out = OUTPUT_ROOT / "voice_previews"
    out.mkdir(parents=True, exist_ok=True)
    return str(pl.synthesize_voice(text, out / f"preview_{int(time.time())}", engine_label, narrator, character,
                                   float(speed)))


def generate_video(script, scene_rows, sentences_per_scene, backend_label, style, subject, aspect, tts_engine,
                   narrator, character, speed, captions, motion, seed, progress=gr.Progress()):
    missing = pl.check_tools()
    if missing:
        raise gr.Error(f"Missing tools: {', '.join(missing)}. Install with: brew install ffmpeg")

    scenes = _rows_to_scenes(scene_rows) or pl.split_script(script, sentences_per_scene)
    if not scenes:
        raise gr.Error("Paste a script (or plan scenes) first.")

    video_size, image_size = pl.ASPECTS[aspect]
    seed = int(seed) if seed and int(seed) >= 0 else random.randint(0, 2**31 - 1)
    run_dir = OUTPUT_ROOT / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "script.txt").write_text(
        "\n\n".join(f"[{i + 1}] {s.narration}\n    visual: {s.visual}" for i, s in enumerate(scenes)))

    backend = pl.make_backend(backend_label)
    n = len(scenes)
    clips, gallery, log = [], [], []
    t0 = time.time()
    for i, scene in enumerate(scenes):
        tag = f"scene_{i + 1:02d}"
        progress(i / n, desc=f"Scene {i + 1}/{n}: voiceover")
        audio = pl.synthesize_voice(scene.narration, run_dir / f"{tag}_voice", tts_engine, narrator, character,
                                    float(speed))

        progress((i + 0.25) / n, desc=f"Scene {i + 1}/{n}: image ({backend_label.split('(')[0].strip()})")
        prompt = pl.build_prompt(scene.visual, style, subject)
        ts = time.time()
        try:
            image = backend.generate(prompt, image_size, seed + i, run_dir / f"{tag}.png")
        except Exception as e:
            raise gr.Error(f"Image generation failed on scene {i + 1}: {e}") from e
        img_secs = time.time() - ts

        progress((i + 0.7) / n, desc=f"Scene {i + 1}/{n}: animating clip")
        caption = pl.render_caption(pl.caption_text(scene.narration), video_size, run_dir / f"{tag}_caption.png") if captions else None
        clip = pl.render_scene(i, image, audio, caption, video_size, run_dir / f"{tag}.mp4", float(motion))
        clips.append(clip)
        gallery.append((str(image), f"{i + 1}. {pl.caption_text(scene.narration)[:80]}"))
        log.append(f"Scene {i + 1}: image {img_secs:.1f}s · {pl.media_duration(clip):.1f}s clip")

    progress(0.98, desc="Stitching final video")
    final = pl.concat_clips(clips, run_dir / "final_video.mp4")
    total = pl.media_duration(final)
    log.append(f"\n✅ {n} scenes · {total:.1f}s video · built in {time.time() - t0:.0f}s · seed {seed}")
    log.append(f"Saved to: {final}")
    return str(final), gallery, "\n".join(log)


with gr.Blocks(title="Local Script-to-Video") as demo:
    gr.Markdown("# 🍎 Local Script → Video Generator\n"
                "Paste a script. It's split into scenes, each gets an AI illustration + neural voiceover, "
                "then animated (Ken Burns) and captioned into one MP4. Runs on your M1 — no fees.")
    with gr.Row():
        with gr.Column(scale=5):
            script_in = gr.Textbox(label="Script", lines=12, value=SAMPLE_SCRIPT,
                                   info="Screenplay format (Scene N / Narrator: / Child: / Visual Prompt:) or plain "
                                        "paragraphs with optional [visual: ...] tags.")
            with gr.Row():
                spp = gr.Slider(1, 5, value=2, step=1, label="Sentences per scene")
                plan_btn = gr.Button("1 · Plan scenes (optional, editable)")
            scenes_df = gr.Dataframe(headers=SCENE_HEADERS, datatype=["str", "str"], column_count=(2, "fixed"),
                                     interactive=True, wrap=True, column_widths=["55%", "45%"], label="Scene plan — edit freely; leave empty to "
                                                                        "auto-plan from the script")
            with gr.Accordion("Settings", open=True):
                with gr.Row():
                    backend_in = gr.Dropdown(pl.image_backend_choices(), value=pl.image_backend_choices()[0],
                                             label="Image engine",
                                             info="Local engines share your 8GB RAM: close other browsers for speed.")
                    style_in = gr.Dropdown(list(pl.STYLES), value="3D animated film", label="Art style")
                    aspect_in = gr.Dropdown(list(pl.ASPECTS), value="16:9 Landscape", label="Format")
                subject_in = gr.Textbox(label="Consistent subject / setting (optional)",
                                        placeholder="e.g. a friendly cartoon owl teacher, green forest background",
                                        info="Appended to every scene prompt to keep a consistent look.")
                with gr.Row():
                    tts_in = gr.Dropdown(list(pl.TTS_ENGINES), value=DEFAULT_TTS, label="Voice engine")
                    speed_in = gr.Slider(0.7, 1.3, value=0.92, step=0.02, label="Speaking speed",
                                         info="~0.9 gives a measured, documentary pace")
                with gr.Row():
                    narrator_in = gr.Dropdown(pl.voices_for(DEFAULT_TTS), value=pl.default_voices(DEFAULT_TTS)[0],
                                              label="Narrator voice")
                    character_in = gr.Dropdown(pl.voices_for(DEFAULT_TTS), value=pl.default_voices(DEFAULT_TTS)[1],
                                               label="Character voice (Child:, Hunter:, …)",
                                               info="Child speakers are pitched up automatically.")
                with gr.Row():
                    preview_btn = gr.Button("🔊 Preview voices", size="sm")
                    preview_out = gr.Audio(label="Voice preview", type="filepath", interactive=False)
                with gr.Row():
                    captions_in = gr.Checkbox(value=True, label="Burn in captions")
                    motion_in = gr.Slider(0, 0.35, value=0.15, step=0.01, label="Ken Burns zoom (0 = static)")
                    seed_in = gr.Number(value=-1, precision=0, label="Seed (-1 = random)")
            go_btn = gr.Button("2 · Generate video", variant="primary", size="lg")
        with gr.Column(scale=4):
            video_out = gr.Video(label="Final video")
            gallery_out = gr.Gallery(label="Scene images", columns=3, height=260)
            log_out = gr.Textbox(label="Log", lines=8)

    plan_btn.click(plan_scenes, [script_in, spp], scenes_df)
    tts_in.change(on_engine_change, tts_in, [narrator_in, character_in])
    preview_btn.click(preview_voice, [script_in, scenes_df, tts_in, narrator_in, character_in, speed_in], preview_out)
    script_in.change(lambda: [], None, scenes_df)  # script edited -> stale plan is cleared
    go_btn.click(generate_video,
                 [script_in, scenes_df, spp, backend_in, style_in, subject_in, aspect_in, tts_in, narrator_in,
                  character_in, speed_in, captions_in, motion_in, seed_in],
                 [video_out, gallery_out, log_out])

if __name__ == "__main__":
    OUTPUT_ROOT.mkdir(exist_ok=True)
    demo.queue().launch(server_name="127.0.0.1", server_port=7860, inbrowser=False,
                        allowed_paths=[str(OUTPUT_ROOT)])
