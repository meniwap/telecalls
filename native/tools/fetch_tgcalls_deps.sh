#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
THIRD_PARTY_DIR="$ROOT_DIR/native/third_party"
WORK_DIR="$ROOT_DIR/.tmp/tgcalls_vendor"
VERSIONS_FILE="$THIRD_PARTY_DIR/VERSIONS.txt"

# Pinned source commit for reproducible vendoring.
MARSHALX_REPO="https://github.com/MarshalX/tgcalls.git"
MARSHALX_COMMIT="${MARSHALX_COMMIT:-7e6b5b11877fa39d6959ea429af3c6950e666768}"

mkdir -p "$WORK_DIR"
mkdir -p "$THIRD_PARTY_DIR"

if [[ ! -d "$WORK_DIR/tgcalls/.git" ]]; then
  git clone --depth 1 "$MARSHALX_REPO" "$WORK_DIR/tgcalls"
fi

if ! git -C "$WORK_DIR/tgcalls" cat-file -e "${MARSHALX_COMMIT}^{commit}" 2>/dev/null; then
  git -C "$WORK_DIR/tgcalls" fetch origin "$MARSHALX_COMMIT" --depth 1
else
  git -C "$WORK_DIR/tgcalls" fetch origin "$MARSHALX_COMMIT" --depth 1 >/dev/null 2>&1 || true
fi
git -C "$WORK_DIR/tgcalls" checkout -f "$MARSHALX_COMMIT"
CHECKED_OUT_COMMIT="$(git -C "$WORK_DIR/tgcalls" rev-parse HEAD)"
git -C "$WORK_DIR/tgcalls" submodule sync --recursive
git -C "$WORK_DIR/tgcalls" submodule update --init --recursive --depth 1

SUBMODULE_CMAKE_COMMIT="$(
  git -C "$WORK_DIR/tgcalls" rev-parse HEAD:cmake 2>/dev/null || echo "missing"
)"
SUBMODULE_PYBIND11_COMMIT="$(
  git -C "$WORK_DIR/tgcalls" rev-parse HEAD:tgcalls/third_party/pybind11 2>/dev/null || echo "missing"
)"
SUBMODULE_LIBVPX_COMMIT="$(
  git -C "$WORK_DIR/tgcalls" rev-parse HEAD:tgcalls/third_party/webrtc/src/third_party/libvpx/source/libvpx 2>/dev/null || echo "missing"
)"
SUBMODULE_LIBYUV_COMMIT="$(
  git -C "$WORK_DIR/tgcalls" rev-parse HEAD:tgcalls/third_party/webrtc/src/third_party/libyuv 2>/dev/null || echo "missing"
)"

SRC_BASE="$WORK_DIR/tgcalls/tgcalls/third_party"
if [[ ! -d "$SRC_BASE/lib_tgcalls" || ! -d "$SRC_BASE/webrtc" ]]; then
  echo "Expected third-party layout not found under $SRC_BASE"
  exit 1
fi

rm -rf "$THIRD_PARTY_DIR/lib_tgcalls" "$THIRD_PARTY_DIR/webrtc"
cp -R "$SRC_BASE/lib_tgcalls" "$THIRD_PARTY_DIR/lib_tgcalls"
cp -R "$SRC_BASE/webrtc" "$THIRD_PARTY_DIR/webrtc"

# Apply compatibility patches after vendoring:
# - Disable WEBRTC_USE_H264 to avoid FFmpeg API mismatches on modern Homebrew.
# - Silence Apple-specific narrowing warnings promoted to errors by clang.
python3 - "$THIRD_PARTY_DIR/webrtc/cmake/libwebrtcbuild.cmake" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
filtered = [line for line in lines if "WEBRTC_USE_H264" not in line]
path.write_text("\n".join(filtered) + "\n")
PY

python3 - "$THIRD_PARTY_DIR/webrtc/cmake/init_target.cmake" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
needle = "                -Wno-deprecated-declarations\n"
insert = (
    "                -Wno-deprecated-declarations\n"
    "                -Wno-narrowing\n"
    "                -Wno-c++11-narrowing\n"
)
if needle in text and "-Wno-c++11-narrowing" not in text:
    text = text.replace(needle, insert, 1)
path.write_text(text)
PY

