from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from telecraft.client.peers import PeerRef
from telecraft.tl.generated.types import PhoneCallDiscardReasonMissed

from .crypto import CallCryptoContext, CallCryptoError, default_crypto_profile
from .errors import (
    CallProtocolError,
    CallsDisabledError,
    CallStateError,
    CallTimeoutError,
    SignalingDataError,
    exception_to_reason,
)
from .native_bridge import NativeBridge
from .session import CallSession
from .signaling import CallSignalingAdapter
from .state import TERMINAL_CALL_STATES, CallEndReason, CallState, can_transition
from .types import PhoneCallRef

IncomingHandler = Callable[[CallSession], Any]


@dataclass(slots=True)
class CallsManagerConfig:
    request_timeout: float = 20.0
    incoming_ring_timeout: float = 45.0
    connect_timeout: float = 30.0
    disconnect_timeout: float = 5.0
    session_ttl_seconds: float = 120.0
    max_retries: int = 2
    retry_backoff: float = 0.35
    native_bridge_enabled: bool = False
    native_test_mode: bool = True

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CallsManagerConfig:
        return cls(
            request_timeout=float(mapping.get("request_timeout", 20.0)),
            incoming_ring_timeout=float(mapping.get("incoming_ring_timeout", 45.0)),
            connect_timeout=float(mapping.get("connect_timeout", 30.0)),
            disconnect_timeout=float(mapping.get("disconnect_timeout", 5.0)),
            session_ttl_seconds=float(mapping.get("session_ttl_seconds", 120.0)),
            max_retries=int(mapping.get("max_retries", 2)),
            retry_backoff=float(mapping.get("retry_backoff", 0.35)),
            native_bridge_enabled=bool(mapping.get("native_bridge_enabled", False)),
            native_test_mode=bool(mapping.get("native_test_mode", True)),
        )


