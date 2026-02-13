# telecalls

Minimal MTProto-first Telegram calls signaling library (Mac-first, no audio engine yet).

## Scope (current)

- MTProto connect/auth/session
- updates stream
- call signaling state machine
- incoming/outgoing call sessions
- handshake crypto (DH + key fingerprint verification)
- optional native transport skeleton (`native/` + `cffi` bridge)

## Not in scope (current)

- audio capture/playback
- VoIP media engine binding
- platform-specific audio backends

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
python tools/smoke_call_handshake.py --network prod --session .sessions/prod_dc2.session.json --peer @username
```
