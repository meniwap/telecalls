from __future__ import annotations

import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from telecraft.mtproto.crypto.hashes import sha1
from telecraft.mtproto.crypto.rsa import (
    RsaPublicKey,
    fingerprint_from_der_spki,
    rsa_encrypt_raw,
    rsa_key_size_bytes,
)


def test_fingerprint_matches_sha1_tail_le() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    der = public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def tl_bytes(data: bytes) -> bytes:
        ln = len(data)
        if ln < 254:
            out = bytes([ln]) + data
            out += b"\x00" * ((4 - ((1 + ln) % 4)) % 4)
            return out
        out = bytes([254]) + struct.pack("<I", ln)[:3] + data
        out += b"\x00" * ((4 - ((4 + ln) % 4)) % 4)
        return out

    nums = public.public_numbers()
    n_bytes = nums.n.to_bytes((nums.n.bit_length() + 7) // 8 or 1, "big", signed=False)
    e_bytes = nums.e.to_bytes((nums.e.bit_length() + 7) // 8 or 1, "big", signed=False)
    digest = sha1(tl_bytes(n_bytes) + tl_bytes(e_bytes))
    expected = int.from_bytes(digest[-8:], "little", signed=True)
    assert fingerprint_from_der_spki(der) == expected


def test_encrypt_decrypt_pkcs1v15_roundtrip() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    der = public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    pub = RsaPublicKey(der_spki=der)
    plaintext = b"hello mtproto"
    ct = pub.encrypt(plaintext)
    pt = private.decrypt(ct, padding.PKCS1v15())
    assert pt == plaintext


def test_encrypt_raw_roundtrips_and_has_sha1_prefix() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    der = public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    data = b"hello mtproto raw"
    ct = rsa_encrypt_raw(der, data)
    assert len(ct) == rsa_key_size_bytes(der)

    priv = private.private_numbers()
    n = priv.public_numbers.n
    d = priv.d

    k = len(ct)
    m_int = pow(int.from_bytes(ct, "big", signed=False), d, n)
    m_bytes = m_int.to_bytes(k, "big", signed=False)

    # We padded to (k-1) bytes, so decrypted block should have a leading 0x00.
    assert m_bytes[0] == 0
    padded = m_bytes[1:]
    assert padded[:20] == sha1(data)
    assert padded[20 : 20 + len(data)] == data
