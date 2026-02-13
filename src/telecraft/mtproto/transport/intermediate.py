from __future__ import annotations

import struct
from dataclasses import dataclass

from .base import TransportError


@dataclass(frozen=True, slots=True)
class IntermediateFraming:
    """
    MTProto transport: intermediate.

    On connect, client must send 0xeeeeeeee.

    Frame format:
    - 4-byte little-endian length in bytes (payload length)
    - followed by payload bytes

    Payload length must be a multiple of 4.
    """

    CONNECT_HEADER: bytes = b"\xee\xee\xee\xee"

    def encode(self, payload: bytes) -> bytes:
        if len(payload) % 4 != 0:
            raise TransportError(
                "Intermediate framing requires payload length multiple of 4 bytes."
            )
        if len(payload) >= 2**31:
            raise TransportError("Payload too large for intermediate framing.")
        return struct.pack("<i", len(payload)) + payload

    def decode_from_buffer(self, buffer: bytearray) -> bytes | None:
        if len(buffer) < 4:
            return None
        (ln,) = struct.unpack("<i", buffer[:4])
        if ln < 0:
            raise TransportError(f"Negative length in intermediate framing: {ln}")
        total = 4 + ln
        if len(buffer) < total:
            return None
        payload = bytes(buffer[4:total])
        del buffer[:total]
        if len(payload) % 4 != 0:
            raise TransportError("Intermediate payload length is not multiple of 4.")
        return payload
