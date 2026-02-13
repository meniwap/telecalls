from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TransportError(Exception):
    pass


class Framing(Protocol):
    """
    Transport framing is responsible only for:
    - turning raw MTProto packet bytes into framed bytes (encode)
    - reading framed bytes and extracting a raw packet (decode)
    """

    def encode(self, payload: bytes) -> bytes: ...
    def decode_from_buffer(self, buffer: bytearray) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
