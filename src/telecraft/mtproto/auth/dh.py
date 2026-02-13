from __future__ import annotations

from dataclasses import dataclass

from telecraft.mtproto.crypto.hashes import sha1
from telecraft.mtproto.crypto.random import random_bytes


class DhError(Exception):
    pass


def _be_bytes_to_int(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


def _int_to_be_bytes(n: int) -> bytes:
    if n < 0:
        raise DhError("negative int")
    ln = (n.bit_length() + 7) // 8 or 1
    return n.to_bytes(ln, "big", signed=False)


def auth_key_id(auth_key: bytes) -> bytes:
    """
    auth_key_id = last 8 bytes of SHA1(auth_key).
    """

    return sha1(auth_key)[-8:]


@dataclass(frozen=True, slots=True)
class DhResult:
    auth_key: bytes
    auth_key_id: bytes  # 8 bytes
    g_b: bytes  # bytes to send in client_DH_inner_data.g_b


def make_dh_result(*, g: int, dh_prime: bytes, g_a: bytes) -> DhResult:
    """
    Compute auth_key and g_b given server parameters.

    This is the "client side" of DH:
    - choose random b
    - g_b = g^b mod dh_prime
    - auth_key = (g_a)^b mod dh_prime
    """

    if g <= 1:
        raise DhError("invalid g")
    p = _be_bytes_to_int(dh_prime)
    if p <= 0:
        raise DhError("invalid dh_prime")
    ga = _be_bytes_to_int(g_a)
    if ga <= 1 or ga >= p - 1:
        raise DhError("invalid g_a")

    # b should be random 256 bytes; using 256 bytes here.
    b = _be_bytes_to_int(random_bytes(256))
    gb_int = pow(g, b, p)
    auth_int = pow(ga, b, p)

    gb = _int_to_be_bytes(gb_int)
    auth = _int_to_be_bytes(auth_int)
    return DhResult(auth_key=auth, auth_key_id=auth_key_id(auth), g_b=gb)
