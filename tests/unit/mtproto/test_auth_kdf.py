from __future__ import annotations

from telecraft.mtproto.auth.kdf import new_nonce_hash, server_salt, tmp_aes_key_iv


def test_tmp_aes_key_iv_lengths() -> None:
    new_nonce = b"\x01" * 32
    server_nonce = b"\x02" * 16
    key, iv = tmp_aes_key_iv(new_nonce=new_nonce, server_nonce=server_nonce)
    assert len(key) == 32
    assert len(iv) == 32


def test_server_salt_length() -> None:
    new_nonce = b"\x01" * 32
    server_nonce = b"\x02" * 16
    salt = server_salt(new_nonce=new_nonce, server_nonce=server_nonce)
    assert len(salt) == 8


def test_new_nonce_hash_lengths() -> None:
    new_nonce = b"\x01" * 32
    auth_key = b"\xaa" * 256
    for n in (1, 2, 3):
        h = new_nonce_hash(new_nonce=new_nonce, auth_key=auth_key, number=n)
        assert len(h) == 16
