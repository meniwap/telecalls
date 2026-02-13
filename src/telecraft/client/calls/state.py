from __future__ import annotations

from enum import Enum


class CallState(str, Enum):
    IDLE = "IDLE"
    RINGING_IN = "RINGING_IN"
    OUTGOING_INIT = "OUTGOING_INIT"
    CONNECTING = "CONNECTING"
    IN_CALL = "IN_CALL"
    DISCONNECTING = "DISCONNECTING"
    ENDED = "ENDED"
    FAILED = "FAILED"


class CallEndReason(str, Enum):
    LOCAL_HANGUP = "LOCAL_HANGUP"
    REMOTE_HANGUP = "REMOTE_HANGUP"
    REJECTED = "REJECTED"
    BUSY = "BUSY"
    MISSED = "MISSED"
    FAILED_TIMEOUT = "FAILED_TIMEOUT"
    FAILED_PROTOCOL = "FAILED_PROTOCOL"
    FAILED_MEDIA = "FAILED_MEDIA"
    FAILED_INTERNAL = "FAILED_INTERNAL"


_ALLOWED_TRANSITIONS: dict[CallState, set[CallState]] = {
    CallState.IDLE: {CallState.RINGING_IN, CallState.OUTGOING_INIT, CallState.FAILED},
    CallState.RINGING_IN: {
        CallState.CONNECTING,
        CallState.DISCONNECTING,
        CallState.ENDED,
        CallState.FAILED,
    },
    CallState.OUTGOING_INIT: {
        CallState.CONNECTING,
        CallState.DISCONNECTING,
        CallState.ENDED,
        CallState.FAILED,
    },
    CallState.CONNECTING: {
        CallState.IN_CALL,
        CallState.DISCONNECTING,
        CallState.ENDED,
        CallState.FAILED,
    },
    CallState.IN_CALL: {CallState.DISCONNECTING, CallState.ENDED, CallState.FAILED},
    CallState.DISCONNECTING: {CallState.ENDED, CallState.FAILED},
    CallState.ENDED: set(),
    CallState.FAILED: set(),
}


def can_transition(from_state: CallState, to_state: CallState) -> bool:
    if from_state == to_state:
        return True
    return to_state in _ALLOWED_TRANSITIONS[from_state]


def assert_transition(from_state: CallState, to_state: CallState) -> None:
    if not can_transition(from_state, to_state):
        raise ValueError(f"invalid call state transition: {from_state.value} -> {to_state.value}")
