from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from telecraft.client.peers import PeerRef

from .session import CallSession
from .signaling import CallSignalingAdapter
from .state import CallEndReason, CallState, can_transition

IncomingHandler = Callable[[CallSession], Any]


class CallsManager:
    def __init__(self, *, raw: Any, enabled: bool = False) -> None:
        self._raw = raw
        self._enabled = enabled
        self._signaling = CallSignalingAdapter(raw)
        self._incoming_handlers: list[IncomingHandler] = []
        self._sessions: dict[int, CallSession] = {}
        self._updates_q: asyncio.Queue[Any] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def on_incoming(self, handler: IncomingHandler) -> None:
        self._incoming_handlers.append(handler)

    def get(self, call_id: int) -> CallSession | None:
        return self._sessions.get(int(call_id))

    async def start(self) -> None:
        if not self._enabled:
            return
        await self._raw.start_updates()
        if self._task is not None:
            return
        self._updates_q = self._raw.subscribe_updates(maxsize=2048)
        self._task = asyncio.create_task(self._updates_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        if self._updates_q is not None:
            self._raw.unsubscribe_updates(self._updates_q)
        self._updates_q = None
        self._task = None

    async def call(self, peer: PeerRef, *, timeout: float = 20.0) -> CallSession:
        self._ensure_enabled()
        await self.start()

        res = await self._signaling.request_call(peer, timeout=timeout)
        phone_call = self._extract_phone_call_obj(res)
        if phone_call is None:
            raise RuntimeError("phone.requestCall returned an unexpected response")

        call_id = self._extract_call_id(phone_call)
        access_hash = self._extract_access_hash(phone_call)
        session = CallSession(
            call_id=call_id,
            access_hash=access_hash,
            incoming=False,
            manager=self,
            state=CallState.OUTGOING_INIT,
        )
        self._sessions[int(call_id)] = session
        self._transition_if_allowed(session, CallState.CONNECTING)
        return session

    async def accept(self, session: CallSession, *, timeout: float = 20.0) -> None:
        self._ensure_enabled()
        await self._signaling.received_call(session.ref, timeout=timeout)
        await self._signaling.accept_call(session.ref, timeout=timeout)
        self._transition_if_allowed(session, CallState.CONNECTING)

    async def reject(self, session: CallSession, *, timeout: float = 20.0) -> None:
        self._ensure_enabled()
        self._transition_if_allowed(session, CallState.DISCONNECTING)
        await self._signaling.reject_call(session.ref, timeout=timeout)
        session.end_reason = CallEndReason.REJECTED
        self._transition_if_allowed(session, CallState.ENDED)

    async def hangup(self, session: CallSession, *, timeout: float = 20.0) -> None:
        self._ensure_enabled()
        self._transition_if_allowed(session, CallState.DISCONNECTING)
        await self._signaling.hangup_call(session.ref, timeout=timeout)
        session.end_reason = CallEndReason.LOCAL_HANGUP
        self._transition_if_allowed(session, CallState.ENDED)

    async def _updates_loop(self) -> None:
        assert self._updates_q is not None
        while True:
            update = await self._updates_q.get()
            try:
                await self._handle_update(update)
            except Exception as exc:  # noqa: BLE001
                session = self._session_from_update(update)
                if session is not None:
                    session._set_failed(exc, reason=CallEndReason.FAILED_PROTOCOL)
                continue

    async def _handle_update(self, update: Any) -> None:
        name = getattr(update, "TL_NAME", None)
        if name == "updatePhoneCall":
            await self._handle_update_phone_call(update)
            return

        if name == "updatePhoneCallSignalingData":
            call_id = getattr(update, "phone_call_id", None)
            data = getattr(update, "data", None)
            if isinstance(call_id, int) and isinstance(data, (bytes, bytearray)):
                session = self._sessions.get(int(call_id))
                if session is not None:
                    session._emit_signaling_data(bytes(data))
            return

    async def _handle_update_phone_call(self, update: Any) -> None:
        phone_call = getattr(update, "phone_call", None)
        if phone_call is None:
            return

        call_id = self._extract_call_id(phone_call)
        session = self._sessions.get(call_id)
        if session is None:
            session = CallSession(
                call_id=call_id,
                access_hash=self._extract_access_hash(phone_call),
                incoming=self._is_incoming_call(phone_call),
                manager=self,
            )
            self._sessions[call_id] = session

        new_access_hash = self._extract_access_hash(phone_call)
        if session.access_hash == 0 and new_access_hash != 0:
            session.access_hash = new_access_hash

        tl_name = getattr(phone_call, "TL_NAME", None)

        if tl_name in {"phoneCallRequested", "phoneCallWaiting"}:
            if session.incoming and session.state == CallState.IDLE:
                self._transition_if_allowed(session, CallState.RINGING_IN)
                await self._emit_incoming(session)
                return
            if not session.incoming and session.state in {CallState.IDLE, CallState.OUTGOING_INIT}:
                self._transition_if_allowed(session, CallState.CONNECTING)
                return

        if tl_name == "phoneCallAccepted":
            self._transition_if_allowed(session, CallState.CONNECTING)
            return

        if tl_name == "phoneCall":
            self._transition_if_allowed(session, CallState.IN_CALL)
            return

        if tl_name == "phoneCallDiscarded":
            if session.end_reason is None:
                session.end_reason = self._map_discard_reason(getattr(phone_call, "reason", None))
            self._transition_if_allowed(session, CallState.ENDED)
            return

        if tl_name == "phoneCallEmpty":
            if session.end_reason is None:
                session.end_reason = CallEndReason.REMOTE_HANGUP
            self._transition_if_allowed(session, CallState.ENDED)

    async def _emit_incoming(self, session: CallSession) -> None:
        for handler in list(self._incoming_handlers):
            try:
                result = handler(session)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                session._set_failed(exc)

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise RuntimeError("calls are disabled (enable_calls=False)")

    def _transition_if_allowed(self, session: CallSession, to_state: CallState) -> None:
        if can_transition(session.state, to_state):
            session._transition(to_state)

    def _session_from_update(self, update: Any) -> CallSession | None:
        name = getattr(update, "TL_NAME", None)
        if name == "updatePhoneCall":
            phone_call = getattr(update, "phone_call", None)
            call_id = getattr(phone_call, "id", None)
            if isinstance(call_id, int):
                return self._sessions.get(int(call_id))
        if name == "updatePhoneCallSignalingData":
            call_id = getattr(update, "phone_call_id", None)
            if isinstance(call_id, int):
                return self._sessions.get(int(call_id))
        return None

    def _extract_phone_call_obj(self, obj: Any) -> Any | None:
        phone_call = getattr(obj, "phone_call", None)
        if phone_call is not None:
            return phone_call
        updates = getattr(obj, "updates", None)
        if isinstance(updates, list):
            for item in updates:
                call_obj = getattr(item, "phone_call", None)
                if call_obj is not None:
                    return call_obj
        return None

    def _extract_call_id(self, phone_call: Any) -> int:
        call_id = getattr(phone_call, "id", None)
        if not isinstance(call_id, int):
            raise RuntimeError("phone call object missing int id")
        return int(call_id)

    def _extract_access_hash(self, phone_call: Any) -> int:
        access_hash = getattr(phone_call, "access_hash", None)
        return int(access_hash) if isinstance(access_hash, int) else 0

    def _is_incoming_call(self, phone_call: Any) -> bool:
        name = getattr(phone_call, "TL_NAME", None)
        if name not in {"phoneCallRequested", "phoneCallWaiting"}:
            return False

        self_id = self._raw.self_user_id
        participant_id = getattr(phone_call, "participant_id", None)
        admin_id = getattr(phone_call, "admin_id", None)

        if isinstance(self_id, int) and isinstance(participant_id, int):
            return int(participant_id) == int(self_id)
        if isinstance(self_id, int) and isinstance(admin_id, int):
            return int(admin_id) != int(self_id)

        return True

    def _map_discard_reason(self, reason: Any) -> CallEndReason:
        name = getattr(reason, "TL_NAME", None)
        if name == "phoneCallDiscardReasonBusy":
            return CallEndReason.BUSY
        if name == "phoneCallDiscardReasonMissed":
            return CallEndReason.MISSED
        if name == "phoneCallDiscardReasonHangup":
            return CallEndReason.REMOTE_HANGUP
        if name == "phoneCallDiscardReasonDisconnect":
            return CallEndReason.REMOTE_HANGUP
        return CallEndReason.REMOTE_HANGUP
