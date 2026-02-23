# Third-party notices

This project can optionally use additional third-party VoIP components for the
`tgcalls` native backend.

## Telegram Calls Library (tgcalls)

- Upstream: https://github.com/TelegramMessenger/tgcalls
- License: LGPL-3.0
- Notes:
  - The optional `telecalls_tgcalls_engine` backend is intended to use dynamic
    linking for LGPL-covered components.
  - Current dynamic artifacts in `native/build/` include:
    - `libtelecalls_tgcalls_engine.dylib`
    - `libtelecalls_lib_tgcalls.dylib`
  - `tg_owt` (WebRTC) is currently linked statically into
    `libtelecalls_lib_tgcalls.dylib` for symbol-visibility compatibility on
    macOS.
  - If you distribute binaries that include or link this backend, provide
    license text and relinking instructions as required by LGPL.
  - Practical relinking workflow:
    1. Rebuild from source with `cmake -S native -B native/build`.
    2. Replace the shipped `libtelecalls_tgcalls_engine.dylib` and dependent
       `libtelecalls_lib_tgcalls.dylib`.
    3. Re-run CFFI binding build if needed:
       `TELECALLS_NATIVE_LIB_DIR=<path> python -m telecraft.client.calls.tgcalls_native_build`.

## WebRTC

- Upstream: https://webrtc.googlesource.com/src
- License: BSD-style (plus transitive third-party licenses in the WebRTC tree)

## Additional vendored scaffold source

- MarshalX/tgcalls scaffold: https://github.com/MarshalX/tgcalls
- Used for reproducible vendoring/build scaffolding of:
  - `lib_tgcalls`
  - `webrtc`
