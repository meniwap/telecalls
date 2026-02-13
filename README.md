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

## Not in scope (current)

- complete Telegram VoIP media compatibility yet (work in progress)
- production-grade reconnect/fallback tuning

## Quick start

```python
from telecraft.client import Client, ClientInit

client = Client(
    network="prod",
    session_path=".sessions/prod_dc2.session.json",
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
        "audio_enabled": True,
        "audio_backend": "portaudio",
        "allow_p2p": False,  # relay-first default
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
cmake --build native/build --config Release

TELECALLS_NATIVE_LIB_DIR=\"$PWD/native/build\" \\
python -m telecraft.client.calls.native_build

# or all-in-one optional build hook
TELECALLS_BUILD_NATIVE=1 python tools/build_native.py
```

## Smoke login/connect

```bash
python tools/smoke_login.py --network prod
python tools/smoke_get_me.py --network prod --session .sessions/prod_dc2.session.json
python tools/smoke_call_handshake.py --network prod --session .sessions/prod_dc2.session.json --peer @username --runs 20
```
