from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from telecraft.client.peers import PeerRef
from telecraft.tl.generated.types import (
    PhoneCallDiscardReasonMissed,
)

from .audio import NullAudioBackend, PortAudioBackend
from .crypto import CallCryptoContext, CallCryptoError, default_crypto_profile
from .errors import (
    CallProtocolError,
    CallsDisabledError,
    CallStateError,
    CallTimeoutError,
    SignalingDataError,
    exception_to_reason,
)
from .native_bridge import (
    ENDPOINT_FLAG_P2P,
    ENDPOINT_FLAG_RELAY,
    ENDPOINT_FLAG_TCP,
    ENDPOINT_FLAG_TURN,
    NativeBridge,
)
from .session import CallSession
from .signaling import CallSignalingAdapter
from .state import TERMINAL_CALL_STATES, CallEndReason, CallState, can_transition
from .types import CallConfig, CallProtocolSettings, PhoneCallRef, parse_call_config

IncomingHandler = Callable[[CallSession], Any]

_STATE_ORDER: dict[CallState, int] = {
    CallState.IDLE: 0,
    CallState.RINGING_IN: 1,
    CallState.OUTGOING_INIT: 1,
    CallState.CONNECTING: 2,
    CallState.IN_CALL: 3,
    CallState.DISCONNECTING: 4,
    CallState.ENDED: 5,
    CallState.FAILED: 5,
}

_PHONE_CALL_EVENT_ORDER: dict[str, int] = {
    "phoneCallRequested": 1,
    "phoneCallWaiting": 1,
    "phoneCallAccepted": 2,
    "phoneCall": 3,
    "phoneCallDiscarded": 4,
    "phoneCallEmpty": 4,
}

