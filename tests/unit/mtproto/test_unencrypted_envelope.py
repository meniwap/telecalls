from __future__ import annotations

import pytest

from telecraft.mtproto.core.unencrypted import UnencryptedMessage, unpack_unencrypted


def test_pack_unpack_unencrypted() -> None:
    msg = UnencryptedMessage(msg_id=4, body=b"\x00\x00\x00\x00")
    packed = msg.pack()
    out = unpack_unencrypted(packed)
    assert out.msg_id == 4
    assert out.body == b"\x00\x00\x00\x00"


def test_unpack_rejects_nonzero_auth_key_id() -> None:
    # auth_key_id=1, msg_id=4, len=4, body=0000
    bad = (
        (1).to_bytes(8, "little", signed=True)
        + (4).to_bytes(8, "little", signed=True)
        + (4).to_bytes(4, "little", signed=True)
        + b"\x00\x00\x00\x00"
    )
    with pytest.raises(Exception):
        unpack_unencrypted(bad)
