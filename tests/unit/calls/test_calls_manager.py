from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from telecraft.client.calls.manager import CallsManager
from telecraft.client.calls.session import CallSession
from telecraft.client.calls.state import CallEndReason, CallState


class _Entities:
    def input_user(self, user_id: int) -> Any:
        return SimpleNamespace(TL_NAME="inputUser", user_id=int(user_id), access_hash=111)


class _Raw:
    def __init__(self) -> None:
        self.self_user_id = 777
        self.entities = _Entities()
        self._subs: list[asyncio.Queue[Any]] = []
        self.calls: list[str] = []

    async def start_updates(self, *, timeout: float = 20.0) -> None:
        _ = timeout

    def subscribe_updates(self, *, maxsize: int = 1024) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    def unsubscribe_updates(self, q: asyncio.Queue[Any]) -> None:
        if q in self._subs:
            self._subs.remove(q)

    async def resolve_peer(self, ref: Any, *, timeout: float = 20.0) -> Any:
        _ = (ref, timeout)
        return SimpleNamespace(peer_type="user", peer_id=555)

    async def prime_entities(self, *, limit: int = 100, timeout: float = 20.0) -> None:
        _ = (limit, timeout)

    async def invoke_api(self, req: Any, *, timeout: float = 20.0) -> Any:
        _ = timeout
        name = getattr(req, "TL_NAME", type(req).__name__)
        self.calls.append(str(name))

        if name == "phone.requestCall":
            return SimpleNamespace(
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallWaiting",
                    id=987,
                    access_hash=654,
                    admin_id=777,
                    participant_id=555,
                ),
                users=[],
                chats=[],
            )

        if name in {"phone.acceptCall", "phone.confirmCall"}:
            return SimpleNamespace(
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallAccepted",
                    id=1,
                    access_hash=2,
                ),
                users=[],
                chats=[],
            )

        return SimpleNamespace(ok=True, users=[], chats=[])

    def _ingest_from_updates_result(self, obj: Any) -> None:
        _ = obj



def test_calls_manager_incoming_and_signaling_update() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)

        incoming: list[CallSession] = []
        signaling_packets: list[bytes] = []

        def _on_incoming(session: CallSession) -> None:
            incoming.append(session)
            session.on_signaling_data(lambda data: signaling_packets.append(data))

        manager.on_incoming(_on_incoming)
        await manager.start()

        assert raw._subs
        q = raw._subs[0]

        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCall",
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallRequested",
                    id=101,
                    access_hash=202,
                    admin_id=999,
                    participant_id=777,
                ),
            )
        )
        await asyncio.sleep(0.01)

        assert len(incoming) == 1
        assert incoming[0].state == CallState.RINGING_IN

        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCallSignalingData",
                phone_call_id=101,
                data=b"abc",
            )
        )
        await asyncio.sleep(0.01)

        assert signaling_packets == [b"abc"]

        await manager.stop()

    asyncio.run(_case())


def test_accept_then_hangup_race_does_not_crash() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        session = CallSession(
            call_id=1,
            access_hash=2,
            incoming=True,
            manager=manager,
            state=CallState.RINGING_IN,
        )

        await asyncio.gather(session.accept(), session.hangup())

        assert session.state in {CallState.DISCONNECTING, CallState.ENDED, CallState.CONNECTING}
        if session.state != CallState.CONNECTING:
            assert session.end_reason in {CallEndReason.LOCAL_HANGUP, None}

    asyncio.run(_case())


def test_receiver_never_crashes_and_marks_session_failed_on_unexpected_update() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        session = CallSession(
            call_id=404,
            access_hash=505,
            incoming=True,
            manager=manager,
            state=CallState.CONNECTING,
        )
        manager._sessions[404] = session

        def _raise(_phone_call: Any) -> int:
            raise RuntimeError("bad update payload")

        manager._extract_access_hash = _raise  # type: ignore[method-assign]

        await manager.start()
        q = raw._subs[0]
        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCall",
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCall",
                    id=404,
                    access_hash=505,
                ),
            )
        )
        await asyncio.sleep(0.01)

        assert session.state == CallState.FAILED
        assert session.end_reason == CallEndReason.FAILED_PROTOCOL

        await manager.stop()

    asyncio.run(_case())


def test_outgoing_call_moves_to_connecting() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)

        session = await manager.call("@someone")

        assert session.call_id == 987
        assert session.access_hash == 654
        assert session.state == CallState.CONNECTING

    asyncio.run(_case())
