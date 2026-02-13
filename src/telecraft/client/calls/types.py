from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telecraft.tl.generated.types import InputPhoneCall, PhoneCallProtocol


@dataclass(frozen=True, slots=True)
class PhoneCallRef:
    call_id: int
    access_hash: int

    @classmethod
    def from_parts(cls, call_id: int, access_hash: int) -> PhoneCallRef:
        return cls(call_id=int(call_id), access_hash=int(access_hash))


def build_input_phone_call(ref: PhoneCallRef | Any) -> Any:
    if isinstance(ref, PhoneCallRef):
        return InputPhoneCall(id=int(ref.call_id), access_hash=int(ref.access_hash))
    return ref


def default_protocol() -> PhoneCallProtocol:
    return PhoneCallProtocol(
        flags=0,
        udp_p2p=True,
        udp_reflector=True,
        min_layer=0,
        max_layer=0,
        library_versions=["telecalls-signaling"],
    )
