from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from telecraft.core.bytes import xor_bytes


class AesIgeError(Exception):
    pass


def _aes_ecb_encrypt_block(key: bytes, block16: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(block16) + enc.finalize()


def _aes_ecb_decrypt_block(key: bytes, block16: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    return dec.update(block16) + dec.finalize()


@dataclass(frozen=True, slots=True)
class AesIge:
    """
    AES-256-IGE as used by MTProto.

    - key: 32 bytes
    - iv: 32 bytes (two 16-byte IV parts)
    - data length must be multiple of 16
    """

    key: bytes
    iv: bytes

    def __post_init__(self) -> None:
        if len(self.key) not in (16, 24, 32):
            raise AesIgeError("AES key must be 16/24/32 bytes")
        if len(self.iv) != 32:
            raise AesIgeError("IGE iv must be 32 bytes")

    def encrypt(self, plaintext: bytes) -> bytes:
        if len(plaintext) % 16 != 0:
            raise AesIgeError("Plaintext length must be multiple of 16")

        iv1 = self.iv[:16]
        iv2 = self.iv[16:]

        out = bytearray()
        prev_c = iv1
        prev_p = iv2
        for i in range(0, len(plaintext), 16):
            p = plaintext[i : i + 16]
            x = xor_bytes(p, prev_c)
            y = _aes_ecb_encrypt_block(self.key, x)
            c = xor_bytes(y, prev_p)
            out += c
            prev_c = c
            prev_p = p
        return bytes(out)

    def decrypt(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) % 16 != 0:
            raise AesIgeError("Ciphertext length must be multiple of 16")

        iv1 = self.iv[:16]
        iv2 = self.iv[16:]

        out = bytearray()
        prev_c = iv1
        prev_p = iv2
        for i in range(0, len(ciphertext), 16):
            c = ciphertext[i : i + 16]
            x = xor_bytes(c, prev_p)
            y = _aes_ecb_decrypt_block(self.key, x)
            p = xor_bytes(y, prev_c)
            out += p
            prev_p = p
            prev_c = c
        return bytes(out)
