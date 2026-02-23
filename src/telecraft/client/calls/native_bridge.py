from __future__ import annotations

import importlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .stats import CallStats

NATIVE_BACKEND_LEGACY = "legacy"
NATIVE_BACKEND_TGCALLS = "tgcalls"
NATIVE_BACKEND_AUTO = "auto"

ENDPOINT_FLAG_RELAY = 1 << 0
ENDPOINT_FLAG_P2P = 1 << 1
ENDPOINT_FLAG_TCP = 1 << 2
ENDPOINT_FLAG_TURN = 1 << 3

TC_ENGINE_STATE_ESTABLISHED = 7
TC_ENGINE_STATE_FAILED = 4

_NETWORK_TYPE_MAP: dict[str, int] = {
    "unknown": 0,
    "wifi": 1,
    "ethernet": 2,
    "cellular": 3,
}

EndpointPayload = dict[str, str | int | bytes]
RtcServerPayload = dict[str, str | int | bool]


def _normalize_native_backend(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in {
        NATIVE_BACKEND_LEGACY,
        NATIVE_BACKEND_TGCALLS,
        NATIVE_BACKEND_AUTO,
    }:
        return normalized
    return NATIVE_BACKEND_LEGACY


@dataclass(slots=True)
class _NativeSession:
    call_id: int
    incoming: bool
    video: bool
    handle: Any | None = None
    mock_queue: deque[bytes] = field(default_factory=deque)
    callback_signaling: deque[bytes] = field(default_factory=deque)
    state_events: deque[int] = field(default_factory=deque)
    error_events: deque[tuple[int, str]] = field(default_factory=deque)
    stats: CallStats = field(default_factory=CallStats)
    muted: bool = False
    bitrate_hint_kbps: int = 24
    c_endpoint_buffers: list[Any] = field(default_factory=list)
    callback_refs: list[Any] = field(default_factory=list)
    audio_queue: deque[bytes] = field(default_factory=deque)
    rtc_servers: list[RtcServerPayload] = field(default_factory=list)
    c_rtc_server_buffers: list[Any] = field(default_factory=list)


class NativeBridge:
    """
    Thin bridge for the optional native engine.

    Behavior:
    - `enabled=False`: no-op bridge, state tracked in-memory.
    - `enabled=True` + native module available: delegate to C engine ABI.
    - `enabled=True` + native module missing: fallback to mock mode.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        test_mode: bool,
        allow_p2p: bool = False,
        relay_preferred: bool = True,
        backend: str = NATIVE_BACKEND_LEGACY,
    ) -> None:
        self._enabled = bool(enabled)
        self._test_mode = bool(test_mode)
        self._allow_p2p = bool(allow_p2p)
        self._relay_preferred = bool(relay_preferred)
        self._backend_preference = _normalize_native_backend(backend)
        self._backend = (
            NATIVE_BACKEND_LEGACY
            if self._backend_preference == NATIVE_BACKEND_AUTO
            else self._backend_preference
        )
        self._network_type = _NETWORK_TYPE_MAP["wifi"]
        self._protocol_version = 9
        self._min_protocol_version = 3
        self._protocol_min_layer = 65
        self._protocol_max_layer = 92
        self._sessions: dict[int, _NativeSession] = {}
        self._ffi: Any | None = None
        self._lib: Any | None = None
        self._available = False
        self._has_rtc_server_api = False
        self._has_debug_api = False

        if self._enabled:
            self._load_native_module()

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def supports_rtc_servers(self) -> bool:
        return bool(self._has_rtc_server_api)

    def _load_native_module(self) -> None:
        candidates: list[tuple[str, str]]
        if self._backend_preference == NATIVE_BACKEND_TGCALLS:
            candidates = [
                (NATIVE_BACKEND_TGCALLS, "telecraft.client.calls._tgcalls_cffi"),
            ]
        elif self._backend_preference == NATIVE_BACKEND_AUTO:
            candidates = [
                (NATIVE_BACKEND_TGCALLS, "telecraft.client.calls._tgcalls_cffi"),
                (NATIVE_BACKEND_LEGACY, "telecraft.client.calls._engine_cffi"),
            ]
        else:
            candidates = [
                (NATIVE_BACKEND_LEGACY, "telecraft.client.calls._engine_cffi"),
            ]

        for backend_name, module_name in candidates:
            try:
                engine_module = importlib.import_module(module_name)
            except Exception:
                continue
            self._ffi = engine_module.ffi
            self._lib = engine_module.lib
            self._available = True
            self._backend = backend_name
            self._has_rtc_server_api = bool(
                hasattr(self._lib, "tc_engine_set_rtc_servers")
            )
            self._has_debug_api = bool(hasattr(self._lib, "tc_engine_poll_debug"))
            return

        self._available = False
        self._has_rtc_server_api = False
        self._has_debug_api = False

    def set_allow_p2p(self, value: bool) -> None:
        self._allow_p2p = bool(value)

    def set_protocol_config(
        self,
        *,
        protocol_version: int,
        min_protocol_version: int,
        min_layer: int,
        max_layer: int,
    ) -> None:
        self._protocol_version = max(1, int(protocol_version))
        self._min_protocol_version = max(1, int(min_protocol_version))
        self._protocol_min_layer = max(0, int(min_layer))
        self._protocol_max_layer = max(self._protocol_min_layer, int(max_layer))
        if not self._enabled or not self._available:
            return
        assert self._lib is not None and self._ffi is not None
        for session in self._sessions.values():
            if session.handle is None or session.handle == self._ffi.NULL:
                continue
            cfg = self._ffi.new("tc_protocol_config_t *")
            cfg.protocol_version = int(self._protocol_version)
            cfg.min_protocol_version = int(self._min_protocol_version)
            cfg.min_layer = int(self._protocol_min_layer)
            cfg.max_layer = int(self._protocol_max_layer)
            _ = self._lib.tc_engine_set_protocol_config(session.handle, cfg)

    def set_network_type(self, value: str) -> None:
        normalized = str(value).strip().lower()
        self._network_type = _NETWORK_TYPE_MAP.get(normalized, _NETWORK_TYPE_MAP["unknown"])
        if not self._enabled or not self._available:
            return
        assert self._lib is not None and self._ffi is not None
        for session in self._sessions.values():
            if session.handle is None or session.handle == self._ffi.NULL:
                continue
            _ = self._lib.tc_engine_set_network_type(session.handle, int(self._network_type))

    def ensure_session(self, *, call_id: int, incoming: bool, video: bool) -> None:
        cid = int(call_id)
        if cid in self._sessions:
            return
        session = _NativeSession(call_id=cid, incoming=bool(incoming), video=bool(video))
        session.stats.native_backend = self._backend
        self._sessions[cid] = session
        if not self._enabled or not self._available:
            return
        ffi = self._ffi
        lib = self._lib
        assert ffi is not None and lib is not None

        @ffi.callback("void(void *, int)")  # type: ignore[untyped-decorator]
        def _on_state(_user_data: Any, state: int) -> None:
            session.state_events.append(int(state))

        @ffi.callback("void(void *, int, const char *)")  # type: ignore[untyped-decorator]
        def _on_error(_user_data: Any, code: int, message: Any) -> None:
            text = "native error"
            if message != ffi.NULL:
                try:
                    text = ffi.string(message).decode("utf-8", errors="replace")
                except Exception:
                    text = "native error"
            session.error_events.append((int(code), text))

        @ffi.callback("void(void *, const uint8_t *, size_t)")  # type: ignore[untyped-decorator]
        def _on_signaling(_user_data: Any, data: Any, length: int) -> None:
            if data == ffi.NULL or int(length) <= 0:
                return
            session.callback_signaling.append(bytes(ffi.buffer(data, int(length))))

        @ffi.callback("void(void *, const tc_stats_t *)")  # type: ignore[untyped-decorator]
        def _on_stats(_user_data: Any, stats_ptr: Any) -> None:
            if stats_ptr == ffi.NULL:
                return
            stats = stats_ptr[0]
            session.stats = CallStats(
                rtt_ms=float(stats.rtt_ms),
                loss=float(stats.loss),
                bitrate_kbps=float(stats.bitrate_kbps),
                jitter_ms=float(stats.jitter_ms),
                packets_sent=int(stats.packets_sent),
                packets_recv=int(stats.packets_recv),
                media_packets_sent=int(stats.media_packets_sent),
                media_packets_recv=int(stats.media_packets_recv),
                signaling_packets_sent=int(stats.signaling_packets_sent),
                signaling_packets_recv=int(stats.signaling_packets_recv),
                send_loss=float(stats.send_loss),
                recv_loss=float(stats.recv_loss),
                endpoint_id=int(stats.endpoint_id),
                native_backend=self._backend,
                updated_at=time.monotonic(),
            )

        params = ffi.new("tc_engine_params_t *")
        params.call_id = cid
        params.incoming = 1 if incoming else 0
        params.video = 1 if video else 0
        params.user_data = ffi.NULL
        params.on_state = _on_state
        params.on_error = _on_error
        params.on_signaling = _on_signaling
        params.on_stats = _on_stats

        session.callback_refs.extend([_on_state, _on_error, _on_signaling, _on_stats, params])
        handle = lib.tc_engine_create(params)
        if handle == ffi.NULL:
            return
        session.handle = handle
        cfg = ffi.new("tc_protocol_config_t *")
        cfg.protocol_version = int(self._protocol_version)
        cfg.min_protocol_version = int(self._min_protocol_version)
        cfg.min_layer = int(self._protocol_min_layer)
        cfg.max_layer = int(self._protocol_max_layer)
        _ = lib.tc_engine_set_protocol_config(handle, cfg)
        _ = lib.tc_engine_set_network_type(handle, int(self._network_type))
        if session.rtc_servers:
            _ = self._apply_rtc_servers(session)
        lib.tc_engine_start(handle)

    def stop(self, call_id: int) -> None:
        cid = int(call_id)
        session = self._sessions.pop(cid, None)
        if session is None:
            return
        if not self._enabled or not self._available:
            return
        assert self._lib is not None and self._ffi is not None
        if session.handle is not None and session.handle != self._ffi.NULL:
            self._lib.tc_engine_stop(session.handle)
            self._lib.tc_engine_destroy(session.handle)

    def set_keys(
        self,
        call_id: int,
        auth_key: bytes,
        key_fingerprint: int,
        *,
        is_outgoing: bool,
    ) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        if not self._enabled or not self._available or session.handle is None:
            return True
        assert self._lib is not None and self._ffi is not None
        key_buf = self._ffi.new("uint8_t[]", auth_key)
        rc = self._lib.tc_engine_set_keys(
            session.handle,
            key_buf,
            len(auth_key),
            int(key_fingerprint),
            1 if is_outgoing else 0,
        )
        return int(rc) == 0

    def set_remote_endpoints(self, call_id: int, endpoints: list[EndpointPayload]) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        selected = self._select_endpoints(endpoints)
        if not selected:
            return False

        first_endpoint = selected[0]
        endpoint_id = first_endpoint.get("id")
        if isinstance(endpoint_id, int):
            session.stats.endpoint_id = endpoint_id

        if not self._enabled or not self._available or session.handle is None:
            return True
        assert self._lib is not None and self._ffi is not None

        c_items = self._ffi.new("tc_endpoint_t[]", len(selected))
        session.c_endpoint_buffers.clear()
        for idx, endpoint in enumerate(selected):
            c_items[idx].id = int(endpoint.get("id", 0))
            ip = str(endpoint.get("ip", ""))
            ipv6 = str(endpoint.get("ipv6", ""))
            port = int(endpoint.get("port", 0))
            flags = int(endpoint.get("flags", 0))
            priority = int(endpoint.get("priority", 100))
            peer_tag = endpoint.get("peer_tag", b"")
            if not isinstance(peer_tag, (bytes, bytearray)):
                peer_tag = b""

            c_ip = self._ffi.new("char[]", ip.encode("utf-8"))
            c_ipv6 = self._ffi.new("char[]", ipv6.encode("utf-8"))
            c_tag = self._ffi.new("uint8_t[]", bytes(peer_tag))

            c_items[idx].ip = c_ip
            c_items[idx].ipv6 = c_ipv6
            c_items[idx].port = port
            c_items[idx].peer_tag = c_tag
            c_items[idx].peer_tag_len = len(peer_tag)
            c_items[idx].flags = flags
            c_items[idx].priority = priority
            session.c_endpoint_buffers.extend([c_ip, c_ipv6, c_tag])

        rc = self._lib.tc_engine_set_remote_endpoints(session.handle, c_items, len(selected))
        return int(rc) == 0

    def set_rtc_servers(self, call_id: int, rtc_servers: list[RtcServerPayload]) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        normalized = self._normalize_rtc_servers(rtc_servers)
        session.rtc_servers = normalized

        if not self._enabled or not self._available or session.handle is None:
            return True
        return self._apply_rtc_servers(session)

    def set_mute(self, call_id: int, muted: bool) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        session.muted = bool(muted)
        if not self._enabled or not self._available or session.handle is None:
            return True
        assert self._lib is not None
        rc = self._lib.tc_engine_set_mute(session.handle, 1 if muted else 0)
        return int(rc) == 0

    def set_bitrate_hint(self, call_id: int, bitrate_kbps: int) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        session.bitrate_hint_kbps = max(8, int(bitrate_kbps))
        if not self._enabled or not self._available or session.handle is None:
            return True
        assert self._lib is not None
        rc = self._lib.tc_engine_set_bitrate_hint(session.handle, int(session.bitrate_hint_kbps))
        return int(rc) == 0

    def push_signaling(self, call_id: int, data: bytes) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False

        payload = bytes(data)
        now = time.monotonic()
        session.stats.updated_at = now
        session.stats.bitrate_kbps = max(1.0, (len(payload) * 8.0) / 1000.0)
        if session.stats.packets_recv is None:
            session.stats.packets_recv = 0
        session.stats.packets_recv += 1
        if session.stats.signaling_packets_recv is None:
            session.stats.signaling_packets_recv = 0
        session.stats.signaling_packets_recv += 1

        if not self._enabled or not self._available or session.handle is None:
            if self._test_mode:
                session.mock_queue.append(payload)
                session.stats.rtt_ms = 50.0
                session.stats.loss = 0.0
                session.stats.recv_loss = 0.0
                return True
            return False

        assert self._lib is not None and self._ffi is not None
        buf = self._ffi.new("uint8_t[]", payload)
        rc = self._lib.tc_engine_push_signaling(session.handle, buf, len(payload))
        rc_int = int(rc)
        if rc_int != 0:
            session.error_events.append((rc_int, "push_signaling"))
            return False
        return True

    def pull_signaling(self, call_id: int) -> bytes | None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return None

        if session.callback_signaling:
            payload = session.callback_signaling.popleft()
            if session.stats.packets_sent is None:
                session.stats.packets_sent = 0
            session.stats.packets_sent += 1
            if session.stats.signaling_packets_sent is None:
                session.stats.signaling_packets_sent = 0
            session.stats.signaling_packets_sent += 1
            return payload

        if not self._enabled or not self._available or session.handle is None:
            if not self._test_mode or not session.mock_queue:
                return None
            payload = session.mock_queue.popleft()
            if session.stats.packets_sent is None:
                session.stats.packets_sent = 0
            session.stats.packets_sent += 1
            if session.stats.signaling_packets_sent is None:
                session.stats.signaling_packets_sent = 0
            session.stats.signaling_packets_sent += 1
            return payload

        assert self._lib is not None and self._ffi is not None
        out_cap = 16 * 1024
        out = self._ffi.new("uint8_t[]", out_cap)
        result = self._lib.tc_engine_pull_signaling(session.handle, out, out_cap)
        if int(result) <= 0:
            return None
        return bytes(self._ffi.buffer(out, int(result)))

    def pull_state_events(self, call_id: int) -> tuple[int, ...]:
        session = self._sessions.get(int(call_id))
        if session is None or not session.state_events:
            return ()
        out = tuple(session.state_events)
        session.state_events.clear()
        return out

    def pull_error_events(self, call_id: int) -> tuple[tuple[int, str], ...]:
        session = self._sessions.get(int(call_id))
        if session is None or not session.error_events:
            return ()
        out = tuple(session.error_events)
        session.error_events.clear()
        return out

    def poll_stats(self, call_id: int) -> CallStats | None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return None
        if not self._enabled or not self._available or session.handle is None:
            return CallStats(
                rtt_ms=session.stats.rtt_ms,
                loss=session.stats.loss,
                bitrate_kbps=session.stats.bitrate_kbps,
                jitter_ms=session.stats.jitter_ms,
                packets_sent=session.stats.packets_sent,
                packets_recv=session.stats.packets_recv,
                media_packets_sent=session.stats.media_packets_sent,
                media_packets_recv=session.stats.media_packets_recv,
                signaling_packets_sent=session.stats.signaling_packets_sent,
                signaling_packets_recv=session.stats.signaling_packets_recv,
                send_loss=session.stats.send_loss,
                recv_loss=session.stats.recv_loss,
                endpoint_id=session.stats.endpoint_id,
                native_backend=self._backend,
                udp_tx_bytes=session.stats.udp_tx_bytes,
                udp_rx_bytes=session.stats.udp_rx_bytes,
                raw_media_packets_sent=session.stats.raw_media_packets_sent,
                raw_media_packets_recv=session.stats.raw_media_packets_recv,
                raw_packets_sent=session.stats.raw_packets_sent,
                raw_packets_recv=session.stats.raw_packets_recv,
                signaling_out_frames=session.stats.signaling_out_frames,
                udp_out_frames=session.stats.udp_out_frames,
                udp_in_frames=session.stats.udp_in_frames,
                decrypt_failures_signaling=session.stats.decrypt_failures_signaling,
                decrypt_failures_udp=session.stats.decrypt_failures_udp,
                local_audio_push_ok=session.stats.local_audio_push_ok,
                local_audio_push_fail=session.stats.local_audio_push_fail,
                updated_at=session.stats.updated_at,
            )

        assert self._lib is not None and self._ffi is not None
        out = self._ffi.new("tc_stats_t *")
        rc = self._lib.tc_engine_poll_stats(session.handle, out)
        if int(rc) != 0:
            return None
        return CallStats(
            rtt_ms=float(out.rtt_ms),
            loss=float(out.loss),
            bitrate_kbps=float(out.bitrate_kbps),
            jitter_ms=float(out.jitter_ms),
            packets_sent=int(out.packets_sent),
            packets_recv=int(out.packets_recv),
            media_packets_sent=int(out.media_packets_sent),
            media_packets_recv=int(out.media_packets_recv),
            signaling_packets_sent=int(out.signaling_packets_sent),
            signaling_packets_recv=int(out.signaling_packets_recv),
            send_loss=float(out.send_loss),
            recv_loss=float(out.recv_loss),
            endpoint_id=int(out.endpoint_id),
            native_backend=self._backend,
            updated_at=time.monotonic(),
        )

    def poll_debug(self, call_id: int) -> dict[str, int] | None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return None
        if not self._enabled or not self._available or session.handle is None:
            if session.stats is None:
                return None
            return {
                "signaling_out_frames": int(session.stats.signaling_out_frames or 0),
                "udp_out_frames": int(session.stats.udp_out_frames or 0),
                "udp_in_frames": int(session.stats.udp_in_frames or 0),
                "udp_recv_attempts": int(session.stats.udp_recv_attempts or 0),
                "udp_recv_timeouts": int(session.stats.udp_recv_timeouts or 0),
                "udp_recv_source_mismatch": int(session.stats.udp_recv_source_mismatch or 0),
                "udp_proto_decode_failures": int(session.stats.udp_proto_decode_failures or 0),
                "udp_rx_peer_tag_mismatch": int(session.stats.udp_rx_peer_tag_mismatch or 0),
                "udp_rx_short_packet_drops": int(session.stats.udp_rx_short_packet_drops or 0),
                "decrypt_failures_signaling": int(session.stats.decrypt_failures_signaling or 0),
                "decrypt_failures_udp": int(session.stats.decrypt_failures_udp or 0),
                "signaling_proto_decode_failures": int(
                    session.stats.signaling_proto_decode_failures or 0
                ),
                "signaling_decrypt_ctr_failures": int(
                    session.stats.signaling_decrypt_ctr_failures or 0
                ),
                "signaling_decrypt_short_failures": int(
                    session.stats.signaling_decrypt_short_failures or 0
                ),
                "signaling_decrypt_ctr_header_invalid": int(
                    session.stats.signaling_decrypt_ctr_header_invalid or 0
                ),
                "signaling_decrypt_candidate_successes": int(
                    session.stats.signaling_decrypt_candidate_successes or 0
                ),
                "signaling_duplicate_ciphertexts_seen": int(
                    session.stats.signaling_duplicate_ciphertexts_seen or 0
                ),
                "signaling_ctr_last_error_code": int(
                    session.stats.signaling_ctr_last_error_code or 0
                ),
                "signaling_short_last_error_code": int(
                    session.stats.signaling_short_last_error_code or 0
                ),
                "signaling_ctr_last_variant": (
                    1
                    if str(session.stats.signaling_ctr_last_variant or "") == "kdf128"
                    else (2 if str(session.stats.signaling_ctr_last_variant or "") == "kdf0" else 0)
                ),
                "signaling_ctr_last_hash_mode": (
                    1
                    if str(session.stats.signaling_ctr_last_hash_mode or "") == "seq_payload"
                    else (
                        2
                        if str(session.stats.signaling_ctr_last_hash_mode or "") == "payload_only"
                        else 0
                    )
                ),
                "signaling_best_failure_mode": (
                    1
                    if str(session.stats.signaling_best_failure_mode or "") == "ctr"
                    else (
                        2
                        if str(session.stats.signaling_best_failure_mode or "") == "short"
                        else 0
                    )
                ),
                "signaling_best_failure_code": int(
                    session.stats.signaling_best_failure_code or 0
                ),
                "signaling_last_decrypt_mode": (
                    1
                    if str(session.stats.signaling_last_decrypt_mode or "") == "ctr"
                    else (
                        2
                        if str(session.stats.signaling_last_decrypt_mode or "") == "short"
                        else 0
                    )
                ),
                "signaling_last_decrypt_direction": (
                    1
                    if str(session.stats.signaling_last_decrypt_direction or "") == "local_role"
                    else (
                        2
                        if str(session.stats.signaling_last_decrypt_direction or "")
                        == "opposite_role"
                        else 0
                    )
                ),
                "signaling_decrypt_last_error_code": int(
                    session.stats.signaling_decrypt_last_error_code or 0
                ),
                "signaling_decrypt_last_error_stage": (
                    1
                    if str(session.stats.signaling_decrypt_last_error_stage or "") == "ctr"
                    else (
                        2
                        if str(session.stats.signaling_decrypt_last_error_stage or "") == "short"
                        else 0
                    )
                ),
                "signaling_proto_last_error_code": int(
                    session.stats.signaling_proto_last_error_code or 0
                ),
                "signaling_candidate_winner_index": int(
                    session.stats.signaling_candidate_winner_index
                    if session.stats.signaling_candidate_winner_index is not None
                    else -1
                ),
                "udp_tx_bytes": int(session.stats.udp_tx_bytes or 0),
                "udp_rx_bytes": int(session.stats.udp_rx_bytes or 0),
                "raw_packets_sent": int(
                    session.stats.raw_packets_sent
                    if session.stats.raw_packets_sent is not None
                    else (session.stats.packets_sent or 0)
                ),
                "raw_packets_recv": int(
                    session.stats.raw_packets_recv
                    if session.stats.raw_packets_recv is not None
                    else (session.stats.packets_recv or 0)
                ),
                "raw_media_packets_sent": int(
                    session.stats.raw_media_packets_sent
                    if session.stats.raw_media_packets_sent is not None
                    else (session.stats.media_packets_sent or 0)
                ),
                "raw_media_packets_recv": int(
                    session.stats.raw_media_packets_recv
                    if session.stats.raw_media_packets_recv is not None
                    else (session.stats.media_packets_recv or 0)
                ),
                "selected_endpoint_id": int(session.stats.selected_endpoint_id or 0),
                "selected_endpoint_kind": (
                    1
                    if str(session.stats.selected_endpoint_kind or "") == "relay"
                    else (
                        2
                        if str(session.stats.selected_endpoint_kind or "") == "webrtc"
                        else 0
                    )
                ),
            }

        if not self._has_debug_api:
            return None

        assert self._lib is not None and self._ffi is not None
        out = self._ffi.new("tc_debug_stats_t *")
        rc = self._lib.tc_engine_poll_debug(session.handle, out)
        if int(rc) != 0:
            return None
        return {
            "signaling_out_frames": int(out.signaling_out_frames),
            "udp_out_frames": int(out.udp_out_frames),
            "udp_in_frames": int(out.udp_in_frames),
            "udp_recv_attempts": int(out.udp_recv_attempts),
            "udp_recv_timeouts": int(out.udp_recv_timeouts),
            "udp_recv_source_mismatch": int(out.udp_recv_source_mismatch),
            "udp_proto_decode_failures": int(out.udp_proto_decode_failures),
            "udp_rx_peer_tag_mismatch": int(out.udp_rx_peer_tag_mismatch),
            "udp_rx_short_packet_drops": int(out.udp_rx_short_packet_drops),
            "decrypt_failures_signaling": int(out.decrypt_failures_signaling),
            "decrypt_failures_udp": int(out.decrypt_failures_udp),
            "signaling_proto_decode_failures": int(out.signaling_proto_decode_failures),
            "signaling_decrypt_ctr_failures": int(out.signaling_decrypt_ctr_failures),
            "signaling_decrypt_short_failures": int(out.signaling_decrypt_short_failures),
            "signaling_decrypt_ctr_header_invalid": int(out.signaling_decrypt_ctr_header_invalid),
            "signaling_decrypt_candidate_successes": int(
                out.signaling_decrypt_candidate_successes
            ),
            "signaling_duplicate_ciphertexts_seen": int(out.signaling_duplicate_ciphertexts_seen),
            "signaling_ctr_last_error_code": int(out.signaling_ctr_last_error_code),
            "signaling_short_last_error_code": int(out.signaling_short_last_error_code),
            "signaling_ctr_last_variant": int(out.signaling_ctr_last_variant),
            "signaling_ctr_last_hash_mode": int(out.signaling_ctr_last_hash_mode),
            "signaling_best_failure_mode": int(out.signaling_best_failure_mode),
            "signaling_best_failure_code": int(out.signaling_best_failure_code),
            "signaling_last_decrypt_mode": int(out.signaling_last_decrypt_mode),
            "signaling_last_decrypt_direction": int(out.signaling_last_decrypt_direction),
            "signaling_decrypt_last_error_code": int(out.signaling_decrypt_last_error_code),
            "signaling_decrypt_last_error_stage": int(out.signaling_decrypt_last_error_stage),
            "signaling_proto_last_error_code": int(out.signaling_proto_last_error_code),
            "signaling_candidate_winner_index": int(out.signaling_candidate_winner_index),
            "udp_tx_bytes": int(out.udp_tx_bytes),
            "udp_rx_bytes": int(out.udp_rx_bytes),
            "raw_packets_sent": int(out.raw_packets_sent),
            "raw_packets_recv": int(out.raw_packets_recv),
            "raw_media_packets_sent": int(out.raw_media_packets_sent),
            "raw_media_packets_recv": int(out.raw_media_packets_recv),
            "selected_endpoint_id": int(out.selected_endpoint_id),
            "selected_endpoint_kind": int(out.selected_endpoint_kind),
        }

    def push_audio_frame(self, call_id: int, pcm: bytes) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        payload = bytes(pcm)
        if not payload or (len(payload) % 2) != 0:
            return False

        if not self._enabled or not self._available or session.handle is None:
            if self._test_mode:
                session.audio_queue.append(payload)
                if session.stats.media_packets_sent is None:
                    session.stats.media_packets_sent = 0
                session.stats.media_packets_sent += 1
                while len(session.audio_queue) > 64:
                    session.audio_queue.popleft()
                return True
            return False

        assert self._lib is not None and self._ffi is not None
        frame_samples = len(payload) // 2
        c_pcm = self._ffi.new("int16_t[]", frame_samples)
        self._ffi.memmove(c_pcm, payload, len(payload))
        rc = self._lib.tc_engine_push_audio_frame(session.handle, c_pcm, frame_samples)
        if int(rc) == 0:
            if session.stats.media_packets_sent is None:
                session.stats.media_packets_sent = 0
            session.stats.media_packets_sent += 1
        return int(rc) == 0

    def pull_audio_frame(self, call_id: int, *, frame_samples: int = 960) -> bytes | None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return None
        if frame_samples <= 0:
            return None

        if not self._enabled or not self._available or session.handle is None:
            if not self._test_mode or not session.audio_queue:
                return None
            if session.stats.media_packets_recv is None:
                session.stats.media_packets_recv = 0
            session.stats.media_packets_recv += 1
            return session.audio_queue.popleft()

        assert self._lib is not None and self._ffi is not None
        c_pcm = self._ffi.new("int16_t[]", frame_samples)
        rc = self._lib.tc_engine_pull_audio_frame(session.handle, c_pcm, frame_samples)
        if int(rc) <= 0:
            return None
        byte_count = int(rc) * 2
        if session.stats.media_packets_recv is None:
            session.stats.media_packets_recv = 0
        session.stats.media_packets_recv += 1
        return bytes(self._ffi.buffer(c_pcm, byte_count))

    def _select_endpoints(self, endpoints: list[EndpointPayload]) -> list[EndpointPayload]:
        selected: list[EndpointPayload] = []
        for endpoint in endpoints:
            flags = int(endpoint.get("flags", 0))
            is_p2p_only = bool(flags & ENDPOINT_FLAG_P2P) and not bool(flags & ENDPOINT_FLAG_RELAY)
            if is_p2p_only and not self._allow_p2p:
                continue
            selected.append(dict(endpoint))

        if not selected and endpoints:
            # Fallback for deployments that provide only P2P/STUN candidates.
            selected = [dict(endpoint) for endpoint in endpoints]

        if not selected:
            return []

        if self._relay_preferred:
            selected.sort(
                key=lambda item: (
                    0 if int(item.get("flags", 0)) & ENDPOINT_FLAG_RELAY else 1,
                    int(item.get("priority", 100)),
                    int(item.get("id", 0)),
                )
            )
        else:
            selected.sort(
                key=lambda item: (
                    int(item.get("priority", 100)),
                    int(item.get("id", 0)),
                )
            )
        return selected

    def _normalize_rtc_servers(self, rtc_servers: list[RtcServerPayload]) -> list[RtcServerPayload]:
        normalized: list[RtcServerPayload] = []
        seen: set[tuple[str, int, str, bool, bool]] = set()
        for item in rtc_servers:
            host = str(item.get("host", "")).strip()
            if not host:
                continue
            port = int(item.get("port", 0))
            if port <= 0 or port > 65535:
                continue
            username = str(item.get("username", ""))
            password = str(item.get("password", ""))
            is_turn = bool(item.get("is_turn", False))
            is_tcp = bool(item.get("is_tcp", False))
            key = (host, port, username, is_turn, is_tcp)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "host": host,
                    "port": port,
                    "username": username,
                    "password": password,
                    "is_turn": is_turn,
                    "is_tcp": is_tcp,
                }
            )
        return normalized

    def _apply_rtc_servers(self, session: _NativeSession) -> bool:
        if not self._has_rtc_server_api:
            # Legacy backend does not expose rtc server API.
            return self._backend != NATIVE_BACKEND_TGCALLS
        if session.handle is None:
            return False
        assert self._ffi is not None and self._lib is not None
        c_items = self._ffi.new("tc_rtc_server_t[]", len(session.rtc_servers))
        session.c_rtc_server_buffers.clear()
        for idx, item in enumerate(session.rtc_servers):
            host = str(item.get("host", ""))
            username = str(item.get("username", ""))
            password = str(item.get("password", ""))
            c_host = self._ffi.new("char[]", host.encode("utf-8"))
            c_user = self._ffi.new("char[]", username.encode("utf-8"))
            c_pass = self._ffi.new("char[]", password.encode("utf-8"))
            c_items[idx].host = c_host
            c_items[idx].port = int(item.get("port", 0))
            c_items[idx].username = c_user
            c_items[idx].password = c_pass
            c_items[idx].is_turn = 1 if bool(item.get("is_turn", False)) else 0
            c_items[idx].is_tcp = 1 if bool(item.get("is_tcp", False)) else 0
            session.c_rtc_server_buffers.extend([c_host, c_user, c_pass])

        rc = self._lib.tc_engine_set_rtc_servers(
            session.handle,
            c_items,
            len(session.rtc_servers),
        )
        return int(rc) == 0
