#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo 'FFmpeg is required. Install with: brew install ffmpeg'
  exit 1
fi
if [ ! -f .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet -r requirements.txt
printf '\nLocal app: http://127.0.0.1:8899\nClose Terminal / press Ctrl+C to stop.\n'
(sleep 2; open 'http://127.0.0.1:8899') &
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8899