python3 - "$THIRD_PARTY_DIR/webrtc/cmake/protobuf/CMakeLists.txt" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
fallback_block = (
    "set(TG_OWT_PROTOBUF_INCLUDE_DIRS \"${Protobuf_INCLUDE_DIRS}\")\n"
    "if(TG_OWT_PROTOBUF_INCLUDE_DIRS STREQUAL \"\" AND DEFINED Protobuf_INCLUDE_DIR)\n"
    "    set(TG_OWT_PROTOBUF_INCLUDE_DIRS \"${Protobuf_INCLUDE_DIR}\")\n"
    "endif()\n\n"
)
marker = (
    "# We have to push it to the main project because the generated sources\n"
    "# and headers will be used as include files.\n"
)
if "TG_OWT_PROTOBUF_INCLUDE_DIRS" not in text and marker in text:
    text = text.replace(marker, marker + fallback_block, 1)

text = text.replace(
    "target_include_directories(proto\nINTERFACE\n    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}>\nPRIVATE\n    ${Protobuf_INCLUDE_DIRS}\n)\n",
    "target_include_directories(proto\nPUBLIC\n    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}>\n    ${TG_OWT_PROTOBUF_INCLUDE_DIRS}\n)\n",
)
text = text.replace(
    "target_include_directories(proto\nINTERFACE\n    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}>\n    ${Protobuf_INCLUDE_DIRS}\n)\n",
    "target_include_directories(proto\nPUBLIC\n    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}>\n    ${TG_OWT_PROTOBUF_INCLUDE_DIRS}\n)\n",
)
path.write_text(text)
PY

python3 - "$THIRD_PARTY_DIR/webrtc/cmake/external.cmake" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    "target_link_libraries(${target_name} PRIVATE ${LIBVPX_LIBRARIES})",
    "target_link_libraries(${target_name} PRIVATE ${LIBVPX_LINK_LIBRARIES})",
)
path.write_text(text)
PY

python3 - "$THIRD_PARTY_DIR/webrtc/CMakeLists.txt" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    "option(TG_OWT_BUILD_AUDIO_BACKENDS \"Build webrtc audio backends.\" ON)\n",
    "option(TG_OWT_BUILD_AUDIO_BACKENDS \"Build webrtc audio backends.\" ON)\n"
    "option(TG_OWT_BUILD_SDKMACOS \"Build Apple ObjC SDK wrappers.\" ON)\n",
)
text = text.replace(
    "if (APPLE)\n    include(cmake/libsdkmacos.cmake)\nendif()\n",
    "if (APPLE AND TG_OWT_BUILD_SDKMACOS)\n    include(cmake/libsdkmacos.cmake)\nendif()\n",
)
text = text.replace(
    "if (APPLE)\n    target_link_libraries(tg_owt PUBLIC tg_owt::libsdkmacos)\nendif()\n",
    "if (APPLE AND TG_OWT_BUILD_SDKMACOS)\n    target_link_libraries(tg_owt PUBLIC tg_owt::libsdkmacos)\nendif()\n",
)
text = text.replace(
    "elseif (APPLE)\n    set(platform_export\n        libsdkmacos\n    )\n",
    "elseif (APPLE AND TG_OWT_BUILD_SDKMACOS)\n    set(platform_export\n        libsdkmacos\n    )\n",
)
text = text.replace(
    "if (NOT TG_OWT_USE_PROTOBUF)\n    remove_target_sources(tg_owt ${webrtc_loc}\n        logging/rtc_event_log/encoder/rtc_event_log_encoder_legacy.cc\n        logging/rtc_event_log/encoder/rtc_event_log_encoder_new_format.cc\n    )\nendif()\n",
    "if (NOT TG_OWT_USE_PROTOBUF)\n    remove_target_sources(tg_owt ${webrtc_loc}\n        logging/rtc_event_log/encoder/rtc_event_log_encoder_legacy.cc\n        logging/rtc_event_log/encoder/rtc_event_log_encoder_new_format.cc\n        logging/rtc_event_log/rtc_event_log_impl.cc\n    )\nendif()\n",
)
path.write_text(text)
PY

cat > "$VERSIONS_FILE" <<EOF
source_repo=$MARSHALX_REPO
requested_commit=$MARSHALX_COMMIT
checked_out_commit=$CHECKED_OUT_COMMIT
vendored_at_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
paths=lib_tgcalls,webrtc
submodule.cmake_helpers=$SUBMODULE_CMAKE_COMMIT
submodule.pybind11=$SUBMODULE_PYBIND11_COMMIT
submodule.libvpx=$SUBMODULE_LIBVPX_COMMIT
submodule.libyuv=$SUBMODULE_LIBYUV_COMMIT
EOF

cat <<MSG
Vendored tgcalls dependencies updated:
  - $THIRD_PARTY_DIR/lib_tgcalls
  - $THIRD_PARTY_DIR/webrtc
Metadata:
  - $VERSIONS_FILE
MSG
