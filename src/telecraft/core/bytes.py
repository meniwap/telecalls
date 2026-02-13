from __future__ import annotations

import struct


class BytesError(Exception):
    pass


def xor_bytes(a: bytes, b: bytes) -> bytes:
    if len(a) != len(b):
        raise BytesError("xor_bytes requires equal-length inputs")
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def pad_to_multiple(data: bytes, multiple: int, pad: bytes = b"\x00") -> bytes:
    if multiple <= 0:
        raise BytesError("multiple must be > 0")
    if len(pad) != 1:
        raise BytesError("pad must be a single byte")
    rem = len(data) % multiple
    if rem == 0:
        return data
    return data + pad * (multiple - rem)


def read_int_le(data: bytes, offset: int = 0) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise BytesError("read_int_le out of bounds")
    return int(struct.unpack_from("<i", data, offset)[0])


def read_uint_le(data: bytes, offset: int = 0) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise BytesError("read_uint_le out of bounds")
    return int(struct.unpack_from("<I", data, offset)[0])


def read_long_le(data: bytes, offset: int = 0) -> int:
    if offset < 0 or offset + 8 > len(data):
        raise BytesError("read_long_le out of bounds")
    return int(struct.unpack_from("<q", data, offset)[0])


def write_int_le(value: int) -> bytes:
    return struct.pack("<i", int(value))


def write_uint_le(value: int) -> bytes:
    return struct.pack("<I", int(value))


def write_long_le(value: int) -> bytes:
    return struct.pack("<q", int(value))
