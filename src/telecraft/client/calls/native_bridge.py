from __future__ import annotations

import importlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .stats import CallStats

EndpointPayload = dict[str, str | int | bytes]


@dataclass(slots=True)
class _NativeSession:
    call_id: int
    incoming: bool
    video: bool
    handle: Any | None = None
    mock_queue: deque[bytes] = field(default_factory=deque)
    stats: CallStats = field(default_factory=CallStats)


class NativeBridge:
    """
    Thin bridge for the optional native engine.

    Behavior:
    - `enabled=False`: no-op bridge, but state is tracked in-memory.
    - `enabled=True` and native module available: delegate to C engine ABI.
    - `enabled=True` and native module missing: fall back to no-op mock mode.
    """

    def __init__(self, *, enabled: bool, test_mode: bool) -> None:
        self._enabled = bool(enabled)
        self._test_mode = bool(test_mode)
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

    def set_keys(self, call_id: int, auth_key: bytes, key_fingerprint: int) -> None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return
        if not self._enabled or not self._available or session.handle is None:
            return
        assert self._lib is not None and self._ffi is not None
        key_buf = self._ffi.new("uint8_t[]", auth_key)
        self._lib.tc_engine_set_keys(
            session.handle,
            key_buf,
            len(auth_key),
            int(key_fingerprint),
        )

    def set_remote_endpoints(self, call_id: int, endpoints: list[EndpointPayload]) -> None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return
        if not self._enabled or not self._available or session.handle is None:
            return
        if not endpoints:
            return
        assert self._lib is not None and self._ffi is not None

        c_items = self._ffi.new("tc_endpoint_t[]", len(endpoints))
        for idx, endpoint in enumerate(endpoints):
            c_items[idx].id = int(endpoint.get("id", 0))
            ip = str(endpoint.get("ip", ""))
            ipv6 = str(endpoint.get("ipv6", ""))
            port = int(endpoint.get("port", 0))
            peer_tag = endpoint.get("peer_tag", b"")
            if not isinstance(peer_tag, (bytes, bytearray)):
                peer_tag = b""
            c_items[idx].ip = self._ffi.new("char[]", ip.encode("utf-8"))
            c_items[idx].ipv6 = self._ffi.new("char[]", ipv6.encode("utf-8"))
            c_items[idx].port = port
            c_items[idx].peer_tag = self._ffi.new("uint8_t[]", bytes(peer_tag))
            c_items[idx].peer_tag_len = len(peer_tag)

        self._lib.tc_engine_set_remote_endpoints(session.handle, c_items, len(endpoints))

    def push_signaling(self, call_id: int, data: bytes) -> None:
        session = self._sessions.get(int(call_id))
        if session is None:
            return

        payload = bytes(data)
        now = time.monotonic()
        session.stats.updated_at = now
        session.stats.bitrate_kbps = max(1.0, (len(payload) * 8.0) / 1000.0)
        session.stats.rtt_ms = 50.0
        session.stats.loss = 0.0
        session.stats.jitter_ms = 2.0

        if not self._enabled or not self._available or session.handle is None:
            if self._test_mode:
                session.mock_queue.append(payload)
            return

        assert self._lib is not None and self._ffi is not None
        buf = self._ffi.new("uint8_t[]", payload)
        self._lib.tc_engine_push_signaling(session.handle, buf, len(payload))

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
        if result <= 0:
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
        if rc != 0:
            return None
        return CallStats(
            rtt_ms=float(out.rtt_ms),
            loss=float(out.loss),
            bitrate_kbps=float(out.bitrate_kbps),
            jitter_ms=float(out.jitter_ms),
            updated_at=time.monotonic(),
        )
