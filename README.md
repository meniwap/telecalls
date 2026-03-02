# telecalls

> WIP: this project is still under active development and does not work reliably yet.

Experimental MTProto-first Telegram calls signaling library (Mac-first, no audio engine yet).

## Scope (current)

- MTProto connect/auth/session
- updates stream
- call signaling state machine
- incoming/outgoing call sessions

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
call = await client.calls.call("@username")
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

## Smoke login/connect

```bash
python tools/smoke_login.py --network prod
python tools/smoke_get_me.py --network prod --session .sessions/prod_dc2.session.json
```
