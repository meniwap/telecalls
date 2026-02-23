from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telecraft.client.peers import PeerRef
from telecraft.mtproto.rpc.sender import RpcErrorException
from telecraft.tl.generated.types import (
    PhoneCallDiscardReasonMissed,
)

from .audio import NullAudioBackend, PortAudioBackend
from .crypto import CallCryptoContext, CallCryptoError, CallCryptoProfile, default_crypto_profile
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
    NATIVE_BACKEND_AUTO,
    NATIVE_BACKEND_LEGACY,
    NATIVE_BACKEND_TGCALLS,
    TC_ENGINE_STATE_ESTABLISHED,
    TC_ENGINE_STATE_FAILED,
    NativeBridge,
)
from .session import CallSession
from .signaling import CallSignalingAdapter
from .state import TERMINAL_CALL_STATES, CallEndReason, CallState, can_transition
from .stats import CallStats
from .types import CallConfig, CallProtocolSettings, PhoneCallRef, parse_call_config
from .visual_key import derive_call_emojis, fingerprint_to_hex

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

_SUPPORTED_INTEROP_PROFILES: set[str] = {"tgvoip_v9"}
_DEFAULT_LIBRARY_VERSIONS: tuple[str, ...] = ("9.0.0",)
_AGENT_DEBUG_LOG_PATH = Path("/Users/meniwap/satla/.cursor/debug.log")
_SUPPORTED_NATIVE_BACKENDS: set[str] = {
    NATIVE_BACKEND_LEGACY,
    NATIVE_BACKEND_TGCALLS,
    NATIVE_BACKEND_AUTO,
}


