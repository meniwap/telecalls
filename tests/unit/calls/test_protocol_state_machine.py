from __future__ import annotations

import asyncio

from telecraft.client.calls.manager import CallsManager, CallsManagerConfig
from telecraft.client.calls.session import CallSession
from telecraft.client.calls.state import CallState


class _Raw:
    self_user_id = 1


def test_refresh_call_readiness_requires_native_media_when_strict() -> None:
    async def _case() -> None:
        manager = CallsManager(
            raw=_Raw(),
            enabled=True,
            config=CallsManagerConfig(strict_media_ready=True, disable_soft_media_ready=True),
        )
        session = CallSession(call_id=1, access_hash=2, incoming=False, manager=manager)
        manager._sessions[session.call_id] = session
        session.state = CallState.CONNECTING
        session.server_ready = True
        session.media_ready = False

        manager._refresh_call_readiness(session)
        assert session.state == CallState.CONNECTING
        assert "media" in session.timers

        manager._cancel_timer(session.call_id, "media")
        session.media_ready = True
        manager._refresh_call_readiness(session)
        assert session.state == CallState.IN_CALL

    asyncio.run(_case())


def test_refresh_call_readiness_can_relax_media_requirement() -> None:
    async def _case() -> None:
        manager = CallsManager(
            raw=_Raw(),
            enabled=True,
            config=CallsManagerConfig(strict_media_ready=False, disable_soft_media_ready=True),
        )
        session = CallSession(call_id=11, access_hash=22, incoming=False, manager=manager)
        manager._sessions[session.call_id] = session
        session.state = CallState.CONNECTING
        session.server_ready = True
        session.media_ready = False

        manager._refresh_call_readiness(session)
        assert session.state == CallState.IN_CALL
        assert session.media_ready is True

    asyncio.run(_case())


def test_refresh_call_readiness_blocks_in_call_until_udp_gate() -> None:
    async def _case() -> None:
        manager = CallsManager(
            raw=_Raw(),
            enabled=True,
            config=CallsManagerConfig(
                strict_media_ready=True,
                disable_soft_media_ready=True,
                native_bridge_enabled=True,
                udp_media_required_for_in_call=True,
            ),
        )
        session = CallSession(call_id=21, access_hash=22, incoming=False, manager=manager)
        manager._sessions[session.call_id] = session
        session.state = CallState.CONNECTING
        session.server_ready = True
        session.media_ready = True
        session._stats.udp_tx_bytes = 0
        session._stats.udp_rx_bytes = 0

        manager._refresh_call_readiness(session)
        assert session.state == CallState.CONNECTING
        assert session.in_call_gate_satisfied is False
        assert session.in_call_gate_block_reason == "udp_gate"

        session._stats.udp_tx_bytes = 58
        manager._refresh_call_readiness(session)
        assert session.state == CallState.IN_CALL
        assert session.in_call_gate_satisfied is True
        assert session.in_call_gate_block_reason is None

    asyncio.run(_case())


def test_derive_native_handshake_block_reason_prefers_signaling_decrypt_then_udp_gate() -> None:
    manager = CallsManager(
        raw=_Raw(),
        enabled=True,
        config=CallsManagerConfig(strict_media_ready=True, disable_soft_media_ready=True),
    )
    session = CallSession(call_id=31, access_hash=41, incoming=True, manager=manager)
    session.state = CallState.CONNECTING
    session.native_state = 6
    session.server_ready = True
    session.media_ready = False
    session._stats.decrypt_failures_signaling = 2

    reason = manager._derive_native_handshake_block_reason(session, session._stats)
    assert reason == "signaling_decrypt"

    session.native_state = 7
    session.server_ready = True
    session.media_ready = True
    session.in_call_gate_block_reason = "udp_gate"
    reason = manager._derive_native_handshake_block_reason(session, session._stats)
    assert reason == "udp_gate"
