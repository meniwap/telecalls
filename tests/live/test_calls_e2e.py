from __future__ import annotations

import asyncio
import os

import pytest

from telecraft.client import CallState, Client, ClientInit


@pytest.mark.live
@pytest.mark.requires_second_account
def test_live_calls_e2e_outgoing() -> None:
    api_id_raw = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    session = os.environ.get("TELECALLS_LIVE_SESSION")
    peer = os.environ.get("TELECALLS_LIVE_CALL_PEER")

    if not api_id_raw or not api_hash or not session or not peer:
        pytest.skip(
            "missing TELEGRAM_API_ID/TELEGRAM_API_HASH/TELECALLS_LIVE_SESSION/"
            "TELECALLS_LIVE_CALL_PEER"
        )

    try:
        api_id = int(api_id_raw)
    except ValueError:
        pytest.skip("invalid TELEGRAM_API_ID")

    hold_seconds = float(os.environ.get("TELECALLS_LIVE_HOLD_SECONDS", "60"))

    async def _case() -> None:
        client = Client(
            network=os.environ.get("TELECALLS_LIVE_NETWORK", "prod"),
            dc_id=int(os.environ.get("TELECALLS_LIVE_DC", "2")),
            session_path=session,
            init=ClientInit(api_id=api_id, api_hash=api_hash),
            enable_calls=True,
            calls_config={
                "native_bridge_enabled": True,
                "native_test_mode": False,
                "allow_p2p": False,
            },
        )

        await client.connect(timeout=30.0)
        try:
            call = await client.calls.call(peer, timeout=40.0)
            terminal = asyncio.Event()

            def _on_state(state: CallState) -> None:
                if state in {CallState.IN_CALL, CallState.ENDED, CallState.FAILED}:
                    terminal.set()

            call.on_state_change(_on_state)
            await asyncio.wait_for(terminal.wait(), timeout=40.0)
            assert call.state in {CallState.IN_CALL, CallState.ENDED}

            if call.state == CallState.IN_CALL:
                await asyncio.sleep(hold_seconds)
                await call.hangup()
        finally:
            await client.close()

    asyncio.run(_case())
