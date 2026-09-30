#!/bin/zsh
# One-command launcher: creates the venv on first run, then starts the app at http://127.0.0.1:7860
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v espeak-ng >/dev/null || brew install espeak-ng   # phonemizer for Kokoro TTS
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
export PYTORCH_ENABLE_MPS_FALLBACK=1
exec .venv/bin/python app.py
