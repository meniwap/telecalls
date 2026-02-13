from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from telecraft.mtproto.crypto.hashes import sha256
from telecraft.tl.generated.types import (
    AccountPassword,
    InputCheckPasswordSrp,
    PasswordKdfAlgoSha256Sha256Pbkdf2Hmacsha512iter100000Sha256ModPow,
)


class SrpError(Exception):
    pass


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    if len(a) != len(b):
        raise SrpError("xor length mismatch")
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def _int_to_be(x: int, length: int) -> bytes:
    if x < 0:
        raise SrpError("negative integer")
    return int(x).to_bytes(length, "big", signed=False)


def _be_to_int(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=False)


def _kdf_password_hash(
    password: str, *, salt1: bytes, salt2: bytes, iterations: int = 100_000
) -> bytes:
    """
    Telegram SRP password hashing for:
      passwordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow

    Ref: https://core.telegram.org/api/srp
    """

    pw = password.encode("utf-8")
    hpw = sha256(pw)

    # PH1 := H(H(password) | salt1 | H(password) | salt2)
    ph1 = sha256(hpw + salt1 + hpw + salt2)

    # PBKDF2-HMAC-SHA512(PH1, salt1, 100000)
    pbk = hashlib.pbkdf2_hmac("sha512", ph1, salt1, iterations, dklen=64)

    # PH2 := H(PBKDF2(...) | salt2)
    ph2 = sha256(pbk + salt2)
    return ph2


@dataclass(frozen=True, slots=True)
class SrpParams:
    srp_id: int
    g: int
    p: bytes
    salt1: bytes
    salt2: bytes
    srp_b: bytes


def _extract_srp_params(password_state: AccountPassword) -> SrpParams:
    if not bool(getattr(password_state, "has_password", False)):
        raise SrpError("Account has no 2FA password")

    algo = password_state.current_algo
    if not isinstance(algo, PasswordKdfAlgoSha256Sha256Pbkdf2Hmacsha512iter100000Sha256ModPow):
        algo_name = getattr(algo, "TL_NAME", type(algo).__name__)
        raise SrpError(f"Unsupported password KDF algorithm: {algo_name}")

    srp_b = password_state.srp_b
    srp_id = password_state.srp_id
    if not isinstance(srp_b, (bytes, bytearray)) or not isinstance(srp_id, int):
        raise SrpError("Missing SRP parameters (srp_b/srp_id)")

    salt1 = cast(bytes, algo.salt1)
    salt2 = cast(bytes, algo.salt2)
    g = int(cast(int, algo.g))
    p = cast(bytes, algo.p)

    if not isinstance(salt1, (bytes, bytearray)) or not isinstance(salt2, (bytes, bytearray)):
        raise SrpError("Invalid salts")
    if not isinstance(p, (bytes, bytearray)) or len(p) < 64:
        raise SrpError("Invalid prime p")
    if g <= 1:
        raise SrpError("Invalid generator g")

    return SrpParams(
        srp_id=int(srp_id),
        g=g,
        p=bytes(p),
        salt1=bytes(salt1),
        salt2=bytes(salt2),
        srp_b=bytes(srp_b),
    )


def make_input_check_password_srp(
    *,
    password: str,
    password_state: AccountPassword,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> InputCheckPasswordSrp:
    """
    Build InputCheckPasswordSRP for auth.checkPassword.
    """

    params = _extract_srp_params(password_state)
    p_bytes = params.p
    p_len = len(p_bytes)
    p = _be_to_int(p_bytes)
    g = int(params.g)

    # Normalize B length to p_len (Telegram expects fixed-size big-endian integers).
    b_bytes = params.srp_b
    if len(b_bytes) > p_len:
        raise SrpError("srp_b longer than p")
    b_bytes = b"\x00" * (p_len - len(b_bytes)) + b_bytes
    B = _be_to_int(b_bytes)
    if B <= 0 or B >= p:
        raise SrpError("Invalid srp_B value")

    g_bytes = _int_to_be(g, p_len)

    # k := H(p | g)
    k = _be_to_int(sha256(p_bytes + g_bytes))

    # x := int(PH2(password, salt1, salt2))
    ph2 = _kdf_password_hash(password, salt1=params.salt1, salt2=params.salt2)
    x = _be_to_int(ph2)

    # a random (256 bytes is fine; we'll mod by p via pow anyway)
    a_raw = random_bytes(256)
    a = _be_to_int(a_raw)
    A = pow(g, a, p)
    A_bytes = _int_to_be(A, p_len)

    # u := H(A | B)
    u = _be_to_int(sha256(A_bytes + b_bytes))
    if u == 0:
        raise SrpError("Invalid u=0")

    # v := g^x mod p
    v = pow(g, x, p)

    # S := (B - k*v) ^ (a + u*x) mod p
    base = (B - (k * v) % p) % p
    if base == 0:
        raise SrpError("Invalid SRP base=0")
    exp = a + u * x
    S = pow(base, exp, p)
    S_bytes = _int_to_be(S, p_len)

    # K := H(S)
    K = sha256(S_bytes)

    # M1 := H(H(p) xor H(g) | H(salt1) | H(salt2) | A | B | K)
    hp = sha256(p_bytes)
    hg = sha256(g_bytes)
    m1 = sha256(
        _xor_bytes(hp, hg) + sha256(params.salt1) + sha256(params.salt2) + A_bytes + b_bytes + K
    )

    return InputCheckPasswordSrp(srp_id=params.srp_id, a=A_bytes, m1=m1)
