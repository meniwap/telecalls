from __future__ import annotations

import importlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .stats import CallStats

ENDPOINT_FLAG_RELAY = 1 << 0
ENDPOINT_FLAG_P2P = 1 << 1
ENDPOINT_FLAG_TCP = 1 << 2
ENDPOINT_FLAG_TURN = 1 << 3

EndpointPayload = dict[str, str | int | bytes]


@dataclass(slots=True)
class _NativeSession:
    call_id: int
    incoming: bool
    video: bool
    handle: Any | None = None
    mock_queue: deque[bytes] = field(default_factory=deque)
    stats: CallStats = field(default_factory=CallStats)
    muted: bool = False
    bitrate_hint_kbps: int = 24
    c_endpoint_buffers: list[Any] = field(default_factory=list)
    audio_queue: deque[bytes] = field(default_factory=deque)


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
    ) -> None:
        self._enabled = bool(enabled)
        self._test_mode = bool(test_mode)
        self._allow_p2p = bool(allow_p2p)
        self._relay_preferred = bool(relay_preferred)
        self._sessions: dict[int, _NativeSession] = {}
        self._ffi: Any | None = None
        self._lib: Any | None = None
        self._available = False

        if self._enabled:
            try:
                engine_module = importlib.import_module("telecraft.client.calls._engine_cffi")

                self._ffi = engine_module.ffi
                self._lib = engine_module.lib
                self._available = True
            except Exception:
                self._available = False

    def set_allow_p2p(self, value: bool) -> None:
        self._allow_p2p = bool(value)

    def ensure_session(self, *, call_id: int, incoming: bool, video: bool) -> None:
        cid = int(call_id)
        if cid in self._sessions:
            return
        session = _NativeSession(call_id=cid, incoming=bool(incoming), video=bool(video))
        self._sessions[cid] = session
        if not self._enabled or not self._available:
            return
        assert self._ffi is not None and self._lib is not None
        params = self._ffi.new("tc_engine_params_t *")
        params.call_id = cid
        params.incoming = 1 if incoming else 0
        params.video = 1 if video else 0
        params.user_data = self._ffi.NULL
        params.on_state = self._ffi.NULL
        params.on_error = self._ffi.NULL
        params.on_signaling = self._ffi.NULL
        handle = self._lib.tc_engine_create(params)
        if handle == self._ffi.NULL:
            return
        session.handle = handle
        self._lib.tc_engine_start(handle)

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

    def set_keys(self, call_id: int, auth_key: bytes, key_fingerprint: int) -> bool:
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
        )
        return int(rc) == 0

    def set_remote_endpoints(self, call_id: int, endpoints: list[EndpointPayload]) -> bool:
        session = self._sessions.get(int(call_id))
        if session is None:
            return False
        selected = self._select_endpoints(endpoints)
        if not selected:
            return False
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

    def push_signaling(self, call_id: int, data: bytes) -> None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return

        payload = bytes(data)
        now = time.monotonic()
        session.stats.updated_at = now
        session.stats.bitrate_kbps = max(1.0, (len(payload) * 8.0) / 1000.0)
        session.stats.loss = 0.0
        session.stats.jitter_ms = 2.0

        if not self._enabled or not self._available or session.handle is None:
            if self._test_mode:
                session.mock_queue.append(payload)
                session.stats.rtt_ms = 50.0
            return

        assert self._lib is not None and self._ffi is not None
        buf = self._ffi.new("uint8_t[]", payload)
        rc = self._lib.tc_engine_push_signaling(session.handle, buf, len(payload))
        if int(rc) != 0:
            return

    def pull_signaling(self, call_id: int) -> bytes | None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return None
        if not self._enabled or not self._available or session.handle is None:
            if not self._test_mode or not session.mock_queue:
                return None
            return session.mock_queue.popleft()

        assert self._lib is not None and self._ffi is not None
        out_cap = 16 * 1024
        out = self._ffi.new("uint8_t[]", out_cap)
        result = self._lib.tc_engine_pull_signaling(session.handle, out, out_cap)
        if int(result) <= 0:
            return None
        return bytes(self._ffi.buffer(out, int(result)))

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
            updated_at=time.monotonic(),
        )

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
                while len(session.audio_queue) > 64:
                    session.audio_queue.popleft()
                return True
            return False

        assert self._lib is not None and self._ffi is not None
        frame_samples = len(payload) // 2
        c_pcm = self._ffi.new("int16_t[]", frame_samples)
        self._ffi.memmove(c_pcm, payload, len(payload))
        rc = self._lib.tc_engine_push_audio_frame(session.handle, c_pcm, frame_samples)
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
            return session.audio_queue.popleft()

        assert self._lib is not None and self._ffi is not None
        c_pcm = self._ffi.new("int16_t[]", frame_samples)
        rc = self._lib.tc_engine_pull_audio_frame(session.handle, c_pcm, frame_samples)
        if int(rc) <= 0:
            return None
        byte_count = int(rc) * 2
        return bytes(self._ffi.buffer(c_pcm, byte_count))

    def _select_endpoints(self, endpoints: list[EndpointPayload]) -> list[EndpointPayload]:
        selected: list[EndpointPayload] = []
        for endpoint in endpoints:
            flags = int(endpoint.get("flags", 0))
            is_p2p_only = bool(flags & ENDPOINT_FLAG_P2P) and not bool(flags & ENDPOINT_FLAG_RELAY)
            if is_p2p_only and not self._allow_p2p:
                continue
            selected.append(dict(endpoint))

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