class CallsManager:
    def __init__(
        self,
        *,
        raw: Any,
        enabled: bool = False,
        config: CallsManagerConfig | Mapping[str, Any] | None = None,
    ) -> None:
        self._raw = raw
        self._enabled = enabled
        self._config = (
            config
            if isinstance(config, CallsManagerConfig)
            else CallsManagerConfig.from_mapping(config or {})
        )
        self._signaling = CallSignalingAdapter(raw)
        self._incoming_handlers: list[IncomingHandler] = []
        self._sessions: dict[int, CallSession] = {}
        self._gc_deadlines: dict[int, float] = {}
        self._dead_letters: list[str] = []

        self._crypto_profile = default_crypto_profile()
        self._native_bridge = NativeBridge(
            enabled=self._config.native_bridge_enabled,
            test_mode=self._config.native_test_mode,
        )

        self._updates_q: asyncio.Queue[Any] | None = None
        self._updates_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._timer_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def dead_letters(self) -> tuple[str, ...]:
        return tuple(self._dead_letters)

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def on_incoming(self, handler: IncomingHandler) -> None:
        self._incoming_handlers.append(handler)

    def get(self, call_id: int) -> CallSession | None:
        self._collect_expired_sessions()
        return self._sessions.get(int(call_id))

    async def start(self) -> None:
        if not self._enabled:
            return
        await self._raw.start_updates()
        if self._updates_task is not None:
            return
        self._updates_q = self._raw.subscribe_updates(maxsize=2048)
        self._updates_task = asyncio.create_task(self._updates_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        if self._updates_task is not None:
            self._updates_task.cancel()
            try:
                await self._updates_task
            except asyncio.CancelledError:
                pass
            self._updates_task = None

        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None

        if self._updates_q is not None:
            self._raw.unsubscribe_updates(self._updates_q)
        self._updates_q = None

        for key in list(self._timer_tasks):
            self._cancel_timer(*key)

        for session in list(self._sessions.values()):
            self._cleanup_session(session)
        self._sessions.clear()
        self._gc_deadlines.clear()

    async def call(
        self,
        peer: PeerRef,
        *,
        video: bool = False,
        timeout: float = 20.0,
    ) -> CallSession:
        self._ensure_enabled()
        await self.start()

        crypto = CallCryptoContext.new_outgoing(self._crypto_profile)
        res = await self._retry(
            "phone.requestCall",
            lambda: self._signaling.request_call(
                peer,
                g_a_hash=crypto.g_a_hash,
                video=video,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        phone_call = self._extract_phone_call_obj(res)
        if phone_call is None:
            raise CallProtocolError("phone.requestCall returned an unexpected response")

        call_id = self._extract_call_id(phone_call)
        access_hash = self._extract_access_hash(phone_call)
        session = self._sessions.get(call_id)
        if session is None:
            session = CallSession(
                call_id=call_id,
                access_hash=access_hash,
                incoming=False,
                manager=self,
                video=video,
                state=CallState.OUTGOING_INIT,
                crypto=crypto,
            )
            self._sessions[call_id] = session
        else:
            session.video = video
            if session.crypto is None:
                session.crypto = crypto
            if can_transition(session.state, CallState.OUTGOING_INIT):
                session._transition(CallState.OUTGOING_INIT)

        self._transition_if_allowed(session, CallState.CONNECTING)
        self._arm_timer(session, "connect", self._config.connect_timeout)
        self._ensure_native_session(session)
        return session

    async def accept(self, session: CallSession, *, timeout: float = 20.0) -> None:
        self._ensure_enabled()
        if session.state not in {CallState.RINGING_IN, CallState.CONNECTING}:
            raise CallStateError(f"accept not allowed in state {session.state.value}")
        if session.crypto is None:
            raise CallProtocolError("cannot accept without incoming crypto context")
        crypto = session.crypto

        await self._retry_for_session(
            session,
            "received_call",
            lambda: self._signaling.received_call(
                session.ref,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        await self._retry_for_session(
            session,
            "accept_call",
            lambda: self._signaling.accept_call(
                session.ref,
                g_b=crypto.g_b,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        self._cancel_timer(session.call_id, "ringing")
        self._transition_if_allowed(session, CallState.CONNECTING)
        self._arm_timer(session, "connect", self._config.connect_timeout)
        self._ensure_native_session(session)

    async def reject(self, session: CallSession, *, timeout: float = 20.0) -> None:
        self._ensure_enabled()
        if session.state in TERMINAL_CALL_STATES:
            return
        self._transition_if_allowed(session, CallState.DISCONNECTING)
        await self._retry_for_session(
            session,
            "reject_call",
            lambda: self._signaling.reject_call(
                session.ref,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        session.end_reason = CallEndReason.REJECTED
        self._transition_if_allowed(session, CallState.ENDED)
        self._cleanup_session(session)

    async def hangup(self, session: CallSession, *, timeout: float = 20.0) -> None:
        self._ensure_enabled()
        if session.state in TERMINAL_CALL_STATES:
            return
        self._transition_if_allowed(session, CallState.DISCONNECTING)
        await self._retry_for_session(
            session,
            "hangup_call",
            lambda: self._signaling.hangup_call(
                session.ref,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        session.end_reason = CallEndReason.LOCAL_HANGUP
        self._transition_if_allowed(session, CallState.ENDED)
        self._cleanup_session(session)

    async def send_signaling_data(
        self,
        call_ref: PhoneCallRef | CallSession,
        data: bytes,
        *,
        timeout: float = 20.0,
    ) -> Any:
        self._ensure_enabled()
        ref, _session = self._resolve_call_ref(call_ref)
        if not isinstance(data, (bytes, bytearray)):
            raise SignalingDataError("send_signaling_data expects bytes")
        payload = bytes(data)
        if not payload:
            raise SignalingDataError("send_signaling_data payload cannot be empty")
        if len(payload) > 16 * 1024:
            raise SignalingDataError("send_signaling_data payload too large")

        return await self._retry(
            "phone.sendSignalingData",
            lambda: self._signaling.send_signaling_data(
                ref,
                payload,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )

    async def confirm(
        self,
        call_ref: PhoneCallRef | CallSession,
        *,
        key_fingerprint: int,
        g_a: bytes = b"",
        timeout: float = 20.0,
    ) -> Any:
        self._ensure_enabled()
        ref, session = self._resolve_call_ref(call_ref)
        if session is not None and session.crypto is not None:
            if not session.crypto.verify_fingerprint(int(key_fingerprint)):
                raise CallProtocolError("confirm key_fingerprint does not match local key material")

        return await self._retry(
            "phone.confirmCall",
            lambda: self._signaling.confirm_call(
                ref,
                key_fingerprint=int(key_fingerprint),
                g_a=bytes(g_a),
                timeout=min(timeout, self._config.request_timeout),
            ),
        )

    async def mute(self, session: CallSession, muted: bool) -> None:
        _ = (session, muted)
        # Placeholder for media phase.
        return

    async def _updates_loop(self) -> None:
        assert self._updates_q is not None
        while True:
            update = await self._updates_q.get()
            try:
                await self._handle_update(update)
            except Exception as exc:  # noqa: BLE001
                session = self._session_from_update(update)
                if session is not None:
                    self._fail_session(session, exc)
                else:
                    self._record_dead_letter(f"unmatched_update:{type(update).__name__}:{exc}")

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self._collect_expired_sessions()
            await self._flush_native_signaling()
            self._refresh_native_stats()

    async def _handle_update(self, update: Any) -> None:
        name = getattr(update, "TL_NAME", None)
        if name == "updatePhoneCall":
            await self._handle_update_phone_call(update)
            return

        if name == "updatePhoneCallSignalingData":
            call_id = getattr(update, "phone_call_id", None)
            data = getattr(update, "data", None)
            if not isinstance(call_id, int) or not isinstance(data, (bytes, bytearray)):
                raise CallProtocolError("malformed updatePhoneCallSignalingData payload")
            session = self._sessions.get(int(call_id))
            if session is None:
                self._record_dead_letter(f"signaling_without_session:{call_id}")
                return
            payload = bytes(data)
            session._emit_signaling_data(payload)
            self._native_bridge.push_signaling(session.call_id, payload)
            return

    async def _handle_update_phone_call(self, update: Any) -> None:
        phone_call = getattr(update, "phone_call", None)
        if phone_call is None:
            raise CallProtocolError("updatePhoneCall missing phone_call object")

        call_id = self._extract_call_id(phone_call)
        session = self._sessions.get(call_id)
        if session is None:
            session = CallSession(
                call_id=call_id,
                access_hash=self._extract_access_hash(phone_call),
                incoming=self._is_incoming_call(phone_call),
                manager=self,
                video=bool(getattr(phone_call, "video", False)),
            )
            self._sessions[call_id] = session
        self._gc_deadlines.pop(call_id, None)

        new_access_hash = self._extract_access_hash(phone_call)
        if session.access_hash == 0 and new_access_hash != 0:
            session.access_hash = new_access_hash

        tl_name = getattr(phone_call, "TL_NAME", None)
        if tl_name == "phoneCallRequested":
            await self._handle_requested(session, phone_call)
            return
        if tl_name == "phoneCallWaiting":
            await self._handle_waiting(session)
            return
        if tl_name == "phoneCallAccepted":
            await self._handle_accepted(session, phone_call)
            return
        if tl_name == "phoneCall":
            await self._handle_established(session, phone_call)
            return
        if tl_name == "phoneCallDiscarded":
            if session.end_reason is None:
                session.end_reason = self._map_discard_reason(getattr(phone_call, "reason", None))
            self._transition_if_allowed(session, CallState.ENDED)
            self._cleanup_session(session)
            return
        if tl_name == "phoneCallEmpty":
            if session.end_reason is None:
                session.end_reason = CallEndReason.REMOTE_HANGUP
            self._transition_if_allowed(session, CallState.ENDED)
            self._cleanup_session(session)
            return

        raise CallProtocolError(f"unsupported phone call payload: {tl_name!r}")

    async def _handle_requested(self, session: CallSession, phone_call: Any) -> None:
        g_a_hash = getattr(phone_call, "g_a_hash", None)
        if not isinstance(g_a_hash, (bytes, bytearray)):
            raise CallProtocolError("phoneCallRequested missing g_a_hash bytes")

        if session.crypto is None:
            session.crypto = CallCryptoContext.new_incoming(bytes(g_a_hash), self._crypto_profile)
        if session.state == CallState.IDLE:
            self._transition_if_allowed(session, CallState.RINGING_IN)
            self._arm_timer(session, "ringing", self._config.incoming_ring_timeout)
            await self._emit_incoming(session)

    async def _handle_waiting(self, session: CallSession) -> None:
        if not session.incoming and session.state in {CallState.IDLE, CallState.OUTGOING_INIT}:
            self._transition_if_allowed(session, CallState.CONNECTING)
            self._arm_timer(session, "connect", self._config.connect_timeout)

    async def _handle_accepted(self, session: CallSession, phone_call: Any) -> None:
        g_b = getattr(phone_call, "g_b", None)
        if session.crypto is None or session.crypto.role != "outgoing":
            raise CallProtocolError("phoneCallAccepted without outgoing crypto context")
        crypto = session.crypto
        if not isinstance(g_b, (bytes, bytearray)):
            raise CallProtocolError("phoneCallAccepted missing g_b bytes")

        material = crypto.apply_remote_g_b(bytes(g_b))
        await self._retry_for_session(
            session,
            "confirm_call",
            lambda: self._signaling.confirm_call(
                session.ref,
                g_a=crypto.g_a,
                key_fingerprint=material.key_fingerprint,
                timeout=self._config.request_timeout,
            ),
        )
        self._ensure_native_session(session)
        self._native_bridge.set_keys(
            session.call_id,
            material.auth_key,
            material.key_fingerprint,
        )
        self._transition_if_allowed(session, CallState.CONNECTING)
        self._arm_timer(session, "connect", self._config.connect_timeout)

    async def _handle_established(self, session: CallSession, phone_call: Any) -> None:
        g_a_or_b = getattr(phone_call, "g_a_or_b", None)
        key_fingerprint = getattr(phone_call, "key_fingerprint", None)
        if not isinstance(key_fingerprint, int):
            raise CallProtocolError("phoneCall missing key_fingerprint")

        if session.crypto is not None:
            if not isinstance(g_a_or_b, (bytes, bytearray)):
                raise CallProtocolError("phoneCall missing g_a_or_b bytes")
            material = session.crypto.apply_final_public(
                bytes(g_a_or_b),
                expected_fingerprint=int(key_fingerprint),
            )
            self._ensure_native_session(session)
            self._native_bridge.set_keys(
                session.call_id,
                material.auth_key,
                material.key_fingerprint,
            )

        self._ensure_native_session(session)
        self._native_bridge.set_remote_endpoints(
            session.call_id,
            self._extract_phone_connections(phone_call),
        )
        self._cancel_timer(session.call_id, "ringing")
        self._cancel_timer(session.call_id, "connect")
        self._transition_if_allowed(session, CallState.IN_CALL)

    async def _emit_incoming(self, session: CallSession) -> None:
        for handler in list(self._incoming_handlers):
            try:
                result = handler(session)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                self._fail_session(session, exc)

    async def _flush_native_signaling(self) -> None:
        for session in list(self._sessions.values()):
            if session.state in TERMINAL_CALL_STATES:
                continue
            while True:
                packet = self._native_bridge.pull_signaling(session.call_id)
                if packet is None:
                    break
                try:
                    await self._signaling.send_signaling_data(
                        session.ref,
                        packet,
                        timeout=self._config.request_timeout,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._fail_session(session, exc)
                    break

    def _refresh_native_stats(self) -> None:
        for session in list(self._sessions.values()):
            if session.state not in {CallState.CONNECTING, CallState.IN_CALL}:
                continue
            stats = self._native_bridge.poll_stats(session.call_id)
            if stats is not None:
                session._set_stats(stats)

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise CallsDisabledError("calls are disabled (enable_calls=False)")

    def _transition_if_allowed(self, session: CallSession, to_state: CallState) -> None:
        if can_transition(session.state, to_state):
            session._transition(to_state)

    def _resolve_call_ref(
        self,
        ref: PhoneCallRef | CallSession,
    ) -> tuple[PhoneCallRef, CallSession | None]:
        if isinstance(ref, CallSession):
            return ref.ref, ref
        session = self._sessions.get(int(ref.call_id))
        return ref, session

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
                nested = getattr(item, "phone_call", None)
                if nested is not None:
                    return nested
        return None

    def _extract_call_id(self, phone_call: Any) -> int:
        call_id = getattr(phone_call, "id", None)
        if not isinstance(call_id, int):
            raise CallProtocolError("phone call object missing int id")
        return int(call_id)

    def _extract_access_hash(self, phone_call: Any) -> int:
        access_hash = getattr(phone_call, "access_hash", None)
        return int(access_hash) if isinstance(access_hash, int) else 0

    def _extract_phone_connections(self, phone_call: Any) -> list[dict[str, str | int | bytes]]:
        out: list[dict[str, str | int | bytes]] = []
        connections = getattr(phone_call, "connections", None)
        if not isinstance(connections, list):
            return out
        for item in connections:
            ip = getattr(item, "ip", None)
            ipv6 = getattr(item, "ipv6", None)
            port = getattr(item, "port", None)
            conn_id = getattr(item, "id", None)
            peer_tag = getattr(item, "peer_tag", None)
            if isinstance(ip, str) and isinstance(port, int) and isinstance(conn_id, int):
                out.append(
                    {
                        "id": int(conn_id),
                        "ip": ip,
                        "ipv6": ipv6 if isinstance(ipv6, str) else "",
                        "port": int(port),
                        "peer_tag": (
                            bytes(peer_tag)
                            if isinstance(peer_tag, (bytes, bytearray))
                            else b""
                        ),
                    }
                )
        return out

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

    def _arm_timer(self, session: CallSession, timer_name: str, timeout: float) -> None:
        key = (session.call_id, timer_name)
        self._cancel_timer(*key)
        session._set_timer_deadline(timer_name, timeout=timeout)

        async def _timer() -> None:
            try:
                await asyncio.sleep(timeout)
            except asyncio.CancelledError:
                return
            session._clear_timer_deadline(timer_name)
            if session.state in TERMINAL_CALL_STATES:
                return
            await self._on_timer_expired(session, timer_name)

        self._timer_tasks[key] = asyncio.create_task(_timer())

    def _cancel_timer(self, call_id: int, timer_name: str) -> None:
        key = (int(call_id), str(timer_name))
        task = self._timer_tasks.pop(key, None)
        if task is not None:
            task.cancel()
        session = self._sessions.get(int(call_id))
        if session is not None:
            session._clear_timer_deadline(timer_name)

    async def _on_timer_expired(self, session: CallSession, timer_name: str) -> None:
        try:
            if timer_name == "ringing":
                await self._signaling.discard_call(
                    session.ref,
                    reason=PhoneCallDiscardReasonMissed(),
                    timeout=self._config.request_timeout,
                )
            elif timer_name == "connect":
                await self._signaling.hangup_call(
                    session.ref,
                    timeout=self._config.request_timeout,
                )
            elif timer_name == "disconnect":
                await self._signaling.hangup_call(
                    session.ref,
                    timeout=self._config.request_timeout,
                )
        except Exception:
            pass
        self._fail_session(session, CallTimeoutError(f"{timer_name} timer expired"))

    def _cancel_session_timers(self, session: CallSession) -> None:
        for key in [k for k in self._timer_tasks if k[0] == session.call_id]:
            self._cancel_timer(*key)
        session.timers.clear()

    def _fail_session(self, session: CallSession, exc: Exception) -> None:
        if isinstance(exc, CallCryptoError):
            exc = CallProtocolError(str(exc))
        session._set_failed(exc, reason=exception_to_reason(exc))
        self._cleanup_session(session)

    def _cleanup_session(self, session: CallSession) -> None:
        self._cancel_session_timers(session)
        self._native_bridge.stop(session.call_id)
        if session.crypto is not None:
            session.crypto.zeroize()
            session.crypto = None
        self._schedule_gc(session.call_id)

    def _schedule_gc(self, call_id: int) -> None:
        self._gc_deadlines[int(call_id)] = time.monotonic() + self._config.session_ttl_seconds

    def _collect_expired_sessions(self) -> None:
        now = time.monotonic()
        expired = [call_id for call_id, deadline in self._gc_deadlines.items() if deadline <= now]
        for call_id in expired:
            self._gc_deadlines.pop(call_id, None)
            self._sessions.pop(call_id, None)
            self._native_bridge.stop(call_id)

    def _ensure_native_session(self, session: CallSession) -> None:
        self._native_bridge.ensure_session(
            call_id=session.call_id,
            incoming=session.incoming,
            video=session.video,
        )

    async def _retry_for_session(
        self,
        session: CallSession,
        key: str,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            return await self._retry(key, call)
        except Exception:
            session.retries[key] = session.retries.get(key, 0) + 1
            raise

    async def _retry(self, key: str, call: Callable[[], Awaitable[Any]]) -> Any:
        last_exc: Exception | None = None
        attempts = max(0, self._config.max_retries) + 1
        for attempt in range(1, attempts + 1):
            try:
                return await call()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= attempts:
                    break
                delay = self._config.retry_backoff * float(attempt)
                await asyncio.sleep(delay)
        if last_exc is None:
            raise RuntimeError(f"{key} failed without exception")
        raise last_exc

    def _record_dead_letter(self, value: str) -> None:
        self._dead_letters.append(value)
        if len(self._dead_letters) > 256:
            self._dead_letters.pop(0)
