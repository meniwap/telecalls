# telecalls

MTProto-first Telegram calls library (Mac-first) with signaling, handshake crypto, and
native transport/audio building blocks.

## Scope (current)

- MTProto connect/auth/session
- updates stream
- call signaling state machine
- incoming/outgoing call sessions
- handshake crypto (DH + key fingerprint verification)
- runtime call-config parsing (`phone.getCallConfig`)
- optional native transport (`native/` + `cffi` bridge)
- Opus codec path in native engine (when `libopus` is available)
- optional PortAudio backend (`audio_enabled=True`)
- MTProto2 short-format signaling packet parser/crypto in native core
- strict readiness gating (`server_ready && media_ready`) by default
- E2E visual key derivation (fingerprint hex + 4 emoji marker)

## Not in scope (current)

- complete Telegram VoIP media compatibility yet (work in progress)
- production-grade reconnect/fallback tuning

## Quick start

```python
from telecraft.client import Client, ClientInit

client = Client(
    network="prod",
    session_path=".sessions/prod_dc4.session.json",
    init=ClientInit(api_id=12345, api_hash="..."),
    enable_calls=True,
)

await client.connect()

client.calls.on_incoming(lambda call: print("incoming", call.call_id))

# outgoing signaling session
call = await client.calls.call("@username", video=False)
await call.hangup()

await client.close()
```

### Audio-enabled call example (macOS/Linux with PortAudio)

```python
client = Client(
    network="prod",
    session_path=".sessions/prod_dc4.session.json",
    init=ClientInit(api_id=12345, api_hash="..."),
    enable_calls=True,
    calls_config={
        "native_bridge_enabled": True,
        "audio_enabled": True,  # ignored when native_backend resolves to "tgcalls"
        "audio_backend": "portaudio",
        "allow_p2p": False,  # relay-first default
        "native_backend": "auto",  # auto: prefer tgcalls backend when available
        "interop_profile": "tgvoip_v9",
        "network_type": "wifi",
        "protocol_min_layer": 65,
        "protocol_max_layer": 92,
        "library_versions": ["9.0.0"],
        "strict_media_ready": True,
        "disable_soft_media_ready": True,
        "degraded_in_call_mode": False,  # keep false for official interop runs
        "udp_media_required_for_in_call": True,  # avoid signaling-only false positives
        "media_ready_timeout": 8.0,
        "signaling_dump_path": ".sessions/signaling.dump.log",  # optional scrubbed digest log
    },
)
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"

python -m ruff check src tests tools
python -m mypy src
python -m pytest -m "not live"
```

### Native bootstrap (optional)

```bash
./tools/bootstrap_native.sh

cmake -S native -B native/build
# macOS Homebrew users may need:
# cmake -S native -B native/build -DCMAKE_PREFIX_PATH="/opt/homebrew/opt/jpeg;/opt/homebrew"
cmake --build native/build --config Release

TELECALLS_NATIVE_LIB_DIR=\"$PWD/native/build\" \\
python -m telecraft.client.calls.native_build

# optional tgcalls backend cffi module
TELECALLS_NATIVE_LIB_DIR=\"$PWD/native/build\" \\
python -m telecraft.client.calls.tgcalls_native_build

# or all-in-one optional build hook
TELECALLS_BUILD_NATIVE=1 python tools/build_native.py

# optional PortAudio smoke diagnostics
python tools/smoke_audio_devices.py
```

## Smoke login/connect

```bash
python tools/smoke_login.py --network prod
python tools/smoke_get_me.py --network prod --dc 4 --session .sessions/prod_dc4.session.json
python tools/smoke_call_handshake.py --network prod --dc 4 --session .sessions/prod_dc4.session.json --peer @username --runs 20
python tools/smoke_call_voice.py --network prod --dc 4 --session .sessions/prod_dc4.session.json --peer @username --hold-seconds 60
```

`smoke_call_voice.py` now reports `ok_signaling_only` vs `ok_media_interop` and prints raw
native counters (`udp_tx_bytes`, `udp_rx_bytes`, `raw_media_packets_*`) to avoid false
positives from local audio push activity.
For `ok_media_interop`, the smoke script now also requires inbound UDP activity
(`udp_in_frames`/`udp_rx_bytes`) in addition to outbound UDP and raw media TX.
It also prints UDP recv diagnostics (`udp_recv_attempts`, `udp_recv_timeouts`,
`udp_recv_source_mismatch`, `udp_proto_decode_failures`, `decrypt_failures_udp`) and
selected endpoint metadata (`selected_endpoint_id`, `selected_endpoint_kind`) for faster
interop triage.
For signaling-stage failures (before UDP starts), it also prints native signaling diagnostics:
`signaling_proto_decode_failures`, `signaling_decrypt_ctr_failures`,
`signaling_decrypt_short_failures`, `signaling_last_decrypt_mode`,
`signaling_last_decrypt_direction`, `signaling_decrypt_last_error_code`,
`signaling_decrypt_last_error_stage`, `signaling_proto_last_error_code`,
`signaling_candidate_winner_index`, and `native_handshake_block_reason`.

Runbook note: keep live call attempts on `DC4` (`.sessions/prod_dc4.session.json`) and do one
attempt per diagnose/fix cycle.

## Native backend selection

`calls_config["native_backend"]` supports:

- `"legacy"`: existing C engine (`telecalls_engine`)
  - `STREAM_DATA` is routed over UDP reflector path (not `phone.sendSignalingData`).
  - Inbound reflector UDP packets are decrypted, parsed (short format), and dispatched
    locally (`PING/PONG/STREAM_STATE/STREAM_DATA/NOP`) without touching signaling callbacks.
- `"tgcalls"`: optional tgcalls-compatible engine (`telecalls_tgcalls_engine`)
- `"auto"`: try tgcalls backend first, fallback to legacy backend

For third-party licensing and distribution notes, see [`NOTICE.md`](NOTICE.md).
