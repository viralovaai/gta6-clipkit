#!/usr/bin/env bash
# ClipKit one-command setup: checks ffmpeg + Python, builds whisper.cpp
# if whisper-cli is missing, downloads the base.en model (~142 MB),
# and installs HyperFrames (Node.js) for the bold template's kinetic
# captions. Run once:  bash setup.sh
set -e
cd "$(dirname "$0")"

echo "== ClipKit setup =="
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not found — install it first (apt install ffmpeg)"; exit 1; }
command -v cmake >/dev/null || { echo "cmake not found — install it first (apt install cmake)"; exit 1; }

if ! command -v whisper-cli >/dev/null && [ ! -x "$HOME/workspace/tools/whisper-cpp/build/bin/whisper-cli" ]; then
  echo "-- whisper.cpp not found, cloning + building (takes a few minutes) --"
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git /tmp/whisper.cpp-build
  cmake -S /tmp/whisper.cpp-build -B /tmp/whisper.cpp-build/build -DCMAKE_BUILD_TYPE=Release
  cmake --build /tmp/whisper.cpp-build/build -j "$(nproc)" --target whisper-cli
  mkdir -p tools
  cp -r /tmp/whisper.cpp-build tools/whisper-cpp
  rm -rf /tmp/whisper.cpp-build
else
  echo "-- whisper-cli found --"
fi

MODEL="$HOME/workspace/tools/whisper-cpp/models/ggml-base.en.bin"
if [ ! -f "$MODEL" ]; then
  echo "-- downloading base.en model (~142 MB) --"
  mkdir -p "$(dirname "$MODEL")"
  curl -L -o "$MODEL" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
else
  echo "-- model found --"
fi

# ---- HyperFrames (bold template: portfolio-reel kinetic captions) ----
# Needs Node.js 18+. Without it, clips still render — with ASS captions
# instead of the HyperFrames overlay — but the bold template will NOT
# look like the portfolio reel, and clipkit.py will say so loudly.
if command -v node >/dev/null && command -v npm >/dev/null; then
  echo "-- installing hyperframes + gsap into hf-assets/ --"
  (cd hf-assets && npm install --no-audit --no-fund)
  echo "-- hyperframes ready (bold template enabled) --"
else
  echo "!! node/npm NOT found — HyperFrames will be UNAVAILABLE."
  echo "   Clips will still render, but the bold template falls back to"
  echo "   plain ASS captions (no portfolio-reel kinetic look)."
  echo "   Install Node.js 18+ (https://nodejs.org) and re-run setup.sh"
  echo "   to enable HyperFrames captions."
fi

echo ""
echo "Done. Try it:"
echo "  python3 clipkit.py YOUR_LONG_VIDEO.mp4 --clips 3"
echo "  ls out/"
