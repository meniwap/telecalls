from __future__ import annotations

from hashlib import sha256
from secrets import token_bytes

from telecraft.mtproto.crypto.aes_ige import AesIge


class MtProto2VoipError(Exception):
    pass


def kdf2(auth_key: bytes, msg_key: bytes, *, x: int) -> tuple[bytes, bytes]:
    if len(msg_key) != 16:
        raise MtProto2VoipError("msg_key must be exactly 16 bytes")
    if x not in {0, 8}:
        raise MtProto2VoipError("x must be 0 or 8")
    if len(auth_key) < 128 + x:
        raise MtProto2VoipError("auth_key is too short for MTProto2 KDF2")

    sha256a = sha256(msg_key + auth_key[x : x + 36]).digest()
    sha256b = sha256(auth_key[40 + x : 76 + x] + msg_key).digest()

    aes_key = sha256a[:8] + sha256b[8:24] + sha256a[24:32]
    aes_iv = sha256b[:8] + sha256a[8:24] + sha256b[24:32]
    return aes_key, aes_iv


def encrypt_short_packet(
    auth_key: bytes,
    plain: bytes,
    *,
    is_outgoing: bool,
    padding: bytes | None = None,
) -> bytes:
    if len(plain) > 0xFFFF:
        raise MtProto2VoipError("plain payload too large for short format")

    x = 0 if is_outgoing else 8
    inner = len(plain).to_bytes(2, "little") + plain
    pad_len = 16 - (len(inner) % 16)
    if pad_len < 16:
        pad_len += 16

    if padding is None:
        pad = token_bytes(pad_len)
    else:
        if len(padding) < pad_len:
            raise MtProto2VoipError("provided padding is shorter than required")
        pad = padding[:pad_len]

    inner = inner + pad
    msg_key_large = sha256(auth_key[88 + x : 120 + x] + inner).digest()
    msg_key = msg_key_large[8:24]
    aes_key, aes_iv = kdf2(auth_key, msg_key, x=x)
    encrypted = AesIge(key=aes_key, iv=aes_iv).encrypt(inner)
    return msg_key + encrypted


def decrypt_short_packet(auth_key: bytes, encrypted: bytes, *, is_outgoing: bool) -> bytes:
    if len(encrypted) < 32 or (len(encrypted) - 16) % 16 != 0:
        raise MtProto2VoipError("encrypted short packet has invalid length")

    msg_key = encrypted[:16]
    cipher = encrypted[16:]
    x = 8 if is_outgoing else 0

    aes_key, aes_iv = kdf2(auth_key, msg_key, x=x)
    inner = AesIge(key=aes_key, iv=aes_iv).decrypt(cipher)

    msg_key_large = sha256(auth_key[88 + x : 120 + x] + inner).digest()
    if msg_key_large[8:24] != msg_key:
        if len(inner) <= 2:
            raise MtProto2VoipError("msg_key mismatch after decryption")
        # Compatibility path for peers hashing without the short-length prefix.
        msg_key_large_alt = sha256(auth_key[88 + x : 120 + x] + inner[2:]).digest()
        if msg_key_large_alt[8:24] != msg_key:
            raise MtProto2VoipError("msg_key mismatch after decryption")

    declared_len = int.from_bytes(inner[:2], "little")
    if declared_len > len(inner) - 2:
        raise MtProto2VoipError("declared payload length exceeds decrypted payload")
    if (len(inner) - 2 - declared_len) < 16:
        raise MtProto2VoipError("decrypted packet has too little padding")

    return inner[2 : 2 + declared_len]
