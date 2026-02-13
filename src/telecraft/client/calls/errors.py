from __future__ import annotations

from telecraft.client.calls.state import CallEndReason


class CallsError(Exception):
    pass


class CallsDisabledError(CallsError):
    pass


class CallStateError(CallsError):
    pass


class CallTimeoutError(CallsError):
    pass


class CallProtocolError(CallsError):
    pass


class CallMediaError(CallsError):
    pass


class CallInternalError(CallsError):
    pass


class SignalingDataError(CallsError):
    pass


def exception_to_reason(exc: Exception) -> CallEndReason:
    if isinstance(exc, CallTimeoutError):
        return CallEndReason.FAILED_TIMEOUT
    if isinstance(exc, CallProtocolError):
        return CallEndReason.FAILED_PROTOCOL
    if isinstance(exc, CallMediaError):
        return CallEndReason.FAILED_MEDIA
    return CallEndReason.FAILED_INTERNAL
