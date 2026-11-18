#!/usr/bin/env bash
# GTA 6 Clipper setup: checks what this Mac (or Linux box) needs, installs what's missing, says when it's ready.
# Safe to re-run any time.
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$SKILL_DIR/.venv"
MODELS="$SKILL_DIR/models"
mkdir -p "$MODELS"
ok()   { printf "  [ok]      %s\n" "$1"; }
inst() { printf "  [install] %s\n" "$1"; }
fail() { printf "  [MISSING] %s\n" "$1"; FAILED=1; }
FAILED=0

echo "GTA 6 Clipper setup"
OS="$(uname)"
if [[ "$OS" != "Darwin" && "$OS" != "Linux" ]]; then echo "Mac or Linux only for now."; exit 1; fi
WHISPER_CLI="whisper-cli"

if [[ "$OS" == "Darwin" ]]; then
  # 1. Homebrew
  if command -v brew >/dev/null 2>&1; then ok "Homebrew"; else
    fail "Homebrew (install from https://brew.sh then re-run setup)"; exit 1; fi

  # 2. Command-line tools: ffmpeg (cutting/encoding), whisper-cpp (local transcript), deno (YouTube downloads)
  for pair in "ffmpeg:ffmpeg" "whisper-cli:whisper-cpp" "deno:deno"; do
    bin="${pair%%:*}"; formula="${pair##*:}"
    if command -v "$bin" >/dev/null 2>&1; then ok "$formula"; else
      inst "$formula"; brew install "$formula" >/dev/null 2>&1 && ok "$formula" || fail "$formula (brew install $formula)"; fi
  done
else
  # 1. Linux (Debian/Ubuntu): ffmpeg + build tools from apt
  SUDO=""; [[ $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
  NEED=""
  command -v ffmpeg >/dev/null 2>&1 || NEED="$NEED ffmpeg"
  command -v cmake  >/dev/null 2>&1 || NEED="$NEED cmake"
  command -v g++    >/dev/null 2>&1 || NEED="$NEED build-essential"
  command -v git    >/dev/null 2>&1 || NEED="$NEED git"
  ls /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf >/dev/null 2>&1 || NEED="$NEED fonts-dejavu-core"
  if [[ -n "$NEED" ]]; then
    if command -v apt-get >/dev/null 2>&1; then
      inst "apt:$NEED"; $SUDO apt-get update -qq >/dev/null 2>&1; $SUDO apt-get install -y -qq $NEED >/dev/null 2>&1 || fail "apt packages:$NEED"
    else fail "install with your package manager:$NEED"; fi
  fi
  command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg" || fail "ffmpeg"

  # 2. whisper.cpp built from source into the skill folder (no Homebrew on Linux)
  WHISPER_CLI="$SKILL_DIR/bin/whisper-cli"
  if [[ -x "$WHISPER_CLI" ]]; then ok "whisper-cpp"; else
    inst "whisper-cpp (building from source, ~2-4 min)"
    SRC="$SKILL_DIR/.build/whisper.cpp"
    rm -rf "$SRC"; mkdir -p "$SKILL_DIR/.build" "$SKILL_DIR/bin"
    if git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp.git "$SRC" \
       && cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF >/dev/null 2>&1 \
       && cmake --build "$SRC/build" -j"$(nproc)" --target whisper-cli >/dev/null 2>&1 \
       && cp "$SRC/build/bin/whisper-cli" "$WHISPER_CLI"; then
      rm -rf "$SKILL_DIR/.build"; ok "whisper-cpp"
    else fail "whisper-cpp (build failed)"; fi
  fi
fi

# 3. uv (fast Python + venv manager)
if command -v uv >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/uv" ]]; then ok "uv"; else
  inst "uv"; curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 && ok "uv" || fail "uv"; fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

# 4. Python environment: yt-dlp (always latest, sites change weekly), OpenCV (facecam finder), Pillow (captions)
if [[ ! -x "$VENV/bin/python" ]]; then inst "Python 3.12 environment"; "$UV" venv --python 3.12 "$VENV" >/dev/null 2>&1; fi
PKGS=("yt-dlp[default]" opencv-python-headless pillow numpy)
[[ "$OS" == "Linux" ]] && PKGS+=(deno)   # deno (YouTube downloads) ships as a PyPI wheel; lands in .venv/bin
"$UV" pip install --python "$VENV/bin/python" -q -U "${PKGS[@]}" >/dev/null 2>&1 \
  && ok "Python packages (${PKGS[*]})" || fail "Python packages"

# 5. Whisper model (speech -> text, runs on this Mac). Reuse an existing one if present.
WHISPER_MODEL=""
for m in "$MODELS/ggml-small.en.bin" "$MODELS/ggml-small.bin" "$HOME/.cache/whisper-cpp/ggml-small.en.bin" "$HOME/.cache/whisper-cpp/ggml-small.bin"; do
  [[ -f "$m" ]] && WHISPER_MODEL="$m" && break; done
if [[ -z "$WHISPER_MODEL" ]]; then
  inst "Whisper small.en model (~465 MB)"
  curl -fL -s -o "$MODELS/ggml-small.en.bin.part" https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin \
    && mv "$MODELS/ggml-small.en.bin.part" "$MODELS/ggml-small.en.bin" && WHISPER_MODEL="$MODELS/ggml-small.en.bin" \
    || { rm -f "$MODELS/ggml-small.en.bin.part"; fail "whisper model (needs access to huggingface.co)"; }
fi
[[ -n "$WHISPER_MODEL" ]] && ok "Whisper model: $WHISPER_MODEL"

# 6. Face detector model (YuNet, ~230 KB) used to find the facecam box
YUNET="$MODELS/face_detection_yunet_2023mar.onnx"
if [[ ! -f "$YUNET" ]]; then
  inst "YuNet face detector"
  curl -L -s -o "$YUNET" https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx || fail "YuNet model"
fi
[[ -s "$YUNET" ]] && ok "Face detector" || fail "Face detector"

# 7. Fonts (macOS ships these)
if [[ "$OS" == "Darwin" ]]; then
  [[ -f "/System/Library/Fonts/Supplemental/Arial Black.ttf" ]] && ok "Caption font (Arial Black)" || fail "Arial Black font"
else
  [[ -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf || -f /usr/share/fonts/opentype/inter/Inter-Black.otf ]] \
    && ok "Caption font" || fail "Caption font (apt-get install fonts-dejavu-core)"
fi

# 8. Free space check: audio-only download keeps this small, but warn early
FREE_GB=$(df -Pk "$SKILL_DIR" | awk 'NR==2{print int($4/1048576)}')
if (( FREE_GB < 3 )); then echo "  [warn]    Only ${FREE_GB} GB free. Each stream needs ~0.5-1 GB while it runs."; else ok "Disk space (${FREE_GB} GB free)"; fi

cat > "$SKILL_DIR/config.json" <<EOF
{"whisper_model": "$WHISPER_MODEL", "yunet_model": "$YUNET", "whisper_cli": "$WHISPER_CLI"}
EOF

if [[ $FAILED -eq 0 ]]; then
  date > "$SKILL_DIR/.ready"
  echo; echo "READY. Say: clip this stream <link>   or   find me today's biggest GTA 6 streams and clip them"
else
  rm -f "$SKILL_DIR/.ready"; echo; echo "NOT READY: fix the [MISSING] items above and re-run setup."; exit 1
fi
