#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="$(uname -s)"

if [[ "$OS" == "Darwin" ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required on macOS: https://brew.sh"
    exit 1
  fi

  deps=(cmake pkg-config opus portaudio openssl@3 ffmpeg jpeg)
  for dep in "${deps[@]}"; do
    if ! brew list "$dep" >/dev/null 2>&1; then
      brew install "$dep"
    fi
  done

  echo "macOS native dependencies ready."
elif [[ "$OS" == "Linux" ]]; then
  echo "Linux detected. Install equivalents for: cmake pkg-config opus portaudio openssl"
  echo "Example (Debian/Ubuntu):"
  echo "  sudo apt-get update"
  echo "  sudo apt-get install -y cmake pkg-config libopus-dev portaudio19-dev libssl-dev"
else
  echo "Unsupported OS: $OS"
  exit 1
fi

cat <<MSG

Next steps:
1. Build native engine:
   cmake -S "$ROOT_DIR/native" -B "$ROOT_DIR/native/build" -DCMAKE_PREFIX_PATH="/opt/homebrew/opt/jpeg;/opt/homebrew"
   cmake --build "$ROOT_DIR/native/build" --config Release
2. Build cffi binding (optional):
   TELECALLS_NATIVE_LIB_DIR="$ROOT_DIR/native/build" python -m telecraft.client.calls.native_build
3. Build tgcalls cffi binding (optional backend):
   TELECALLS_NATIVE_LIB_DIR="$ROOT_DIR/native/build" python -m telecraft.client.calls.tgcalls_native_build
4. Vendor tgcalls dependencies scaffold (optional):
   "$ROOT_DIR/native/tools/fetch_tgcalls_deps.sh"
5. PortAudio diagnostics helper (optional):
   python "$ROOT_DIR/tools/smoke_audio_devices.py"
MSG
