from __future__ import annotations

from telecraft.tl.codec import TLReader, TLWriter, dumps, loads
from telecraft.tl.generated.types import InputPeerUser


def test_bytes_roundtrip() -> None:
    w = TLWriter()
    w.write_bytes(b"abc")
    r = TLReader(w.to_bytes())
    assert r.read_bytes() == b"abc"


def test_string_roundtrip() -> None:
    w = TLWriter()
    w.write_string("שלום")
    r = TLReader(w.to_bytes())
    assert r.read_string().decode("utf-8") == "שלום"


def test_vector_roundtrip_ints() -> None:
    w = TLWriter()
    w.write_value("Vector<int>", [1, 2, 3])
    r = TLReader(w.to_bytes())
    assert r.read_value("Vector<int>") == [1, 2, 3]


def test_int128_int256_roundtrip() -> None:
    w = TLWriter()
    v128 = b"\x01" * 16
    v256 = b"\x02" * 32
    w.write_value("int128", v128)
    w.write_value("int256", v256)
    r = TLReader(w.to_bytes())
    assert r.read_value("int128") == v128
    assert r.read_value("int256") == v256


def test_object_roundtrip_input_peer_user() -> None:
    obj = InputPeerUser(user_id=123, access_hash=456)
    data = dumps(obj)
    out = loads(data)
    assert isinstance(out, InputPeerUser)
    assert out.user_id == 123
    assert out.access_hash == 456
