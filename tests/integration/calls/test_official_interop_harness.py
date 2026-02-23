from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from telecraft.client.calls.manager import CallsManager
from telecraft.client.calls.session import CallSession
from telecraft.client.calls.state import CallState


class _RawHarness:
    def __init__(self) -> None:
        self.self_user_id = 7
        self._subs: list[asyncio.Queue[Any]] = []

    async def start_updates(self, *, timeout: float = 20.0) -> None:
        _ = timeout

    def subscribe_updates(self, *, maxsize: int = 1024) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    def unsubscribe_updates(self, q: asyncio.Queue[Any]) -> None:
        if q in self._subs:
            self._subs.remove(q)

    async def invoke_api(self, req: Any, *, timeout: float = 20.0) -> Any:
        _ = timeout
        name = getattr(req, "TL_NAME", "")
        if name == "messages.getDhConfig":
            return SimpleNamespace(TL_NAME="messages.dhConfigNotModified")
        if name == "phone.getCallConfig":
            return SimpleNamespace(TL_NAME="dataJSON", data="{}")
        return SimpleNamespace(TL_NAME="ok")


def test_manager_ingests_signaling_without_crash_when_session_exists() -> None:
    async def _case() -> None:
        raw = _RawHarness()
        manager = CallsManager(raw=raw, enabled=True)
        await manager.start()

        session = CallSession(
            call_id=404,
            access_hash=505,
            incoming=True,
            manager=manager,
            state=CallState.CONNECTING,
        )
        manager._sessions[session.call_id] = session

        assert raw._subs
        q = raw._subs[0]
        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCallSignalingData",
                phone_call_id=session.call_id,
                data=b"\x01\x02\x03",
            )
        )
        await asyncio.sleep(0.02)

        assert session.signaling_rx_count == 1
        assert session.state == CallState.CONNECTING
        await manager.stop()

    asyncio.run(_case())
