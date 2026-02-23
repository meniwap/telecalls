# tgcalls Native Backend

This document describes the optional `tgcalls` backend path for calls that use
`phoneConnectionWebrtc`.

## Status

- `native_backend="legacy"` keeps using `telecalls_engine` (current default-safe path).
- `native_backend="tgcalls"` loads `telecalls_tgcalls_engine` through
  `telecraft.client.calls._tgcalls_cffi`.
- `native_backend="auto"` tries `tgcalls` first, then falls back to `legacy`.

## Vendoring (reproducible)

Use the helper script:

```bash
./native/tools/fetch_tgcalls_deps.sh
```

It mirrors the MarshalX third-party structure into:

- `native/third_party/lib_tgcalls`
- `native/third_party/webrtc`
- `native/third_party/VERSIONS.txt` (source commit + submodule SHAs)

After copying sources, the script also applies local compatibility patches used
by this repo (macOS clang flags, libvpx link flags, optional `sdkmacos` guards,
and protobuf include fixes).

## Build steps (macOS)

```bash
cmake -S native -B native/build -DCMAKE_PREFIX_PATH="/opt/homebrew/opt/jpeg;/opt/homebrew"
cmake --build native/build --config Release

TELECALLS_NATIVE_LIB_DIR="$PWD/native/build" \
  .venv/bin/python -m telecraft.client.calls.tgcalls_native_build
```

Expected native artifacts:

- `native/build/libtelecalls_lib_tgcalls.dylib`
- `native/build/libtelecalls_tgcalls_engine.dylib`

`tg_owt` is linked statically into `libtelecalls_lib_tgcalls.dylib` in the
current macOS setup to avoid hidden-symbol/linker issues.

Required native packages on macOS:

- `cmake`
- `pkg-config`
- `opus`
- `portaudio`
- `openssl@3`
- `ffmpeg`
- `jpeg`

## Runtime wiring

- `CallsManager` extracts relay endpoints (`phoneConnection*`) and WebRTC ICE servers.
- Relay endpoints are passed with `tc_engine_set_remote_endpoints`.
- ICE/TURN entries are passed with `tc_engine_set_rtc_servers`.
- Native backend wraps `tgcalls::Meta::Create(...)` (`InstanceImpl`) and feeds
  signaling through `receiveSignalingData` / `signalingDataEmitted`.
- Call readiness (`media_ready`) is now driven by native tgcalls state
  (`TC_ENGINE_STATE_ESTABLISHED`) instead of shim loopback semantics.

## Audio policy

- For `native_backend="tgcalls"`, audio is native-managed.
- Python PortAudio/Null audio loops are intentionally skipped to avoid synthetic
  media counters.
- PortAudio diagnostics are still available via:

```bash
python tools/smoke_audio_devices.py
```

Optional device override env vars:

- `TELECALLS_PA_INPUT_DEVICE`
- `TELECALLS_PA_OUTPUT_DEVICE`

## Version negotiation

- Stage 1 target: `library_versions=["9.0.0"]`
- Stage 2 optional target: `library_versions=["11.0.0","9.0.0"]`

v11 includes SCTP signaling framing; the backend must fully support the
tgcalls signaling stack before promoting v11 as default.
