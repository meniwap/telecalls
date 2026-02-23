from __future__ import annotations

import pytest

from telecraft.client.calls.crypto_mtproto2 import (
    MtProto2VoipError,
    decrypt_short_packet,
    encrypt_short_packet,
    kdf2,
)


def test_kdf2_vectors_are_stable() -> None:
    auth_key = bytes(range(256))
    msg_key = bytes(range(16))

    aes_key_0, aes_iv_0 = kdf2(auth_key, msg_key, x=0)
    aes_key_8, aes_iv_8 = kdf2(auth_key, msg_key, x=8)

    assert aes_key_0.hex() == "704ed09c8b41668ae8f99d244738f71dbddc44469b6bbd4aa8573dd042bd059e"
    assert aes_iv_0.hex() == "4d266000a550edabbf4c7ce40fd0043cc92230184cd317a5cc9c2482fd3b9318"
    assert aes_key_8.hex() == "217725799b245806458174a1fcfbc883906807b15033fdd0ea2b4d69cf9c364e"
    assert aes_iv_8.hex() == "669a6538917a4fa56ca32360a431c9160be4ad887140980dab91ce7bdc47ffbc"


def test_short_packet_encrypt_decrypt_roundtrip_directional() -> None:
    auth_key = bytes(range(256))
    plain = b"hello-telecalls"

    encrypted = encrypt_short_packet(
        auth_key,
        plain,
        is_outgoing=True,
        padding=b"\x11" * 64,
    )

    assert (
        encrypted.hex()
        == "51fc2d95ce749d96ff56b6eac954e56b70941849d3243f7076becf7373a4a800"
        "141b5623fb2c36e71991682d218a937b6c88fd6649cc553187dc4efa1e677921"
    )

    decrypted = decrypt_short_packet(auth_key, encrypted, is_outgoing=False)
    assert decrypted == plain


def test_short_packet_rejects_wrong_direction() -> None:
    auth_key = bytes(range(256))
    encrypted = encrypt_short_packet(auth_key, b"abc", is_outgoing=True, padding=b"\x22" * 64)

    with pytest.raises(MtProto2VoipError):
        decrypt_short_packet(auth_key, encrypted, is_outgoing=True)
