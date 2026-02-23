# Third-party VoIP Dependencies (tgcalls backend)

This directory is reserved for vendored dependencies used by the
`telecalls_tgcalls_engine` backend.

## Expected layout

```text
native/third_party/
  lib_tgcalls/
  webrtc/
  VERSIONS.txt
```

## Source of truth

For reproducible bootstrapping, we currently use the MarshalX scaffold
repository because it already contains a CMake-oriented split of:

- `tgcalls/third_party/lib_tgcalls`
- `tgcalls/third_party/webrtc`

The official `TelegramMessenger/tgcalls` repository is used as protocol
reference and runtime behavior reference, but does not ship all build
artifacts/dependencies required for standalone local CMake builds.

## Bootstrap helper

Use:

```bash
./native/tools/fetch_tgcalls_deps.sh
```

This script fetches pinned commits (including required submodules) and copies
only the required third-party trees into this directory. It also writes
`VERSIONS.txt` with the exact source and submodule commit hashes, then applies
the local compatibility patch set used by this repository.

## Licensing

- `lib_tgcalls`: LGPL-3.0
- `webrtc`: BSD-style (plus transitive third-party notices)

Keep dynamic linking for LGPL-covered components when distributing binaries.
