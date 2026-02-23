from __future__ import annotations

import pytest

from telecraft.client.calls.crypto_mtproto2 import (
    MtProto2VoipError,
    decrypt_short_packet,
    encrypt_short_packet,
)


def test_mtproto2_short_packet_vector_stable() -> None:
    auth_key = bytes(range(256))
    plain = b"interop-vector"
    encrypted = encrypt_short_packet(
        auth_key,
        plain,
        is_outgoing=False,
        padding=b"\xAB" * 64,
    )
    assert (
        encrypted.hex()
        == "b10973e55181c51e80620c3ad846cae5326ce18aba9651eb69b98a80013a61b4"
        "5c25b0aef1ac136daa3c19c3368746be"
    )
    assert decrypt_short_packet(auth_key, encrypted, is_outgoing=True) == plain


def test_mtproto2_short_packet_wrong_direction_rejected() -> None:
    auth_key = bytes(range(256))
    encrypted = encrypt_short_packet(
        auth_key,
        b"hello",
        is_outgoing=False,
        padding=b"\xCD" * 64,
    )
    with pytest.raises(MtProto2VoipError):
        _ = decrypt_short_packet(auth_key, encrypted, is_outgoing=False)
