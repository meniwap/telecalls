from __future__ import annotations

import asyncio
import os

import pytest

from telecraft.client import CallState, Client, ClientInit


@pytest.mark.live
@pytest.mark.requires_second_account
def test_private_call_official_client_bidirectional_media() -> None:
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

    hold_seconds = float(os.environ.get("TELECALLS_LIVE_HOLD_SECONDS", "15"))
    require_media_recv = os.environ.get("TELECALLS_LIVE_REQUIRE_MEDIA_RECV", "1") == "1"
    library_versions = [
        item.strip()
        for item in os.environ.get("TELECALLS_LIVE_LIBRARY_VERSIONS", "9.0.0").split(",")
        if item.strip()
    ]
    if not library_versions:
        library_versions = ["9.0.0"]

    async def _case() -> None:
        client = Client(
            network=os.environ.get("TELECALLS_LIVE_NETWORK", "prod"),
            dc_id=int(os.environ.get("TELECALLS_LIVE_DC", "4")),
            session_path=session,
            init=ClientInit(api_id=api_id, api_hash=api_hash),
            enable_calls=True,
            calls_config={
                "native_bridge_enabled": True,
                "native_test_mode": False,
                "native_backend": os.environ.get("TELECALLS_LIVE_NATIVE_BACKEND", "auto"),
                "allow_p2p": False,
                "protocol_min_layer": 65,
                "protocol_max_layer": 92,
                "library_versions": library_versions,
                "strict_media_ready": True,
                "disable_soft_media_ready": True,
            },
        )
        await client.connect(timeout=35.0)
        try:
            call = await client.calls.call(peer, timeout=45.0)
            terminal = asyncio.Event()

            def _on_state(state: CallState) -> None:
                if state in {CallState.IN_CALL, CallState.ENDED, CallState.FAILED}:
                    terminal.set()

            call.on_state_change(_on_state)
            await asyncio.wait_for(terminal.wait(), timeout=45.0)
            assert call.state == CallState.IN_CALL
            assert call.e2e_key_fingerprint_hex is not None
            assert call.e2e_emojis is not None
            assert len(call.e2e_emojis) == 4

            max_packets_recv = 0
            max_signaling_recv = 0
            max_media_recv = 0
            start = asyncio.get_running_loop().time()
            while (asyncio.get_running_loop().time() - start) < hold_seconds:
                await asyncio.sleep(1.0)
                stats = call.stats()
                media_recv = int(stats.get("media_packets_recv") or 0)
                packets_recv = int(stats.get("packets_recv") or 0)
                signaling_recv = int(stats.get("signaling_packets_recv") or 0)
                if media_recv > max_media_recv:
                    max_media_recv = media_recv
                if packets_recv > max_packets_recv:
                    max_packets_recv = packets_recv
                if signaling_recv > max_signaling_recv:
                    max_signaling_recv = signaling_recv

            await call.hangup()
            if require_media_recv:
                assert (
                    max_media_recv > 0 or max_packets_recv > max_signaling_recv
                ), (
                    "expected real media/traffic from native engine, got "
                    f"media_packets_recv={max_media_recv}, "
                    f"packets_recv={max_packets_recv}, "
                    f"signaling_packets_recv={max_signaling_recv}"
                )
        finally:
            await client.close()

    asyncio.run(_case())
