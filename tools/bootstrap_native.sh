#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="$(uname -s)"

if [[ "$OS" == "Darwin" ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required on macOS: https://brew.sh"
    exit 1
  fi

  deps=(cmake pkg-config opus portaudio)
  for dep in "${deps[@]}"; do
    if ! brew list "$dep" >/dev/null 2>&1; then
      brew install "$dep"
    fi
  done

  echo "macOS native dependencies ready."
elif [[ "$OS" == "Linux" ]]; then
  echo "Linux detected. Install equivalents for: cmake pkg-config opus portaudio"
  echo "Example (Debian/Ubuntu):"
  echo "  sudo apt-get update"
  echo "  sudo apt-get install -y cmake pkg-config libopus-dev portaudio19-dev"
else
  echo "Unsupported OS: $OS"
  exit 1
fi

cat <<MSG

Next steps:
1. Build native engine:
   cmake -S "$ROOT_DIR/native" -B "$ROOT_DIR/native/build"
   cmake --build "$ROOT_DIR/native/build" --config Release
2. Build cffi binding (optional):
   TELECALLS_NATIVE_LIB_DIR="$ROOT_DIR/native/build" python -m telecraft.client.calls.native_build
MSG
