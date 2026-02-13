from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from telecraft.mtproto.auth.handshake import decrypt_server_dh_inner, rsa_encrypt_inner_data
from telecraft.mtproto.auth.kdf import tmp_aes_key_iv
from telecraft.mtproto.crypto.aes_ige import AesIge
from telecraft.mtproto.crypto.hashes import sha1
from telecraft.mtproto.crypto.rsa import RsaPublicKey
from telecraft.tl.codec import dumps
from telecraft.tl.generated.types import PQInnerData, ServerDhInnerData, ServerDhParamsOk


def test_decrypt_server_dh_inner_strips_sha1_prefix() -> None:
    new_nonce = b"\x11" * 32
    nonce = b"\x01" * 16
    server_nonce = b"\x02" * 16

    inner = ServerDhInnerData(
        nonce=nonce,
        server_nonce=server_nonce,
        g=3,
        dh_prime=b"\x03" * 64,
        g_a=b"\x04" * 64,
        server_time=123456,
    )

    data = dumps(inner)
    plaintext = sha1(data) + data
    pad_len = (-len(plaintext)) % 16
    plaintext += b"\x00" * pad_len

    key, iv = tmp_aes_key_iv(new_nonce=new_nonce, server_nonce=server_nonce)
    enc = AesIge(key=key, iv=iv).encrypt(plaintext)

    server_dh = ServerDhParamsOk(
        nonce=nonce,
        server_nonce=server_nonce,
        encrypted_answer=enc,
    )

    out = decrypt_server_dh_inner(server_dh, new_nonce=new_nonce)
    assert out == inner


def test_rsa_encrypt_inner_data_uses_mtproto_raw_padding() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = private.public_key().public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    key = RsaPublicKey(der_spki=der)
    inner = PQInnerData(
        pq=b"\x01\x43",
        p=b"\x11",
        q=b"\x13",
        nonce=b"\x01" * 16,
        server_nonce=b"\x02" * 16,
        new_nonce=b"\x03" * 32,
    )

    ct = rsa_encrypt_inner_data(inner, key)
    assert len(ct) == key.key_size_bytes

    priv = private.private_numbers()
    n = priv.public_numbers.n
    d = priv.d
    k = len(ct)

    m_int = pow(int.from_bytes(ct, "big", signed=False), d, n)
    m_bytes = m_int.to_bytes(k, "big", signed=False)

    assert m_bytes[0] == 0
    padded = m_bytes[1:]

    inner_bytes = dumps(inner)
    assert padded[:20] == sha1(inner_bytes)
    assert padded[20 : 20 + len(inner_bytes)] == inner_bytes
