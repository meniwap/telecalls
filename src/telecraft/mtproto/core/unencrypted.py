from __future__ import annotations

import struct
from dataclasses import dataclass


class UnencryptedMessageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UnencryptedMessage:
    msg_id: int
    body: bytes  # TL-serialized payload (e.g. ReqPqMulti)

    def pack(self) -> bytes:
        # auth_key_id=0 (8 bytes), then msg_id (8), then length (4), then body
        if self.msg_id % 4 != 0:
            raise UnencryptedMessageError("msg_id must be divisible by 4")
        if len(self.body) % 4 != 0:
            # TL objects are aligned to 4 (but we keep the check explicit)
            raise UnencryptedMessageError("body length must be divisible by 4")
        return (
            struct.pack("<q", 0)
            + struct.pack("<q", int(self.msg_id))
            + struct.pack("<i", len(self.body))
            + self.body
        )


def unpack_unencrypted(data: bytes) -> UnencryptedMessage:
    if len(data) < 8 + 8 + 4:
        raise UnencryptedMessageError("packet too small")
    auth_key_id = struct.unpack_from("<q", data, 0)[0]
    if auth_key_id != 0:
        raise UnencryptedMessageError("auth_key_id is not 0 (not an unencrypted packet)")
    msg_id = struct.unpack_from("<q", data, 8)[0]
    ln = struct.unpack_from("<i", data, 16)[0]
    if ln < 0:
        raise UnencryptedMessageError("negative message length")
    end = 20 + ln
    if end != len(data):
        raise UnencryptedMessageError("length mismatch")
    body = data[20:end]
    return UnencryptedMessage(msg_id=msg_id, body=body)