def _agent_debug_log(
    *,
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    # region agent log
    payload: dict[str, Any] = {
        "id": f"py_{int(time.time() * 1000)}_{hypothesis_id}",
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "data": dict(data),
    }
    try:
        _AGENT_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AGENT_DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception:
        pass
    # endregion


def _parse_library_versions(mapping: Mapping[str, Any]) -> tuple[str, ...]:
    value = mapping.get("library_versions")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parsed = tuple(str(item).strip() for item in value if str(item).strip())
        if parsed:
            return parsed
    single = mapping.get("library_version")
    if single is not None:
        normalized = str(single).strip()
        if normalized:
            return (normalized,)
    return _DEFAULT_LIBRARY_VERSIONS


def _normalize_native_backend(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in _SUPPORTED_NATIVE_BACKENDS:
        return normalized
    return NATIVE_BACKEND_LEGACY


@dataclass(slots=True)
class CallsManagerConfig:
    request_timeout: float = 20.0
    incoming_ring_timeout: float = 45.0
    connect_timeout: float = 20.0
    media_ready_timeout: float = 8.0
    disconnect_timeout: float = 5.0
    session_ttl_seconds: float = 120.0
    max_retries: int = 2
    retry_backoff: float = 0.35
    native_bridge_enabled: bool = False
    native_test_mode: bool = False
    native_backend: str = NATIVE_BACKEND_LEGACY
    allow_p2p: bool = False
    force_udp_reflector: bool = True
    max_signaling_history: int = 128
    update_dedupe_window_seconds: float = 300.0
    call_config_refresh_seconds: float = 300.0
    structured_logs: bool = True
    protocol_min_layer: int = 65
    protocol_max_layer: int = 92
    library_versions: tuple[str, ...] = _DEFAULT_LIBRARY_VERSIONS
    bitrate_hint_kbps: int = 24
    audio_enabled: bool = False
    audio_backend: str = "null"
    audio_sample_rate: int = 48_000
    audio_channels: int = 1
    audio_frame_samples: int = 960
    interop_profile: str = "tgvoip_v9"
    network_type: str = "wifi"
    signaling_dump_path: str | None = None
    fail_on_protocol_mismatch: bool = True
    strict_media_ready: bool = True
    disable_soft_media_ready: bool = True
    native_decrypt_fallback_soft_ready: bool = False
    degraded_in_call_mode: bool = False
    native_backend_oracle_compare: bool = False
    udp_media_required_for_in_call: bool = True

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CallsManagerConfig:
        return cls(
            request_timeout=float(mapping.get("request_timeout", 20.0)),
            incoming_ring_timeout=float(mapping.get("incoming_ring_timeout", 45.0)),
            connect_timeout=float(mapping.get("connect_timeout", 20.0)),
            media_ready_timeout=float(mapping.get("media_ready_timeout", 8.0)),
            disconnect_timeout=float(mapping.get("disconnect_timeout", 5.0)),
            session_ttl_seconds=float(mapping.get("session_ttl_seconds", 120.0)),
            max_retries=int(mapping.get("max_retries", 2)),
            retry_backoff=float(mapping.get("retry_backoff", 0.35)),
            native_bridge_enabled=bool(mapping.get("native_bridge_enabled", False)),
            native_test_mode=bool(mapping.get("native_test_mode", False)),
            native_backend=str(mapping.get("native_backend", NATIVE_BACKEND_LEGACY)),
            allow_p2p=bool(mapping.get("allow_p2p", False)),
            force_udp_reflector=bool(mapping.get("force_udp_reflector", True)),
            max_signaling_history=int(mapping.get("max_signaling_history", 128)),
            update_dedupe_window_seconds=float(
                mapping.get("update_dedupe_window_seconds", 300.0)
            ),
            call_config_refresh_seconds=float(mapping.get("call_config_refresh_seconds", 300.0)),
            structured_logs=bool(mapping.get("structured_logs", True)),
            protocol_min_layer=int(mapping.get("protocol_min_layer", 65)),
            protocol_max_layer=int(mapping.get("protocol_max_layer", 92)),
            library_versions=_parse_library_versions(mapping),
            bitrate_hint_kbps=int(mapping.get("bitrate_hint_kbps", 24)),
            audio_enabled=bool(mapping.get("audio_enabled", False)),
            audio_backend=str(mapping.get("audio_backend", "null")),
            audio_sample_rate=int(mapping.get("audio_sample_rate", 48_000)),
            audio_channels=int(mapping.get("audio_channels", 1)),
            audio_frame_samples=int(mapping.get("audio_frame_samples", 960)),
            interop_profile=str(mapping.get("interop_profile", "tgvoip_v9")),
            network_type=str(mapping.get("network_type", "wifi")),
            signaling_dump_path=(
                str(mapping["signaling_dump_path"])
                if mapping.get("signaling_dump_path") is not None
                else None
            ),
            fail_on_protocol_mismatch=bool(mapping.get("fail_on_protocol_mismatch", True)),
            strict_media_ready=bool(mapping.get("strict_media_ready", True)),
            disable_soft_media_ready=bool(mapping.get("disable_soft_media_ready", True)),
            native_decrypt_fallback_soft_ready=bool(
                mapping.get("native_decrypt_fallback_soft_ready", False)
            ),
            degraded_in_call_mode=bool(mapping.get("degraded_in_call_mode", False)),
            native_backend_oracle_compare=bool(
                mapping.get("native_backend_oracle_compare", False)
            ),
            udp_media_required_for_in_call=bool(
                mapping.get("udp_media_required_for_in_call", True)
            ),
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
        self._config.native_backend = _normalize_native_backend(self._config.native_backend)
        if self._config.interop_profile not in _SUPPORTED_INTEROP_PROFILES:
            raise CallProtocolError(
                f"unsupported interop_profile={self._config.interop_profile!r}; "
                f"supported={sorted(_SUPPORTED_INTEROP_PROFILES)}"
            )
        self._logger = logging.getLogger(__name__)
        self._signaling = CallSignalingAdapter(raw)
        self._incoming_handlers: list[IncomingHandler] = []
        self._sessions: dict[int, CallSession] = {}
        self._gc_deadlines: dict[int, float] = {}
        self._dead_letters: list[str] = []
        self._seen_update_keys: dict[str, float] = {}
        self._seen_update_order: deque[tuple[str, float]] = deque()
        self._agent_prev_in_signaling: dict[int, bytes] = {}

        self._crypto_profile = default_crypto_profile()
        self._dh_config_version = 0
        self._dh_profile_last_refresh: float = 0.0
        self._call_config: CallConfig | None = None
        self._call_config_last_refresh: float = 0.0
        self._protocol_settings = CallProtocolSettings(
            udp_p2p=self._config.allow_p2p,
            udp_reflector=self._config.force_udp_reflector,
            min_layer=max(0, int(self._config.protocol_min_layer)),
            max_layer=max(
                max(0, int(self._config.protocol_min_layer)),
                int(self._config.protocol_max_layer),
            ),
            library_versions=self._config.library_versions,
        )
        self._active_library_version_index = 0
        self._default_connect_timeout = 20.0
        self._default_request_timeout = 20.0

        self._native_bridge = NativeBridge(
            enabled=self._config.native_bridge_enabled,
            test_mode=self._config.native_test_mode,
            allow_p2p=self._config.allow_p2p,
            relay_preferred=self._config.force_udp_reflector,
            backend=self._config.native_backend,
        )
        self._log(
            logging.INFO,
            "call.native_backend_selected",
            backend_preference=self._config.native_backend,
            backend_selected=str(getattr(self._native_bridge, "backend", "legacy")),
            supports_rtc_servers=bool(
                getattr(self._native_bridge, "supports_rtc_servers", False)
            ),
        )
        self._native_audio_managed = (
            str(getattr(self._native_bridge, "backend", "legacy")) == NATIVE_BACKEND_TGCALLS
        )
        if self._native_audio_managed:
            self._log(
                logging.INFO,
                "call.native_audio_managed",
                backend="tgcalls",
                python_audio_enabled=bool(self._config.audio_enabled),
            )
        self._native_bridge.set_network_type(self._config.network_type)
        self._native_bridge.set_protocol_config(
            protocol_version=9,
            min_protocol_version=3,
            min_layer=self._protocol_settings.min_layer,
            max_layer=self._protocol_settings.max_layer,
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
        await self._refresh_dh_profile(force=True)
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
        session.server_ready = False
        session.media_ready = False
        session.protocol_negotiated = False
        session.disconnect_reason_raw = None
        session.e2e_key_fingerprint_hex = None
        session.e2e_emojis = None
        session.ringback_stopped_at = None
        session.ring_tone_stopped_at = None
        session.ring_tone_active = True
        session.in_call_gate_satisfied = False
        session.in_call_gate_block_reason = None
        session.native_handshake_block_reason = None
        session.last_signaling_blob_len = None
        session.repeated_signaling_blob_count = 0
        session.selected_relay_endpoint_id = None
        session.selected_relay_endpoint_kind = None
        session.signaling_rx_count = 0
        session.signaling_tx_count = 0
        session.native_decrypt_failures = 0
        session.audio_capture_frames = 0
        session.audio_push_ok = 0
        session.audio_push_fail = 0

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
        self._mark_ring_tone_active(session, source="incoming_accept")
        self._stop_call_tones(session, reason="incoming_accept_start")

        await self._retry_for_session(
            session,
            "received_call",
            lambda: self._signaling.received_call(
                session.ref,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        accept_result = await self._retry_for_session(
            session,
            "accept_call",
            lambda: self._signaling.accept_call(
                session.ref,
                g_b=crypto.g_b,
                protocol=self._protocol_settings,
                timeout=min(timeout, self._config.request_timeout),
            ),
        )
        accept_phone_call = self._extract_phone_call_obj(accept_result)
        if accept_phone_call is not None:
            try:
                await self._dispatch_phone_call_event(session, accept_phone_call)
            except CallProtocolError as exc:
                self._log(
                    logging.DEBUG,
                    "call.accept_response_ignored",
                    session=session,
                    error=repr(exc),
                    tl_name=str(getattr(accept_phone_call, "TL_NAME", "")),
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
        self._stop_call_tones(session, reason="reject_local")
        try:
            await self._retry_for_session(
                session,
                "reject_call",
                lambda: self._signaling.reject_call(
                    session.ref,
                    timeout=min(timeout, self._config.request_timeout),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            if not self._is_call_not_active_error(exc):
                raise
            self._log(
                logging.DEBUG,
                "call.reject_already_inactive",
                session=session,
                error=repr(exc),
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
        self._stop_call_tones(session, reason="hangup_local")
        try:
            await self._retry_for_session(
                session,
                "hangup_call",
                lambda: self._signaling.hangup_call(
                    session.ref,
                    timeout=min(timeout, self._config.request_timeout),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            if not self._is_call_not_active_error(exc):
                raise
            self._log(
                logging.DEBUG,
                "call.hangup_already_inactive",
                session=session,
                error=repr(exc),
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
            interval = 0.5
            if any(
                session.state == CallState.CONNECTING and session.server_ready
                for session in self._sessions.values()
            ):
                interval = 0.05
            await asyncio.sleep(interval)
            self._collect_expired_sessions()
            for session in list(self._sessions.values()):
                if session.state in TERMINAL_CALL_STATES:
                    continue
                if session.native_key_attached:
                    self._flush_pending_signaling_to_native(session)
            await self._flush_native_signaling()
            self._refresh_native_stats()
            self._prune_seen_update_keys()
            await self._refresh_dh_profile(force=False)
            await self._refresh_call_config(force=False)

    async def _handle_update(self, update: Any) -> None:
        name = getattr(update, "TL_NAME", None)
        if name != "updatePhoneCallSignalingData":
            key = self._update_key(update)
            if key is not None and self._seen_update_key(key):
                self._log(
                    logging.DEBUG,
                    "update.duplicate",
                    call_id=self._call_id_from_update(update),
                )
                return
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
            session.signaling_rx_count += 1
            session.last_signaling_blob_len = int(len(payload))
            prev_payload = self._agent_prev_in_signaling.get(session.call_id)
            starts_with_prev = False
            prev_starts_with_curr = False
            prev_len = 0
            if prev_payload is not None:
                prev_len = len(prev_payload)
                starts_with_prev = payload.startswith(prev_payload)
                prev_starts_with_curr = prev_payload.startswith(payload)
            self._agent_prev_in_signaling[session.call_id] = payload
            if session.signaling_rx_count <= 8 or starts_with_prev:
                _agent_debug_log(
                    run_id="run1",
                    hypothesis_id="H12",
                    location="manager.py:608",
                    message="incoming signaling shape",
                    data={
                        "call_id": int(session.call_id),
                        "rx_count": int(session.signaling_rx_count),
                        "len": int(len(payload)),
                        "prev_len": int(prev_len),
                        "starts_with_prev": bool(starts_with_prev),
                        "prev_starts_with_curr": bool(prev_starts_with_curr),
                        "head8_hex": payload[:8].hex(),
                        "head32_hex": payload[:32].hex(),
                        "tail8_hex": payload[-8:].hex(),
                    },
                )
            digest = hashlib.sha256(payload).digest()
            is_duplicate = session._is_duplicate_signaling(
                digest,
                max_history=self._config.max_signaling_history,
            )
            if is_duplicate:
                session.repeated_signaling_blob_count += 1
                self._log(logging.DEBUG, "signaling.retransmit", session=session)
            else:
                session._emit_signaling_data(payload)
                self._dump_signaling_blob(session, "in", payload)
            accepted = self._native_bridge.push_signaling(session.call_id, payload)
            if not accepted:
                native_errors = self._native_bridge.pull_error_events(session.call_id)
                native_error = native_errors[-1] if native_errors else None
                self._log(
                    logging.DEBUG,
                    "signaling.native_rejected",
                    session=session,
                    native_key_attached=session.native_key_attached,
                    native_error=native_error,
                )
                if self._should_retry_native_push(session, native_error):
                    session._queue_pending_signaling(
                        payload,
                        max_items=max(64, self._config.max_signaling_history * 4),
                    )
                    self._log(
                        logging.DEBUG,
                        "signaling.queued_for_retry",
                        session=session,
                        queued=len(session._pending_signaling),
                    )
            if (
                session.server_ready
                and not session.media_ready
                and self._config.degraded_in_call_mode
                and not self._config.disable_soft_media_ready
                and session.signaling_rx_count >= 3
                and session.signaling_tx_count >= 1
            ):
                session.media_ready = True
                session.had_degraded_media_ready = True
                self._log(logging.INFO, "call.media_ready_soft", session=session)
                self._refresh_call_readiness(session)
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
                server_ready=False,
                media_ready=False,
                protocol_negotiated=False,
            )
            self._sessions[call_id] = session
            self._log(logging.DEBUG, "call.session_created", session=session)
        self._gc_deadlines.pop(call_id, None)

        await self._dispatch_phone_call_event(session, phone_call)

    async def _dispatch_phone_call_event(self, session: CallSession, phone_call: Any) -> None:
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
        self._log(logging.DEBUG, "call.remote_event", session=session, tl_name=tl_name)

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
            reason_obj = getattr(phone_call, "reason", None)
            session.disconnect_reason_raw = str(getattr(reason_obj, "TL_NAME", ""))
            if session.end_reason is None:
                mapped_reason = self._map_discard_reason(reason_obj)
                if (
                    mapped_reason == CallEndReason.REMOTE_HANGUP
                    and self._config.fail_on_protocol_mismatch
                    and session.state == CallState.CONNECTING
                    and not session.media_ready
                ):
                    mapped_reason = CallEndReason.FAILED_PROTOCOL
                session.end_reason = mapped_reason
            if session.end_reason == CallEndReason.FAILED_PROTOCOL:
                self._maybe_activate_protocol_fallback(session)
            self._stop_call_tones(session, reason="remote_discard")
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
            session.disconnect_reason_raw = "phoneCallEmpty"
            if session.end_reason is None:
                session.end_reason = CallEndReason.REMOTE_HANGUP
            self._stop_call_tones(session, reason="phoneCallEmpty")
            self._transition_if_allowed(session, CallState.ENDED)
            self._cleanup_session(session)
            self._log(logging.INFO, "call.empty_remote", session=session)
            return

        raise CallProtocolError(f"unsupported phone call payload: {tl_name!r}")

    async def _handle_requested(self, session: CallSession, phone_call: Any) -> None:
        g_a_hash = getattr(phone_call, "g_a_hash", None)
        if not isinstance(g_a_hash, (bytes, bytearray)):
            raise CallProtocolError("phoneCallRequested missing g_a_hash bytes")

        session.server_ready = False
        session.media_ready = False
        session.protocol_negotiated = False
        session.disconnect_reason_raw = None
        session.e2e_key_fingerprint_hex = None
        session.e2e_emojis = None
        session.ringback_stopped_at = None
        session.ring_tone_stopped_at = None
        session.ring_tone_active = True
        session.in_call_gate_satisfied = False
        session.in_call_gate_block_reason = None
        session.native_handshake_block_reason = None
        session.last_signaling_blob_len = None
        session.repeated_signaling_blob_count = 0
        session.selected_relay_endpoint_id = None
        session.selected_relay_endpoint_kind = None
        session.signaling_rx_count = 0
        session.signaling_tx_count = 0
        session.native_decrypt_failures = 0
        session.audio_capture_frames = 0
        session.audio_push_ok = 0
        session.audio_push_fail = 0
        if session.crypto is None:
            session.crypto = CallCryptoContext.new_incoming(bytes(g_a_hash), self._crypto_profile)
        if session.state == CallState.IDLE:
            self._transition_if_allowed(session, CallState.RINGING_IN)
            self._arm_timer(session, "ringing", self._config.incoming_ring_timeout)
            await self._emit_incoming(session)

    async def _handle_waiting(self, session: CallSession) -> None:
        session.server_ready = False
        session.media_ready = False
        session.protocol_negotiated = False
        session.disconnect_reason_raw = None
        session.e2e_key_fingerprint_hex = None
        session.e2e_emojis = None
        session.ringback_stopped_at = None
        session.ring_tone_stopped_at = None
        session.ring_tone_active = not session.incoming
        session.in_call_gate_satisfied = False
        session.in_call_gate_block_reason = None
        session.native_handshake_block_reason = None
        session.last_signaling_blob_len = None
        session.repeated_signaling_blob_count = 0
        session.selected_relay_endpoint_id = None
        session.selected_relay_endpoint_kind = None
        session.signaling_rx_count = 0
        session.signaling_tx_count = 0
        session.native_decrypt_failures = 0
        session.audio_capture_frames = 0
        session.audio_push_ok = 0
        session.audio_push_fail = 0
        if not session.incoming and session.state in {CallState.IDLE, CallState.OUTGOING_INIT}:
            self._transition_if_allowed(session, CallState.CONNECTING)
            self._arm_timer(session, "connect", self._config.connect_timeout)

    async def _handle_accepted(self, session: CallSession, phone_call: Any) -> None:
        g_b = getattr(phone_call, "g_b", None)
        if session.crypto is None or session.crypto.role != "outgoing":
            raise CallProtocolError("phoneCallAccepted without outgoing crypto context")
        crypto = session.crypto
        session.server_ready = False
        session.media_ready = False
        session.protocol_negotiated = False
        session.disconnect_reason_raw = None
        session.e2e_key_fingerprint_hex = None
        session.e2e_emojis = None
        session.ringback_stopped_at = None
        session.ring_tone_stopped_at = None
        session.ring_tone_active = True
        session.in_call_gate_satisfied = False
        session.in_call_gate_block_reason = None
        session.native_handshake_block_reason = None
        session.last_signaling_blob_len = None
        session.repeated_signaling_blob_count = 0
        session.selected_relay_endpoint_id = None
        session.selected_relay_endpoint_kind = None
        session.signaling_rx_count = 0
        session.signaling_tx_count = 0
        session.native_decrypt_failures = 0
        session.audio_capture_frames = 0
        session.audio_push_ok = 0
        session.audio_push_fail = 0
        if not isinstance(g_b, (bytes, bytearray)):
            raise CallProtocolError("phoneCallAccepted missing g_b bytes")

        material = crypto.apply_remote_g_b(bytes(g_b))
        self._set_verified_key_visual(session, material.auth_key, material.key_fingerprint)
        self._stop_call_tones(session, reason="phoneCallAccepted")
        self._ensure_native_session(session)
        session.native_key_attached = self._native_bridge.set_keys(
            session.call_id,
            material.auth_key,
            material.key_fingerprint,
            is_outgoing=True,
        )
        if session.native_key_attached:
            self._flush_pending_signaling_to_native(session)
        confirm_result = await self._retry_for_session(
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
        confirm_phone_call = self._extract_phone_call_obj(confirm_result)
        if confirm_phone_call is not None:
            await self._dispatch_phone_call_event(session, confirm_phone_call)
        self._transition_if_allowed(session, CallState.CONNECTING)
        self._arm_timer(session, "connect", self._config.connect_timeout)

    async def _handle_established(self, session: CallSession, phone_call: Any) -> None:
        g_a_or_b = getattr(phone_call, "g_a_or_b", None)
        key_fingerprint = getattr(phone_call, "key_fingerprint", None)
        if not isinstance(key_fingerprint, int):
            raise CallProtocolError("phoneCall missing key_fingerprint")
        session.disconnect_reason_raw = None

        if session.crypto is not None:
            if not isinstance(g_a_or_b, (bytes, bytearray)):
                raise CallProtocolError("phoneCall missing g_a_or_b bytes")
            if (
                session.crypto.role == "outgoing"
                and session.crypto.key_material is not None
                and session.crypto.verify_fingerprint(int(key_fingerprint))
            ):
                material = session.crypto.key_material
            else:
                material = session.crypto.apply_final_public(
                    bytes(g_a_or_b),
                    expected_fingerprint=int(key_fingerprint),
                )
            self._ensure_native_session(session)
            needs_attach = not (
                session.native_key_attached
                and session.crypto.role == "outgoing"
                and material.key_fingerprint == int(key_fingerprint)
            )
            if needs_attach:
                attached = self._native_bridge.set_keys(
                    session.call_id,
                    material.auth_key,
                    material.key_fingerprint,
                    is_outgoing=bool(session.crypto.role == "outgoing"),
                )
                session.native_key_attached = attached
            else:
                session.native_key_attached = True
            self._set_verified_key_visual(session, material.auth_key, material.key_fingerprint)

        self._ensure_native_session(session)
        remote_protocol = getattr(phone_call, "protocol", None)
        remote_versions_raw = getattr(remote_protocol, "library_versions", None)
        remote_versions: list[str] = []
        if isinstance(remote_versions_raw, Sequence) and not isinstance(
            remote_versions_raw, (str, bytes, bytearray)
        ):
            for item in remote_versions_raw[:8]:
                remote_versions.append(str(item))
        # #region agent log
        _agent_debug_log(
            run_id="run1",
            hypothesis_id="H15",
            location="manager.py:903",
            message="protocol negotiation snapshot",
            data={
                "call_id": int(session.call_id),
                "remote_protocol_tl": str(getattr(remote_protocol, "TL_NAME", "")),
                "remote_min_layer": (
                    int(getattr(remote_protocol, "min_layer", -1))
                    if isinstance(getattr(remote_protocol, "min_layer", None), int)
                    else -1
                ),
                "remote_max_layer": (
                    int(getattr(remote_protocol, "max_layer", -1))
                    if isinstance(getattr(remote_protocol, "max_layer", None), int)
                    else -1
                ),
                "remote_udp_p2p": bool(getattr(remote_protocol, "udp_p2p", False)),
                "remote_udp_reflector": bool(getattr(remote_protocol, "udp_reflector", False)),
                "remote_versions": ",".join(remote_versions),
                "local_versions": ",".join(self._protocol_settings.library_versions),
                "active_library_version_index": int(self._active_library_version_index),
            },
        )
        # #endregion
        raw_connections = getattr(phone_call, "connections", None)
        raw_count = len(raw_connections) if isinstance(raw_connections, list) else None
        first_item = (
            raw_connections[0]
            if isinstance(raw_connections, list) and raw_connections
            else None
        )
        self._log(
            logging.DEBUG,
            "call.established_payload",
            session=session,
            connections_type=type(raw_connections).__name__,
            connections_count=raw_count,
            first_connection_tl=(
                str(getattr(first_item, "TL_NAME", "")) if first_item is not None else ""
            ),
            first_connection_ip_type=type(getattr(first_item, "ip", None)).__name__
            if first_item is not None
            else "",
            first_connection_port_type=type(getattr(first_item, "port", None)).__name__
            if first_item is not None
            else "",
            has_connection_field=hasattr(phone_call, "connection"),
            has_alternative_connections=hasattr(phone_call, "alternative_connections"),
        )
        endpoints = self._extract_phone_connections(phone_call)
        endpoints_applied = self._native_bridge.set_remote_endpoints(session.call_id, endpoints)
        relay_selected = next(
            (
                item
                for item in endpoints
                if str(item.get("kind", "")) == "phoneConnection"
            ),
            (endpoints[0] if endpoints else None),
        )
        if isinstance(relay_selected, dict):
            endpoint_id = relay_selected.get("id")
            session.selected_relay_endpoint_id = (
                int(endpoint_id) if isinstance(endpoint_id, int) else None
            )
            session.selected_relay_endpoint_kind = str(relay_selected.get("kind", "unknown"))
        else:
            session.selected_relay_endpoint_id = None
            session.selected_relay_endpoint_kind = None
        rtc_servers = self._extract_rtc_servers(phone_call)
        rtc_servers_applied = True
        set_rtc_servers = getattr(self._native_bridge, "set_rtc_servers", None)
        if callable(set_rtc_servers):
            rtc_servers_applied = bool(set_rtc_servers(session.call_id, rtc_servers))
        elif rtc_servers:
            rtc_servers_applied = False
        self._log(
            logging.DEBUG,
            "call.endpoints_loaded",
            session=session,
            endpoints=len(endpoints),
            kinds=",".join(
                sorted({str(item.get("kind", "")) for item in endpoints if isinstance(item, dict)})
            ),
            applied=endpoints_applied,
        )
        self._log(
            logging.DEBUG,
            "call.rtc_servers_loaded",
            session=session,
            rtc_servers=len(rtc_servers),
            applied=rtc_servers_applied,
            native_backend=str(getattr(self._native_bridge, "backend", "legacy")),
            supports_rtc_servers=bool(
                getattr(self._native_bridge, "supports_rtc_servers", False)
            ),
        )
        native_backend = str(getattr(self._native_bridge, "backend", "legacy"))
        supports_rtc_servers = bool(getattr(self._native_bridge, "supports_rtc_servers", False))
        has_webrtc_endpoint = any(
            str(item.get("kind", "")) == "phoneConnectionWebrtc" for item in endpoints
        )
        if has_webrtc_endpoint and (
            native_backend != NATIVE_BACKEND_TGCALLS
            or not supports_rtc_servers
        ):
            self._log(
                logging.WARNING,
                "call.native_backend_webrtc_limited",
                session=session,
                native_backend=native_backend,
                backend_preference=self._config.native_backend,
            )
        self._flush_pending_signaling_to_native(session)
        session.server_ready = True
        self._stop_call_tones(session, reason="phoneCall")
        self._cancel_timer(session.call_id, "ringing")
        self._transition_if_allowed(session, CallState.CONNECTING)
        self._refresh_call_readiness(session)

    async def _emit_incoming(self, session: CallSession) -> None:
        for handler in list(self._incoming_handlers):
            try:
                result = handler(session)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                self._fail_session(session, exc)

    def _refresh_call_readiness(self, session: CallSession) -> None:
        block_reason: str | None = None
        if session.state in TERMINAL_CALL_STATES:
            return
        if not session.server_ready:
            block_reason = "server_ready"
            self._update_in_call_gate_status(session, satisfied=False, block_reason=block_reason)
            self._cancel_timer(session.call_id, "media")
            self._transition_if_allowed(session, CallState.CONNECTING)
            return
        if not session.media_ready and self._config.strict_media_ready:
            block_reason = "native_media_ready"
            self._update_in_call_gate_status(session, satisfied=False, block_reason=block_reason)
            self._arm_timer(session, "media", self._config.media_ready_timeout)
            self._transition_if_allowed(session, CallState.CONNECTING)
            return
        if not session.media_ready and not self._config.strict_media_ready:
            session.media_ready = True
            self._log(logging.DEBUG, "call.media_ready_relaxed", session=session)
        if self._config.udp_media_required_for_in_call and self._config.native_bridge_enabled:
            udp_tx = int(getattr(session._stats, "udp_tx_bytes", 0) or 0)
            udp_rx = int(getattr(session._stats, "udp_rx_bytes", 0) or 0)
            if udp_tx <= 0 and udp_rx <= 0:
                block_reason = "udp_gate"
                self._update_in_call_gate_status(
                    session,
                    satisfied=False,
                    block_reason=block_reason,
                )
                self._arm_timer(session, "media", self._config.media_ready_timeout)
                self._log(
                    logging.DEBUG,
                    "call.media_ready_waiting_udp",
                    session=session,
                    udp_tx_bytes=udp_tx,
                    udp_rx_bytes=udp_rx,
                    native_backend=getattr(session._stats, "native_backend", None),
                )
                self._transition_if_allowed(session, CallState.CONNECTING)
                return

        self._update_in_call_gate_status(session, satisfied=True, block_reason=None)
        self._cancel_timer(session.call_id, "connect")
        self._cancel_timer(session.call_id, "media")
        session.protocol_negotiated = True
        self._stop_call_tones(session, reason="in_call")
        self._transition_if_allowed(session, CallState.IN_CALL)
        self._start_audio_session(session)
        if session.e2e_key_fingerprint_hex is not None and session.e2e_emojis is not None:
            self._log(
                logging.INFO,
                "call.e2e_visual_in_call",
                session=session,
                key_fingerprint=session.e2e_key_fingerprint_hex,
                emojis="".join(session.e2e_emojis),
            )
        self._log(
            logging.INFO,
            "call.in_call",
            session=session,
            server_ready=session.server_ready,
            media_ready=session.media_ready,
        )

    def _should_retry_native_push(
        self,
        session: CallSession,
        native_error: tuple[int, str] | None,
    ) -> bool:
        _ = session
        if native_error is None:
            return False
        code, _message = native_error
        return int(code) == -4

    def _flush_pending_signaling_to_native(self, session: CallSession) -> None:
        while True:
            payload = session._pop_pending_signaling()
            if payload is None:
                return
            accepted = self._native_bridge.push_signaling(session.call_id, payload)
            if accepted:
                continue
            native_errors = self._native_bridge.pull_error_events(session.call_id)
            native_error = native_errors[-1] if native_errors else None
            if self._should_retry_native_push(session, native_error):
                session._queue_pending_signaling(
                    payload,
                    max_items=max(64, self._config.max_signaling_history * 4),
                )
            self._log(
                logging.DEBUG,
                "signaling.pending_flush_rejected",
                session=session,
                native_error=native_error,
            )
            return

    async def _flush_native_signaling(self) -> None:
        for session in list(self._sessions.values()):
            if session.state in TERMINAL_CALL_STATES:
                continue
            while True:
                packet = self._native_bridge.pull_signaling(session.call_id)
                if packet is None:
                    break
                try:
                    self._dump_signaling_blob(session, "out", packet)
                    session.signaling_tx_count += 1
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

            for state_code in self._native_bridge.pull_state_events(session.call_id):
                session.native_state = int(state_code)
                if int(state_code) == TC_ENGINE_STATE_ESTABLISHED:
                    session.native_decrypt_failures = 0
                    if not session.media_ready:
                        session.media_ready = True
                        self._log(logging.INFO, "call.media_ready", session=session)
                    self._refresh_call_readiness(session)
                elif (
                    int(state_code) == TC_ENGINE_STATE_FAILED
                    and self._config.fail_on_protocol_mismatch
                ):
                    self._fail_session(
                        session,
                        CallProtocolError("native engine entered failed state"),
                    )
                    break

            if session.state in TERMINAL_CALL_STATES:
                continue

            errors = self._native_bridge.pull_error_events(session.call_id)
            if errors and self._config.fail_on_protocol_mismatch:
                code, message = errors[-1]
                if int(code) == -1 and "failed to decrypt signaling packet" in str(message):
                    _agent_debug_log(
                        run_id="run1",
                        hypothesis_id="H1",
                        location="manager.py:1009",
                        message="native decrypt error observed",
                        data={
                            "call_id": int(session.call_id),
                            "state": session.state.value,
                            "native_state": (
                                int(session.native_state)
                                if session.native_state is not None
                                else None
                            ),
                            "server_ready": bool(session.server_ready),
                            "media_ready": bool(session.media_ready),
                            "signaling_rx_count": int(session.signaling_rx_count),
                            "signaling_tx_count": int(session.signaling_tx_count),
                            "audio_push_ok": int(session.audio_push_ok),
                            "audio_push_fail": int(session.audio_push_fail),
                            "native_error_code": int(code),
                        },
                    )
                    session.native_decrypt_failures += 1
                    self._log(
                        logging.DEBUG,
                        "call.native_decrypt_ignored",
                        session=session,
                        native_error=(code, message),
                        failures=session.native_decrypt_failures,
                    )
                    if (
                        self._config.degraded_in_call_mode
                        and self._config.native_decrypt_fallback_soft_ready
                        and session.server_ready
                        and not session.media_ready
                        and session.native_decrypt_failures >= 2
                        and session.signaling_rx_count >= 1
                    ):
                        session.media_ready = True
                        session.had_degraded_media_ready = True
                        self._log(
                            logging.INFO,
                            "call.media_ready_degraded",
                            session=session,
                            failures=session.native_decrypt_failures,
                        )
                        self._refresh_call_readiness(session)
                    continue
                self._fail_session(
                    session,
                    CallProtocolError(f"native error code={code}: {message}"),
                )
                continue

            stats = self._native_bridge.poll_stats(session.call_id)
            if stats is None:
                stats = session._stats
            poll_debug = getattr(self._native_bridge, "poll_debug", None)
            debug_stats = (
                (poll_debug(session.call_id) or {})
                if callable(poll_debug)
                else {}
            )
            prev_stats = session._stats
            raw_media_packets_sent = stats.media_packets_sent
            raw_media_packets_recv = stats.media_packets_recv
            raw_packets_sent = stats.packets_sent
            raw_packets_recv = stats.packets_recv
            stats.native_backend = getattr(self._native_bridge, "backend", None)
            if stats.recv_loss is not None and stats.loss is None:
                stats.loss = stats.recv_loss
            stats.signaling_packets_recv = int(session.signaling_rx_count)
            stats.signaling_packets_sent = int(session.signaling_tx_count)
            if stats.packets_recv is None or int(stats.packets_recv) <= 0:
                stats.packets_recv = stats.signaling_packets_recv
            if stats.packets_sent is None or int(stats.packets_sent) <= 0:
                stats.packets_sent = stats.signaling_packets_sent
            if (
                stats.media_packets_recv in {None, 0}
                and prev_stats.media_packets_recv is not None
            ):
                stats.media_packets_recv = int(prev_stats.media_packets_recv)
            if (
                stats.media_packets_sent in {None, 0}
                and prev_stats.media_packets_sent is not None
            ):
                stats.media_packets_sent = int(prev_stats.media_packets_sent)
            if stats.media_packets_recv is None:
                stats.media_packets_recv = 0
            if stats.media_packets_sent is None:
                stats.media_packets_sent = 0
            stats.raw_media_packets_sent = int(
                debug_stats.get(
                    "raw_media_packets_sent",
                    0 if raw_media_packets_sent is None else int(raw_media_packets_sent),
                )
            )
            stats.raw_media_packets_recv = int(
                debug_stats.get(
                    "raw_media_packets_recv",
                    0 if raw_media_packets_recv is None else int(raw_media_packets_recv),
                )
            )
            stats.raw_packets_sent = int(
                debug_stats.get(
                    "raw_packets_sent", 0 if raw_packets_sent is None else int(raw_packets_sent)
                )
            )
            stats.raw_packets_recv = int(
                debug_stats.get(
                    "raw_packets_recv", 0 if raw_packets_recv is None else int(raw_packets_recv)
                )
            )
            stats.udp_tx_bytes = int(debug_stats.get("udp_tx_bytes", 0))
            stats.udp_rx_bytes = int(debug_stats.get("udp_rx_bytes", 0))
            stats.signaling_out_frames = int(debug_stats.get("signaling_out_frames", 0))
            stats.udp_out_frames = int(debug_stats.get("udp_out_frames", 0))
            stats.udp_in_frames = int(debug_stats.get("udp_in_frames", 0))
            stats.udp_recv_attempts = int(debug_stats.get("udp_recv_attempts", 0))
            stats.udp_recv_timeouts = int(debug_stats.get("udp_recv_timeouts", 0))
            stats.udp_recv_source_mismatch = int(
                debug_stats.get("udp_recv_source_mismatch", 0)
            )
            stats.udp_proto_decode_failures = int(
                debug_stats.get("udp_proto_decode_failures", 0)
            )
            stats.udp_rx_peer_tag_mismatch = int(
                debug_stats.get("udp_rx_peer_tag_mismatch", 0)
            )
            stats.udp_rx_short_packet_drops = int(
                debug_stats.get("udp_rx_short_packet_drops", 0)
            )
            stats.decrypt_failures_signaling = int(
                debug_stats.get("decrypt_failures_signaling", 0)
            )
            stats.decrypt_failures_udp = int(debug_stats.get("decrypt_failures_udp", 0))
            stats.signaling_proto_decode_failures = int(
                debug_stats.get("signaling_proto_decode_failures", 0)
            )
            stats.signaling_decrypt_ctr_failures = int(
                debug_stats.get("signaling_decrypt_ctr_failures", 0)
            )
            stats.signaling_decrypt_short_failures = int(
                debug_stats.get("signaling_decrypt_short_failures", 0)
            )
            stats.signaling_decrypt_ctr_header_invalid = int(
                debug_stats.get("signaling_decrypt_ctr_header_invalid", 0)
            )
            stats.signaling_decrypt_candidate_successes = int(
                debug_stats.get("signaling_decrypt_candidate_successes", 0)
            )
            stats.signaling_duplicate_ciphertexts_seen = int(
                debug_stats.get("signaling_duplicate_ciphertexts_seen", 0)
            )
            stats.signaling_ctr_last_error_code = int(
                debug_stats.get("signaling_ctr_last_error_code", 0)
            )
            stats.signaling_short_last_error_code = int(
                debug_stats.get("signaling_short_last_error_code", 0)
            )
            ctr_variant_code = int(debug_stats.get("signaling_ctr_last_variant", 0))
            if ctr_variant_code == 1:
                stats.signaling_ctr_last_variant = "kdf128"
            elif ctr_variant_code == 2:
                stats.signaling_ctr_last_variant = "kdf0"
            else:
                stats.signaling_ctr_last_variant = "none"
            ctr_hash_mode_code = int(debug_stats.get("signaling_ctr_last_hash_mode", 0))
            if ctr_hash_mode_code == 1:
                stats.signaling_ctr_last_hash_mode = "seq_payload"
            elif ctr_hash_mode_code == 2:
                stats.signaling_ctr_last_hash_mode = "payload_only"
            else:
                stats.signaling_ctr_last_hash_mode = "none"
            best_failure_mode_code = int(debug_stats.get("signaling_best_failure_mode", 0))
            if best_failure_mode_code == 1:
                stats.signaling_best_failure_mode = "ctr"
            elif best_failure_mode_code == 2:
                stats.signaling_best_failure_mode = "short"
            else:
                stats.signaling_best_failure_mode = "none"
            stats.signaling_best_failure_code = int(
                debug_stats.get("signaling_best_failure_code", 0)
            )
            signaling_mode_code = int(debug_stats.get("signaling_last_decrypt_mode", 0))
            if signaling_mode_code == 1:
                stats.signaling_last_decrypt_mode = "ctr"
            elif signaling_mode_code == 2:
                stats.signaling_last_decrypt_mode = "short"
            else:
                stats.signaling_last_decrypt_mode = "none"
            signaling_dir_code = int(debug_stats.get("signaling_last_decrypt_direction", 0))
            if signaling_dir_code == 1:
                stats.signaling_last_decrypt_direction = "local_role"
            elif signaling_dir_code == 2:
                stats.signaling_last_decrypt_direction = "opposite_role"
            else:
                stats.signaling_last_decrypt_direction = "none"
            signaling_err_stage_code = int(
                debug_stats.get("signaling_decrypt_last_error_stage", 0)
            )
            if signaling_err_stage_code == 1:
                stats.signaling_decrypt_last_error_stage = "ctr"
            elif signaling_err_stage_code == 2:
                stats.signaling_decrypt_last_error_stage = "short"
            else:
                stats.signaling_decrypt_last_error_stage = "none"
            stats.signaling_decrypt_last_error_code = int(
                debug_stats.get("signaling_decrypt_last_error_code", 0)
            )
            stats.signaling_proto_last_error_code = int(
                debug_stats.get("signaling_proto_last_error_code", 0)
            )
            winner_index = int(debug_stats.get("signaling_candidate_winner_index", -1))
            stats.signaling_candidate_winner_index = (
                winner_index if winner_index >= 0 else None
            )
            selected_endpoint_id = debug_stats.get("selected_endpoint_id")
            stats.selected_endpoint_id = (
                int(selected_endpoint_id)
                if isinstance(selected_endpoint_id, int)
                else None
            )
            selected_endpoint_kind_code = int(debug_stats.get("selected_endpoint_kind", 0))
            if selected_endpoint_kind_code == 1:
                stats.selected_endpoint_kind = "relay"
            elif selected_endpoint_kind_code == 2:
                stats.selected_endpoint_kind = "webrtc"
            else:
                stats.selected_endpoint_kind = "unknown"
            stats.local_audio_push_ok = int(session.audio_push_ok)
            stats.local_audio_push_fail = int(session.audio_push_fail)
            if stats.selected_endpoint_id is not None:
                session.selected_relay_endpoint_id = int(stats.selected_endpoint_id)
            if stats.selected_endpoint_kind is not None:
                session.selected_relay_endpoint_kind = str(stats.selected_endpoint_kind)
            session.native_handshake_block_reason = self._derive_native_handshake_block_reason(
                session,
                stats,
            )
            if (
                session.state == CallState.IN_CALL
                and (session.audio_push_ok > 0 or session.native_decrypt_failures > 0)
            ):
                _agent_debug_log(
                    run_id="run1",
                    hypothesis_id="H4",
                    location="manager.py:1068",
                    message="native stats snapshot",
                    data={
                        "call_id": int(session.call_id),
                        "native_state": (
                            int(session.native_state)
                            if session.native_state is not None
                            else None
                        ),
                        "audio_push_ok": int(session.audio_push_ok),
                        "audio_push_fail": int(session.audio_push_fail),
                        "decrypt_failures": int(session.native_decrypt_failures),
                        "raw_media_packets_sent": (
                            int(raw_media_packets_sent)
                            if raw_media_packets_sent is not None
                            else None
                        ),
                        "raw_media_packets_recv": (
                            int(raw_media_packets_recv)
                            if raw_media_packets_recv is not None
                            else None
                        ),
                        "raw_packets_sent": (
                            int(raw_packets_sent) if raw_packets_sent is not None else None
                        ),
                        "raw_packets_recv": (
                            int(raw_packets_recv) if raw_packets_recv is not None else None
                        ),
                        "final_media_packets_sent": int(stats.media_packets_sent or 0),
                        "final_media_packets_recv": int(stats.media_packets_recv or 0),
                        "udp_tx_bytes": int(stats.udp_tx_bytes or 0),
                        "udp_rx_bytes": int(stats.udp_rx_bytes or 0),
                        "udp_out_frames": int(stats.udp_out_frames or 0),
                        "udp_in_frames": int(stats.udp_in_frames or 0),
                    },
                )
            session._set_stats(stats)
            if (
                session.state == CallState.CONNECTING
                and session.server_ready
                and session.media_ready
            ):
                self._refresh_call_readiness(session)

    async def _refresh_dh_profile(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and (
            now - self._dh_profile_last_refresh
        ) < self._config.call_config_refresh_seconds:
            return

        try:
            result = await self._signaling.get_dh_config(
                version=self._dh_config_version,
                random_length=0,
                timeout=self._config.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "call.dh_config_fetch_failed", error=repr(exc))
            return

        tl_name = str(getattr(result, "TL_NAME", ""))
        if tl_name == "messages.dhConfigNotModified":
            self._dh_profile_last_refresh = now
            return
        if tl_name != "messages.dhConfig":
            self._log(logging.DEBUG, "call.dh_config_unexpected", tl_name=tl_name)
            return

        g = getattr(result, "g", None)
        dh_prime = getattr(result, "p", None)
        version = getattr(result, "version", None)
        if not isinstance(g, int) or not isinstance(dh_prime, (bytes, bytearray)):
            self._log(logging.WARNING, "call.dh_config_malformed", tl_name=tl_name)
            return

        try:
            profile = CallCryptoProfile(g=int(g), dh_prime=bytes(dh_prime))
            profile.validate()
        except Exception as exc:  # noqa: BLE001
            self._log(logging.WARNING, "call.dh_config_invalid", error=repr(exc))
            return

        self._crypto_profile = profile
        if isinstance(version, int):
            self._dh_config_version = int(version)
        self._dh_profile_last_refresh = now
        self._log(
            logging.INFO,
            "call.dh_config_loaded",
            dh_version=self._dh_config_version,
            g=profile.g,
            p_len=profile.p_size,
        )

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
        self._protocol_settings = self._merge_protocol_settings()
        self._native_bridge.set_allow_p2p(self._protocol_settings.udp_p2p)
        self._native_bridge.set_protocol_config(
            protocol_version=9,
            min_protocol_version=3,
            min_layer=self._protocol_settings.min_layer,
            max_layer=self._protocol_settings.max_layer,
        )
        self._native_bridge.set_network_type(self._config.network_type)
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
            interop_profile=self._config.interop_profile,
            network_type=self._config.network_type,
            remote_layers=f"{parsed.protocol.min_layer}-{parsed.protocol.max_layer}",
            remote_connection_max_layer=parsed.connection_max_layer,
            local_versions=",".join(self._protocol_settings.library_versions),
        )

    def _merge_protocol_settings(self) -> CallProtocolSettings:
        configured_versions = tuple(
            item.strip() for item in self._config.library_versions if item.strip()
        )
        if not configured_versions:
            configured_versions = _DEFAULT_LIBRARY_VERSIONS
        idx = min(self._active_library_version_index, len(configured_versions) - 1)
        versions = (configured_versions[idx],)

        udp_p2p = bool(self._config.allow_p2p)
        udp_reflector = bool(self._config.force_udp_reflector)

        min_layer = int(max(0, self._config.protocol_min_layer))
        max_layer = int(max(min_layer, self._config.protocol_max_layer))

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
        if to_state == CallState.IN_CALL and not session.in_call_gate_satisfied:
            self._log(
                logging.ERROR,
                "call.in_call_invariant_violation",
                session=session,
                gate_block_reason=session.in_call_gate_block_reason,
                server_ready=session.server_ready,
                media_ready=session.media_ready,
                udp_tx_bytes=int(getattr(session._stats, "udp_tx_bytes", 0) or 0),
                udp_rx_bytes=int(getattr(session._stats, "udp_rx_bytes", 0) or 0),
            )
            return
        if can_transition(session.state, to_state):
            from_state = session.state
            session._transition(to_state)
            self._log(
                logging.DEBUG,
                "call.transition",
                session=session,
                state_from=from_state.value,
                state_to=to_state.value,
            )
            return
        self._log(
            logging.DEBUG,
            "call.transition_ignored",
            session=session,
            state_from=session.state.value,
            state_to=to_state.value,
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
        raw_connections = getattr(phone_call, "connections", None)
        if raw_connections is None or isinstance(raw_connections, (str, bytes, bytearray)):
            return out
        try:
            connections = list(raw_connections)
        except TypeError:
            return out
        for item in connections:
            ip = getattr(item, "ip", None)
            ipv6 = getattr(item, "ipv6", None)
            port = getattr(item, "port", None)
            conn_id = getattr(item, "id", None)
            tl_name = str(getattr(item, "TL_NAME", ""))
            if isinstance(ip, (bytes, bytearray)):
                try:
                    ip = bytes(ip).decode("utf-8")
                except UnicodeDecodeError:
                    ip = None
            if isinstance(ipv6, (bytes, bytearray)):
                try:
                    ipv6 = bytes(ipv6).decode("utf-8")
                except UnicodeDecodeError:
                    ipv6 = ""
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

    def _extract_rtc_servers(self, phone_call: Any) -> list[dict[str, str | int | bool]]:
        out: list[dict[str, str | int | bool]] = []
        raw_connections = getattr(phone_call, "connections", None)
        if raw_connections is None or isinstance(raw_connections, (str, bytes, bytearray)):
            return out
        try:
            connections = list(raw_connections)
        except TypeError:
            return out

        seen: set[tuple[str, int, str, bool, bool]] = set()
        for item in connections:
            if str(getattr(item, "TL_NAME", "")) != "phoneConnectionWebrtc":
                continue

            ip = getattr(item, "ip", None)
            ipv6 = getattr(item, "ipv6", None)
            host: str = ""
            if isinstance(ip, (bytes, bytearray)):
                try:
                    host = bytes(ip).decode("utf-8")
                except UnicodeDecodeError:
                    host = ""
            elif isinstance(ip, str):
                host = ip

            if not host:
                if isinstance(ipv6, (bytes, bytearray)):
                    try:
                        host = bytes(ipv6).decode("utf-8")
                    except UnicodeDecodeError:
                        host = ""
                elif isinstance(ipv6, str):
                    host = ipv6

            port = getattr(item, "port", None)
            if not isinstance(host, str) or not host or not isinstance(port, int):
                continue

            username = getattr(item, "username", "")
            password = getattr(item, "password", "")
            if isinstance(username, (bytes, bytearray)):
                try:
                    username = bytes(username).decode("utf-8")
                except UnicodeDecodeError:
                    username = ""
            if isinstance(password, (bytes, bytearray)):
                try:
                    password = bytes(password).decode("utf-8")
                except UnicodeDecodeError:
                    password = ""

            is_turn = bool(getattr(item, "turn", False))
            is_stun = bool(getattr(item, "stun", False))
            is_tcp = False
            if not is_turn and not is_stun:
                continue

            key = (
                host,
                int(port),
                str(username),
                bool(is_turn),
                bool(is_tcp),
            )
            if key in seen:
                continue
            seen.add(key)

            out.append(
                {
                    "host": host,
                    "port": int(port),
                    "username": str(username),
                    "password": str(password),
                    "is_turn": bool(is_turn),
                    "is_tcp": bool(is_tcp),
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
        if name == "phoneCallDiscardReasonMigrateConferenceCall":
            return CallEndReason.REMOTE_HANGUP
        return CallEndReason.REMOTE_HANGUP

    def _maybe_activate_protocol_fallback(self, session: CallSession) -> None:
        if session.incoming:
            return
        if session.retries.get("protocol_fallback", 0) > 0:
            return
        elapsed = time.monotonic() - session.created_at
        if elapsed > 3.0:
            return

        configured_versions = tuple(
            item.strip() for item in self._config.library_versions if item.strip()
        )
        if len(configured_versions) <= 1:
            return
        if self._active_library_version_index >= (len(configured_versions) - 1):
            return

        from_version = configured_versions[self._active_library_version_index]
        self._active_library_version_index += 1
        to_version = configured_versions[self._active_library_version_index]
        session.retries["protocol_fallback"] = 1

        self._protocol_settings = self._merge_protocol_settings()
        self._native_bridge.set_protocol_config(
            protocol_version=9,
            min_protocol_version=3,
            min_layer=self._protocol_settings.min_layer,
            max_layer=self._protocol_settings.max_layer,
        )
        self._log(
            logging.INFO,
            "call.protocol_fallback_armed",
            session=session,
            from_library_version=from_version,
            to_library_version=to_version,
            elapsed_ms=int(elapsed * 1000),
        )

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
            elif timer_name == "media":
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
        reason = exception_to_reason(exc)
        self._log(
            logging.ERROR,
            "call.failed",
            session=session,
            error=repr(exc),
            reason_code=reason.value,
        )
        fallback_would_run = (
            reason == CallEndReason.FAILED_PROTOCOL
            and self._config.fail_on_protocol_mismatch
            and session.state == CallState.CONNECTING
            and not session.media_ready
        )
        # #region agent log
        _agent_debug_log(
            run_id="run1",
            hypothesis_id="H17",
            location="manager.py:1660",
            message="fallback eligibility evaluated",
            data={
                "call_id": int(session.call_id),
                "reason": reason.value,
                "state": session.state.value,
                "media_ready": bool(session.media_ready),
                "native_decrypt_failures": int(session.native_decrypt_failures),
                "fail_on_protocol_mismatch": bool(self._config.fail_on_protocol_mismatch),
                "fallback_would_run": bool(fallback_would_run),
                "active_library_version_index": int(self._active_library_version_index),
                "configured_versions": ",".join(
                    item.strip() for item in self._config.library_versions if item.strip()
                ),
            },
        )
        # #endregion
        if (
            reason == CallEndReason.FAILED_PROTOCOL
            and self._config.fail_on_protocol_mismatch
            and session.state == CallState.CONNECTING
            and not session.media_ready
        ):
            self._maybe_activate_protocol_fallback(session)
        self._stop_call_tones(session, reason="failed")
        session.server_ready = False
        session.media_ready = False
        session.protocol_negotiated = False
        session._set_failed(exc, reason=reason)
        self._cleanup_session(session)

    def _cleanup_session(self, session: CallSession) -> None:
        session.final_stats_snapshot = dict(session._stats.as_dict())
        session.final_audio_capture_frames = int(session.audio_capture_frames)
        session.final_audio_push_ok = int(session.audio_push_ok)
        session.final_audio_push_fail = int(session.audio_push_fail)
        self._stop_call_tones(session, reason="cleanup")
        self._cancel_session_timers(session)
        self._stop_audio_session(session)
        self._native_bridge.stop(session.call_id)
        session.server_ready = False
        session.media_ready = False
        session.protocol_negotiated = False
        session.native_state = None
        session.signaling_rx_count = 0
        session.signaling_tx_count = 0
        session.native_decrypt_failures = 0
        if session.crypto is not None:
            session.crypto.zeroize()
            session.crypto = None
        session._clear_signaling_history()
        session._clear_pending_signaling()
        self._agent_prev_in_signaling.pop(session.call_id, None)
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
        _agent_debug_log(
            run_id="run1",
            hypothesis_id="H3",
            location="manager.py:1568",
            message="ensure_native_session invoked",
            data={
                "call_id": int(session.call_id),
                "state": session.state.value,
                "incoming": bool(session.incoming),
                "native_key_attached": bool(session.native_key_attached),
                "protocol_min_layer": int(self._protocol_settings.min_layer),
                "protocol_max_layer": int(self._protocol_settings.max_layer),
            },
        )
        self._native_bridge.ensure_session(
            call_id=session.call_id,
            incoming=session.incoming,
            video=session.video,
        )
        self._native_bridge.set_protocol_config(
            protocol_version=9,
            min_protocol_version=3,
            min_layer=self._protocol_settings.min_layer,
            max_layer=self._protocol_settings.max_layer,
        )
        self._native_bridge.set_network_type(self._config.network_type)
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

    def _is_call_not_active_error(self, exc: Exception) -> bool:
        if not isinstance(exc, RpcErrorException):
            return False
        message = str(getattr(exc, "message", exc))
        return "CALL_NOT_ACTIVE" in message

    def _dump_signaling_blob(self, session: CallSession, direction: str, payload: bytes) -> None:
        dump_path = self._config.signaling_dump_path
        if dump_path is None:
            return
        digest = hashlib.sha256(payload).hexdigest()
        head_hex = payload[:16].hex()
        line = (
            f"{time.time():.6f} call_id={session.call_id} dir={direction} "
            f"len={len(payload)} sha256={digest} head16={head_hex}\n"
        )
        path = Path(dump_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:  # noqa: BLE001
            self._log(logging.DEBUG, "call.signaling_dump_failed", session=session, error=repr(exc))

    def _set_verified_key_visual(
        self,
        session: CallSession,
        auth_key: bytes,
        key_fingerprint: int,
    ) -> None:
        session.e2e_key_fingerprint_hex = fingerprint_to_hex(int(key_fingerprint))
        session.e2e_emojis = derive_call_emojis(auth_key, count=4)
        self._log(
            logging.DEBUG,
            "call.e2e_visual_ready",
            session=session,
            key_fingerprint=session.e2e_key_fingerprint_hex,
            emojis="".join(session.e2e_emojis),
        )

    def _mark_ring_tone_active(self, session: CallSession, *, source: str) -> None:
        if session.server_ready:
            self._log(logging.DEBUG, "call.tone_ignored_resume", session=session, source=source)
            return
        session.ring_tone_active = True
        session.ringback_stopped_at = None
        session.ring_tone_stopped_at = None

    def _stop_call_tones(self, session: CallSession, *, reason: str) -> None:
        if not session.ring_tone_active and session.ringback_stopped_at is not None:
            return
        now = time.monotonic()
        session.ring_tone_active = False
        if session.ringback_stopped_at is None:
            session.ringback_stopped_at = now
        if session.ring_tone_stopped_at is None:
            session.ring_tone_stopped_at = now
        self._stop_audio_session(session)
        self._log(logging.DEBUG, "call.tone_stopped", session=session, reason=reason)

    def _stop_local_ringback(self, session: CallSession, *, source: str) -> None:
        self._stop_call_tones(session, reason=source)

    def _update_in_call_gate_status(
        self,
        session: CallSession,
        *,
        satisfied: bool,
        block_reason: str | None,
    ) -> None:
        prev_satisfied = bool(session.in_call_gate_satisfied)
        prev_reason = session.in_call_gate_block_reason
        session.in_call_gate_satisfied = bool(satisfied)
        session.in_call_gate_block_reason = None if satisfied else block_reason
        if satisfied:
            if not prev_satisfied or prev_reason is not None:
                self._log(logging.DEBUG, "call.in_call_gate_satisfied", session=session)
            return
        if prev_satisfied or prev_reason != session.in_call_gate_block_reason:
            self._log(
                logging.DEBUG,
                "call.in_call_gate_blocked",
                session=session,
                reason=session.in_call_gate_block_reason,
            )

    def _derive_native_handshake_block_reason(
        self,
        session: CallSession,
        stats: CallStats,
    ) -> str | None:
        if session.state in TERMINAL_CALL_STATES:
            return None
        if (
            session.server_ready
            and session.media_ready
            and session.in_call_gate_block_reason == "udp_gate"
        ):
            return "udp_gate"

        native_state = int(session.native_state) if session.native_state is not None else None
        signaling_proto_fails = int(stats.signaling_proto_decode_failures or 0)
        signaling_decrypt_fails = int(stats.decrypt_failures_signaling or 0)

        if native_state == 5:
            return "wait_init"
        if native_state == 6:
            if signaling_proto_fails > 0:
                return "signaling_proto"
            if signaling_decrypt_fails > 0:
                return "signaling_decrypt"
            return "wait_init_ack"
        if native_state == 7 and session.in_call_gate_block_reason is not None:
            return str(session.in_call_gate_block_reason)
        return None

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
        if self._native_audio_managed:
            self._log(
                logging.DEBUG,
                "call.audio_skipped_native_backend",
                session=session,
                backend=str(getattr(self._native_bridge, "backend", "legacy")),
            )
            return
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
            session.audio_capture_frames += 1
            pushed = self._native_bridge.push_audio_frame(session.call_id, payload)
            if pushed:
                session.audio_push_ok += 1
                return
            session.audio_push_fail += 1
            if session.audio_push_fail <= 3 or (session.audio_push_fail % 100) == 0:
                self._log(
                    logging.DEBUG,
                    "call.audio_push_failed",
                    session=session,
                    capture_frames=session.audio_capture_frames,
                    pushed_ok=session.audio_push_ok,
                    push_fail=session.audio_push_fail,
                )

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
