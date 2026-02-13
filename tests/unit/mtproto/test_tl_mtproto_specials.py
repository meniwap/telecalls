from __future__ import annotations

import gzip
import struct

from telecraft.tl.codec import MsgContainer, RpcResult, dumps, loads
from telecraft.tl.generated.types import Pong


def _tl_bytes(data: bytes) -> bytes:
    ln = len(data)
    if ln < 254:
        out = bytes([ln]) + data
        out += b"\x00" * ((4 - ((1 + ln) % 4)) % 4)
        return out
    out = bytes([254]) + struct.pack("<I", ln)[:3] + data
    out += b"\x00" * ((4 - ((4 + ln) % 4)) % 4)
    return out


def test_parse_rpc_result() -> None:
    pong = Pong(msg_id=123, ping_id=456)
    payload = dumps(pong)
    rpc_result_cid = -212046591  # 0xF35C6D01
    data = struct.pack("<i", rpc_result_cid) + struct.pack("<q", 777) + payload
    obj = loads(data)
    assert isinstance(obj, RpcResult)
    assert obj.req_msg_id == 777
    assert isinstance(obj.result, Pong)


def test_parse_gzip_packed_unwraps() -> None:
    pong = Pong(msg_id=1, ping_id=2)
    inner = dumps(pong)
    packed = gzip.compress(inner)
    gzip_packed_cid = 812830625  # 0x3072CFA1
    data = struct.pack("<i", gzip_packed_cid) + _tl_bytes(packed)
    obj = loads(data)
    assert isinstance(obj, Pong)
    assert obj.ping_id == 2


def test_parse_msg_container() -> None:
    pong = Pong(msg_id=10, ping_id=20)
    rpc_result_cid = -212046591  # 0xF35C6D01
    inner_obj = struct.pack("<i", rpc_result_cid) + struct.pack("<q", 999) + dumps(pong)

    msg_container_cid = 1945237724  # 0x73F1F8DC
    msg_id = 1111
    seqno = 1
    data = (
        struct.pack("<i", msg_container_cid)
        + struct.pack("<i", 1)  # count
        + struct.pack("<q", msg_id)
        + struct.pack("<i", seqno)
        + struct.pack("<i", len(inner_obj))
        + inner_obj
    )

    obj = loads(data)
    assert isinstance(obj, MsgContainer)
    assert len(obj.messages) == 1
    assert obj.messages[0].msg_id == msg_id
    assert obj.messages[0].seqno == seqno
    assert isinstance(obj.messages[0].obj, RpcResult)
    assert isinstance(obj.messages[0].obj.result, Pong)
