from __future__ import annotations

import pytest

from telecraft.mtproto.transport.abridged import AbridgedFraming
from telecraft.mtproto.transport.intermediate import IntermediateFraming


def test_abridged_encode_decode_small() -> None:
    f = AbridgedFraming()
    payload = b"\x01\x02\x03\x04" * 3  # 12 bytes => 3 words
    framed = f.encode(payload)
    assert framed[:1] == b"\x03"
    buf = bytearray(framed)
    out = f.decode_from_buffer(buf)
    assert out == payload
    assert buf == bytearray()


def test_abridged_encode_decode_large_header() -> None:
    f = AbridgedFraming()
    payload = b"\x00\x00\x00\x00" * 200  # 200 words => uses 0x7f + 3 bytes
    framed = f.encode(payload)
    assert framed[0] == 0x7F
    buf = bytearray(framed)
    out = f.decode_from_buffer(buf)
    assert out == payload
    assert buf == bytearray()


def test_abridged_requires_multiple_of_4() -> None:
    f = AbridgedFraming()
    with pytest.raises(Exception):
        f.encode(b"\x00")


def test_intermediate_encode_decode() -> None:
    f = IntermediateFraming()
    payload = b"\xaa\xbb\xcc\xdd" * 5
    framed = f.encode(payload)
    buf = bytearray(framed)
    out = f.decode_from_buffer(buf)
    assert out == payload
    assert buf == bytearray()


def test_intermediate_partial_buffer() -> None:
    f = IntermediateFraming()
    payload = b"\xaa\xbb\xcc\xdd" * 2
    framed = f.encode(payload)
    buf = bytearray(framed[:3])
    assert f.decode_from_buffer(buf) is None
    buf.extend(framed[3:])
    assert f.decode_from_buffer(buf) == payload
