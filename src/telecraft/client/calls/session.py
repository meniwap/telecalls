from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .state import CallEndReason, CallState, assert_transition
from .types import PhoneCallRef

StateHandler = Callable[[CallState], None]
ErrorHandler = Callable[[Exception], None]
SignalingHandler = Callable[[bytes], None]


@dataclass(slots=True)
class CallSession:
    call_id: int
    access_hash: int
    incoming: bool
    manager: Any
    state: CallState = CallState.IDLE
    end_reason: CallEndReason | None = None
    retries: int = 0
    created_at: float = field(default_factory=time.monotonic)
    connected_at: float | None = None
    ended_at: float | None = None

    _state_handlers: list[StateHandler] = field(default_factory=list)
    _error_handlers: list[ErrorHandler] = field(default_factory=list)
    _signaling_handlers: list[SignalingHandler] = field(default_factory=list)

    @property
    def ref(self) -> PhoneCallRef:
        return PhoneCallRef.from_parts(self.call_id, self.access_hash)

    def on_state_change(self, handler: StateHandler) -> None:
        self._state_handlers.append(handler)

    def on_error(self, handler: ErrorHandler) -> None:
        self._error_handlers.append(handler)

    def on_signaling_data(self, handler: SignalingHandler) -> None:
        self._signaling_handlers.append(handler)

    async def accept(self) -> None:
        await self.manager.accept(self)

    async def reject(self) -> None:
        await self.manager.reject(self)

    async def hangup(self) -> None:
        await self.manager.hangup(self)

    def _transition(self, to_state: CallState) -> None:
        assert_transition(self.state, to_state)
        self.state = to_state
        if to_state == CallState.IN_CALL and self.connected_at is None:
            self.connected_at = time.monotonic()
        if to_state in {CallState.ENDED, CallState.FAILED}:
            self.ended_at = time.monotonic()
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
        if self.state not in {CallState.ENDED, CallState.FAILED}:
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