_DUPLICATE_SAFE_REMOTE_EVENTS: set[str] = {
    "phoneCallWaiting",
    "phoneCallAccepted",
    "phoneCall",
    "phoneCallDiscarded",
    "phoneCallEmpty",
}


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
    allow_p2p: bool = False
    force_udp_reflector: bool = True
    max_signaling_history: int = 128
    update_dedupe_window_seconds: float = 300.0
    call_config_refresh_seconds: float = 300.0
    structured_logs: bool = True
    library_version: str = "telecalls-signaling"
    bitrate_hint_kbps: int = 24
    audio_enabled: bool = False
    audio_backend: str = "null"
    audio_sample_rate: int = 48_000
    audio_channels: int = 1
    audio_frame_samples: int = 960

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
            allow_p2p=bool(mapping.get("allow_p2p", False)),
            force_udp_reflector=bool(mapping.get("force_udp_reflector", True)),
            max_signaling_history=int(mapping.get("max_signaling_history", 128)),
            update_dedupe_window_seconds=float(
                mapping.get("update_dedupe_window_seconds", 300.0)
            ),
            call_config_refresh_seconds=float(mapping.get("call_config_refresh_seconds", 300.0)),
            structured_logs=bool(mapping.get("structured_logs", True)),
            library_version=str(mapping.get("library_version", "telecalls-signaling")),
            bitrate_hint_kbps=int(mapping.get("bitrate_hint_kbps", 24)),
            audio_enabled=bool(mapping.get("audio_enabled", False)),
            audio_backend=str(mapping.get("audio_backend", "null")),
            audio_sample_rate=int(mapping.get("audio_sample_rate", 48_000)),
            audio_channels=int(mapping.get("audio_channels", 1)),
            audio_frame_samples=int(mapping.get("audio_frame_samples", 960)),
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
        self._logger = logging.getLogger(__name__)
        self._signaling = CallSignalingAdapter(raw)
        self._incoming_handlers: list[IncomingHandler] = []
        self._sessions: dict[int, CallSession] = {}
        self._gc_deadlines: dict[int, float] = {}
        self._dead_letters: list[str] = []
        self._seen_update_keys: dict[str, float] = {}
        self._seen_update_order: deque[tuple[str, float]] = deque()

        self._crypto_profile = default_crypto_profile()
        self._call_config: CallConfig | None = None
        self._call_config_last_refresh: float = 0.0
        self._protocol_settings = CallProtocolSettings(
            udp_p2p=self._config.allow_p2p,
            udp_reflector=self._config.force_udp_reflector,
            library_versions=(self._config.library_version,),
        )
        self._default_connect_timeout = 30.0
        self._default_request_timeout = 20.0

        self._native_bridge = NativeBridge(
            enabled=self._config.native_bridge_enabled,
            test_mode=self._config.native_test_mode,
            allow_p2p=self._config.allow_p2p,
            relay_preferred=self._config.force_udp_reflector,
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
        await self._refresh_call_config(force=True)
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
                protocol=self._protocol_settings,
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
        self._log(logging.INFO, "call.outgoing_init", session=session, peer=str(peer))
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
                protocol=self._protocol_settings,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        self._cancel_timer(session.call_id, "ringing")
        self._transition_if_allowed(session, CallState.CONNECTING)
        self._arm_timer(session, "connect", self._config.connect_timeout)
        self._ensure_native_session(session)
        self._log(logging.INFO, "call.accepted_local", session=session)

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
        self._log(logging.INFO, "call.rejected_local", session=session)

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
        self._log(logging.INFO, "call.hangup_local", session=session)

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
                protocol=self._protocol_settings,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )

    async def mute(self, session: CallSession, muted: bool) -> None:
        session.muted = bool(muted)
        if not self._native_bridge.set_mute(session.call_id, session.muted):
            self._log(logging.DEBUG, "call.mute_bridge_unavailable", session=session)

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
            self._prune_seen_update_keys()
            await self._refresh_call_config(force=False)

    async def _handle_update(self, update: Any) -> None:
        key = self._update_key(update)
        if key is not None and self._seen_update_key(key):
            self._log(logging.DEBUG, "update.duplicate", call_id=self._call_id_from_update(update))
            return

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
            digest = hashlib.sha256(payload).digest()
            if session._is_duplicate_signaling(
                digest,
                max_history=self._config.max_signaling_history,
            ):
                self._log(logging.DEBUG, "signaling.duplicate", session=session)
                return
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
            self._log(logging.DEBUG, "call.session_created", session=session)
        self._gc_deadlines.pop(call_id, None)

        new_access_hash = self._extract_access_hash(phone_call)
        if session.access_hash == 0 and new_access_hash != 0:
            session.access_hash = new_access_hash

        tl_name = str(getattr(phone_call, "TL_NAME", ""))
        if self._is_stale_phone_call_event(session, tl_name):
            self._log(logging.DEBUG, "call.event_stale", session=session, tl_name=tl_name)
            return
        if session.last_remote_event == tl_name and tl_name in _DUPLICATE_SAFE_REMOTE_EVENTS:
            self._log(logging.DEBUG, "call.event_idempotent", session=session, tl_name=tl_name)
            return
        session._remember_remote_event(tl_name)

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
            self._log(
                logging.INFO,
                "call.discarded_remote",
                session=session,
                reason=session.end_reason.value if session.end_reason else None,
            )
            return
        if tl_name == "phoneCallEmpty":
            if session.end_reason is None:
                session.end_reason = CallEndReason.REMOTE_HANGUP
            self._transition_if_allowed(session, CallState.ENDED)
            self._cleanup_session(session)
            self._log(logging.INFO, "call.empty_remote", session=session)
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
                protocol=self._protocol_settings,
                timeout=self._config.request_timeout,
            ),
        )
        self._ensure_native_session(session)
        attached = self._native_bridge.set_keys(
            session.call_id,
            material.auth_key,
            material.key_fingerprint,
        )
        if not attached:
            self._log(logging.WARNING, "call.native_key_attach_failed", session=session)
        session.native_key_attached = attached
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
            attached = self._native_bridge.set_keys(
                session.call_id,
                material.auth_key,
                material.key_fingerprint,
            )
            session.native_key_attached = attached

        self._ensure_native_session(session)
        self._native_bridge.set_remote_endpoints(
            session.call_id,
            self._extract_phone_connections(phone_call),
        )
        self._cancel_timer(session.call_id, "ringing")
        self._cancel_timer(session.call_id, "connect")
        self._transition_if_allowed(session, CallState.IN_CALL)
        self._start_audio_session(session)
        self._log(logging.INFO, "call.in_call", session=session)

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
                digest = hashlib.sha256(packet).digest()
                if session._is_duplicate_signaling(
                    digest,
                    max_history=self._config.max_signaling_history,
                ):
                    continue
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

    async def _refresh_call_config(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and (
            now - self._call_config_last_refresh
        ) < self._config.call_config_refresh_seconds:
            return

        try:
            raw_config = await self._signaling.get_call_config(timeout=self._config.request_timeout)
            parsed = parse_call_config(raw_config)
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "call.config_fetch_failed", error=repr(exc))
            return

        self._call_config = parsed
        self._call_config_last_refresh = now
        self._protocol_settings = self._merge_protocol_settings(parsed.protocol)
        self._native_bridge.set_allow_p2p(self._protocol_settings.udp_p2p)
        if self._config.connect_timeout == self._default_connect_timeout:
            if parsed.connect_timeout_seconds is not None:
                self._config.connect_timeout = parsed.connect_timeout_seconds
        if self._config.request_timeout == self._default_request_timeout:
            if parsed.packet_timeout_seconds is not None:
                self._config.request_timeout = parsed.packet_timeout_seconds
        self._log(
            logging.INFO,
            "call.config_loaded",
            protocol_layers=f"{self._protocol_settings.min_layer}-{self._protocol_settings.max_layer}",
            udp_p2p=self._protocol_settings.udp_p2p,
            udp_reflector=self._protocol_settings.udp_reflector,
        )

    def _merge_protocol_settings(self, settings: CallProtocolSettings) -> CallProtocolSettings:
        versions = tuple(settings.library_versions)
        if not versions:
            versions = (self._config.library_version,)
        elif self._config.library_version not in versions:
            versions = (self._config.library_version, *versions)

        udp_p2p = bool(settings.udp_p2p and self._config.allow_p2p)
        udp_reflector = bool(settings.udp_reflector or self._config.force_udp_reflector)

        min_layer = int(max(0, settings.min_layer))
        max_layer = int(max(min_layer, settings.max_layer))

        return CallProtocolSettings(
            udp_p2p=udp_p2p,
            udp_reflector=udp_reflector,
            min_layer=min_layer,
            max_layer=max_layer,
            library_versions=versions,
        )

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise CallsDisabledError("calls are disabled (enable_calls=False)")

    def _transition_if_allowed(self, session: CallSession, to_state: CallState) -> None:
        if session.state == to_state:
            return
        if can_transition(session.state, to_state):
            from_state = session.state
            session._transition(to_state)
            self._log(
                logging.DEBUG,
                "call.transition",
                session=session,
                from_state=from_state.value,
                to_state=to_state.value,
            )
            return
        self._log(
            logging.DEBUG,
            "call.transition_ignored",
            session=session,
            from_state=session.state.value,
            to_state=to_state.value,
        )

    def _resolve_call_ref(
        self,
        ref: PhoneCallRef | CallSession,
    ) -> tuple[PhoneCallRef, CallSession | None]:
        if isinstance(ref, CallSession):
            return ref.ref, ref
        session = self._sessions.get(int(ref.call_id))
        return ref, session

    def _session_from_update(self, update: Any) -> CallSession | None:
        call_id = self._call_id_from_update(update)
        if call_id is None:
            return None
        return self._sessions.get(call_id)

    def _call_id_from_update(self, update: Any) -> int | None:
        name = getattr(update, "TL_NAME", None)
        if name == "updatePhoneCall":
            phone_call = getattr(update, "phone_call", None)
            call_id = getattr(phone_call, "id", None)
            if isinstance(call_id, int):
                return int(call_id)
        if name == "updatePhoneCallSignalingData":
            call_id = getattr(update, "phone_call_id", None)
            if isinstance(call_id, int):
                return int(call_id)
        return None

    def _update_key(self, update: Any) -> str | None:
        name = getattr(update, "TL_NAME", None)
        if name == "updatePhoneCall":
            phone_call = getattr(update, "phone_call", None)
            tl_name = getattr(phone_call, "TL_NAME", "")
            call_id = getattr(phone_call, "id", None)
            if not isinstance(call_id, int):
                return None
            payload = "|".join(
                [
                    "phone",
                    str(call_id),
                    str(tl_name),
                    str(getattr(phone_call, "access_hash", "")),
                    str(getattr(phone_call, "date", "")),
                    str(getattr(phone_call, "start_date", "")),
                    str(getattr(phone_call, "duration", "")),
                    str(getattr(phone_call, "key_fingerprint", "")),
                ]
            )
            return payload
        if name == "updatePhoneCallSignalingData":
            call_id = getattr(update, "phone_call_id", None)
            data = getattr(update, "data", None)
            if not isinstance(call_id, int) or not isinstance(data, (bytes, bytearray)):
                return None
            digest = hashlib.sha256(bytes(data)).hexdigest()
            return f"signaling|{call_id}|{digest}"
        return None

    def _seen_update_key(self, key: str) -> bool:
        now = time.monotonic()
        if key in self._seen_update_keys:
            return True
        self._seen_update_keys[key] = now
        self._seen_update_order.append((key, now))
        return False

    def _prune_seen_update_keys(self) -> None:
        now = time.monotonic()
        window = self._config.update_dedupe_window_seconds
        while self._seen_update_order:
            key, ts = self._seen_update_order[0]
            if now - ts <= window and len(self._seen_update_keys) <= 8192:
                break
            self._seen_update_order.popleft()
            current = self._seen_update_keys.get(key)
            if current is None:
                continue
            if current == ts or (now - current) > window:
                self._seen_update_keys.pop(key, None)

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
            tl_name = str(getattr(item, "TL_NAME", ""))
            if not isinstance(ip, str) or not isinstance(port, int) or not isinstance(conn_id, int):
                continue

            flags = 0
            priority = 100
            if tl_name == "phoneConnection":
                flags |= ENDPOINT_FLAG_RELAY
                priority = 10
                if bool(getattr(item, "tcp", False)):
                    flags |= ENDPOINT_FLAG_TCP
                    priority = 15
            elif tl_name == "phoneConnectionWebrtc":
                if bool(getattr(item, "turn", False)):
                    flags |= ENDPOINT_FLAG_RELAY | ENDPOINT_FLAG_TURN
                    priority = 20
                if bool(getattr(item, "stun", False)):
                    flags |= ENDPOINT_FLAG_P2P
                    if priority > 30:
                        priority = 30

            peer_tag = getattr(item, "peer_tag", None)
            if not isinstance(peer_tag, (bytes, bytearray)):
                peer_tag = b""

            out.append(
                {
                    "id": int(conn_id),
                    "ip": ip,
                    "ipv6": ipv6 if isinstance(ipv6, str) else "",
                    "port": int(port),
                    "peer_tag": bytes(peer_tag),
                    "flags": int(flags),
                    "priority": int(priority),
                    "kind": tl_name,
                }
            )

        out.sort(key=lambda item: (int(item.get("priority", 100)), int(item.get("id", 0))))
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
        if name == "phoneCallDiscardReasonMigrateConferenceCall":
            return CallEndReason.REMOTE_HANGUP
        return CallEndReason.REMOTE_HANGUP

    def _is_stale_phone_call_event(self, session: CallSession, tl_name: str) -> bool:
        if session.state in TERMINAL_CALL_STATES:
            return tl_name not in {"phoneCallDiscarded", "phoneCallEmpty"}
        if tl_name not in _PHONE_CALL_EVENT_ORDER:
            return False
        current_rank = _STATE_ORDER.get(session.state, 0)
        event_rank = _PHONE_CALL_EVENT_ORDER[tl_name]
        return event_rank + 1 < current_rank

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
        self._log(logging.ERROR, "call.failed", session=session, error=repr(exc))
        session._set_failed(exc, reason=exception_to_reason(exc))
        self._cleanup_session(session)

    def _cleanup_session(self, session: CallSession) -> None:
        self._cancel_session_timers(session)
        self._stop_audio_session(session)
        self._native_bridge.stop(session.call_id)
        if session.crypto is not None:
            session.crypto.zeroize()
            session.crypto = None
        session._clear_signaling_history()
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
        self._native_bridge.set_bitrate_hint(
            session.call_id,
            self._config.bitrate_hint_kbps,
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

    def _log(
        self,
        level: int,
        event: str,
        *,
        session: CallSession | None = None,
        call_id: int | None = None,
        **fields: Any,
    ) -> None:
        if not self._config.structured_logs:
            return
        payload: dict[str, Any] = {"event": event}
        effective_call_id = call_id
        if session is not None:
            effective_call_id = session.call_id
            payload["state"] = session.state.value
            payload["incoming"] = session.incoming
        if effective_call_id is not None:
            payload["call_id"] = int(effective_call_id)
        payload.update(fields)
        self._logger.log(level, "calls: %s", payload)

    def _start_audio_session(self, session: CallSession) -> None:
        if not self._config.audio_enabled:
            return
        if session.audio_backend is not None:
            return

        backend_name = self._config.audio_backend.strip().lower()
        backend: Any
        if backend_name == "portaudio":
            try:
                backend = PortAudioBackend(
                    sample_rate=self._config.audio_sample_rate,
                    channels=self._config.audio_channels,
                    frame_size=self._config.audio_frame_samples,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(
                    logging.WARNING,
                    "call.audio_portaudio_unavailable",
                    session=session,
                    error=repr(exc),
                )
                backend = NullAudioBackend()
        else:
            backend = NullAudioBackend()

        silence = b"\x00" * (self._config.audio_frame_samples * self._config.audio_channels * 2)

        def _capture_cb(payload: bytes) -> None:
            self._native_bridge.push_audio_frame(session.call_id, payload)

        def _playback_source() -> bytes | None:
            frame = self._native_bridge.pull_audio_frame(
                session.call_id,
                frame_samples=self._config.audio_frame_samples * self._config.audio_channels,
            )
            return frame if frame is not None else silence

        try:
            backend.start_capture(_capture_cb)
            backend.start_playback(_playback_source)
            session.audio_backend = backend
            self._log(logging.INFO, "call.audio_started", session=session, backend=backend_name)
        except Exception as exc:  # noqa: BLE001
            self._log(logging.WARNING, "call.audio_start_failed", session=session, error=repr(exc))
            try:
                backend.stop()
            except Exception:
                pass
            session.audio_backend = None

    def _stop_audio_session(self, session: CallSession) -> None:
        backend = session.audio_backend
        if backend is None:
            return
        try:
            stop_fn = getattr(backend, "stop", None)
            if callable(stop_fn):
                stop_fn()
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "call.audio_stop_failed", session=session, error=repr(exc))
        finally:
            session.audio_backend = None
