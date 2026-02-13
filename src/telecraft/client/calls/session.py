from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .crypto import CallCryptoContext
from .state import TERMINAL_CALL_STATES, CallEndReason, CallState, assert_transition
from .stats import CallStats
from .types import PhoneCallRef

StateHandler = Callable[[CallState], None]
ErrorHandler = Callable[[Exception], None]
SignalingHandler = Callable[[bytes], None]
StatsHandler = Callable[[CallStats], None]


@dataclass(slots=True)
class CallSession:
    call_id: int
    access_hash: int
    incoming: bool
    manager: Any
    video: bool = False
    state: CallState = CallState.IDLE
    end_reason: CallEndReason | None = None
    retries: dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    connected_at: float | None = None
    ended_at: float | None = None
    muted: bool = False
    crypto: CallCryptoContext | None = None
    timers: dict[str, float] = field(default_factory=dict)
    last_error: Exception | None = None
    native_key_attached: bool = False
    last_remote_event: str | None = None
    audio_backend: Any | None = None
    _stats: CallStats = field(default_factory=CallStats)
    _signaling_seen_order: deque[bytes] = field(default_factory=deque)
    _signaling_seen_set: set[bytes] = field(default_factory=set)

    _state_handlers: list[StateHandler] = field(default_factory=list)
    _error_handlers: list[ErrorHandler] = field(default_factory=list)
    _signaling_handlers: list[SignalingHandler] = field(default_factory=list)
    _stats_handlers: list[StatsHandler] = field(default_factory=list)

    @property
    def ref(self) -> PhoneCallRef:
        return PhoneCallRef.from_parts(self.call_id, self.access_hash)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_CALL_STATES

    def on_state_change(self, handler: StateHandler) -> None:
        self._state_handlers.append(handler)

    def on_error(self, handler: ErrorHandler) -> None:
        self._error_handlers.append(handler)

    def on_signaling_data(self, handler: SignalingHandler) -> None:
        self._signaling_handlers.append(handler)

    def on_stats(self, handler: StatsHandler) -> None:
        self._stats_handlers.append(handler)

    async def accept(self) -> None:
        await self.manager.accept(self)

    async def reject(self) -> None:
        await self.manager.reject(self)

    async def hangup(self) -> None:
        await self.manager.hangup(self)

    async def mute(self, muted: bool) -> None:
        self.muted = bool(muted)
        await self.manager.mute(self, self.muted)

    def stats(self) -> dict[str, float | None]:
        return self._stats.as_dict()

    def _set_timer_deadline(self, name: str, *, timeout: float) -> None:
        self.timers[str(name)] = time.monotonic() + max(timeout, 0.0)

    def _clear_timer_deadline(self, name: str) -> None:
        self.timers.pop(str(name), None)

    def _transition(self, to_state: CallState) -> None:
        assert_transition(self.state, to_state)
        self.state = to_state
        now = time.monotonic()
        if to_state == CallState.IN_CALL and self.connected_at is None:
            self.connected_at = now
        if to_state in TERMINAL_CALL_STATES:
            self.ended_at = now
        for handler in list(self._state_handlers):
            try:
                handler(to_state)
            except Exception:
                continue

    def _set_failed(
        self,
        exc: Exception,
        reason: CallEndReason = CallEndReason.FAILED_INTERNAL,
    ) -> None:
        self.last_error = exc
        if self.state not in TERMINAL_CALL_STATES:
            self.end_reason = reason
            self.state = CallState.FAILED
            self.ended_at = time.monotonic()
            for state_handler in list(self._state_handlers):
                try:
                    state_handler(self.state)
                except Exception:
                    continue
        for error_handler in list(self._error_handlers):
            try:
                error_handler(exc)
            except Exception:
                continue

    def _emit_signaling_data(self, data: bytes) -> None:
        for handler in list(self._signaling_handlers):
            try:
                handler(data)
            except Exception:
                continue

    def _set_stats(self, stats: CallStats) -> None:
        self._stats = stats
        for handler in list(self._stats_handlers):
            try:
                handler(stats)
            except Exception:
                continue

    def _remember_remote_event(self, event_name: str) -> None:
        self.last_remote_event = str(event_name)

    def _is_duplicate_signaling(self, digest: bytes, *, max_history: int) -> bool:
        if digest in self._signaling_seen_set:
            return True

        self._signaling_seen_order.append(digest)
        self._signaling_seen_set.add(digest)

        while len(self._signaling_seen_order) > max(1, int(max_history)):
            evicted = self._signaling_seen_order.popleft()
            self._signaling_seen_set.discard(evicted)
        return False

    def _clear_signaling_history(self) -> None:
        self._signaling_seen_order.clear()
        self._signaling_seen_set.clear()
