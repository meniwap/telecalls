from __future__ import annotations

import secrets
from dataclasses import dataclass
from functools import lru_cache

from telecraft.mtproto.crypto.hashes import sha1, sha256


class CallCryptoError(Exception):
    pass


# Telegram "good prime" used for DH in MTProto/calls.
_DEFAULT_DH_PRIME_HEX = (
    "C71CAEB9C6B1C9048E6C522F70F13F73980D40238E3E21C14934D037"
    "563D930F48198A0AA7C14058229493D22530F4DBFA336F6E0AC92513"
    "9543AED44CCE7C3720FD51F69458705AC68CD4FE6B6B13ABDC974651"
    "2969328454F18FAF8C595F642477FE96BB2A941D5BCD1D4AC8CC4988"
    "0708FA9B378E3C4F3A9060BEE67CF9A4A4A695811051907E162753B5"
    "6B0F6B410DBA74D8A84B2A14B3144E0EF1284754FD17ED950D5965B4"
    "B9DD46582DB1178D169C6BC465B0D6FF9CA3928FEF5B9AE4E418FC15"
    "E83EBEA0F87FA9FF5EED70050DED2849F47BF959D956850CE929851F"
    "0D8115F635B105EE2E4E15D04B2454BF6F4FADF034B10403119CD8E3"
    "B92FCC5B"
)

_VALID_GENERATORS: set[int] = {2, 3, 4, 5, 7}
_MILLER_RABIN_BASES: tuple[int, ...] = (2, 3, 5, 7, 11, 13, 17)
_SMALL_PRIMES: tuple[int, ...] = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29)


def _to_int(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


def _to_be(data: int, *, size: int) -> bytes:
    if data < 0:
        raise CallCryptoError("negative integer")
    return int(data).to_bytes(size, "big", signed=False)


def _decompose_for_miller_rabin(n: int) -> tuple[int, int]:
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    return d, r


def _is_probable_prime(n: int) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n == p:
            return True
        if n % p == 0:
            return False
    d, r = _decompose_for_miller_rabin(n)
    for a in _MILLER_RABIN_BASES:
        if a >= n:
            continue
        x = pow(a, d, n)
        if x in {1, n - 1}:
            continue
        witness = True
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                witness = False
                break
        if witness:
            return False
    return True


@lru_cache(maxsize=8)
def _is_valid_prime_group(prime_hex: str) -> bool:
    try:
        p = int(prime_hex, 16)
    except ValueError:
        return False
    if p <= 0 or p.bit_length() < 2048:
        return False
    if not _is_probable_prime(p):
        return False
    q = (p - 1) // 2
    return _is_probable_prime(q)


@dataclass(frozen=True, slots=True)
class CallCryptoProfile:
    g: int
    dh_prime: bytes

    @property
    def p(self) -> int:
        return _to_int(self.dh_prime)

    @property
    def p_size(self) -> int:
        return len(self.dh_prime)

    def validate(self) -> None:
        if self.g not in _VALID_GENERATORS:
            raise CallCryptoError(f"unsupported DH generator: {self.g}")
        prime_hex = self.dh_prime.hex()
        if not _is_valid_prime_group(prime_hex):
            raise CallCryptoError("invalid DH prime group")


def default_crypto_profile() -> CallCryptoProfile:
    profile = CallCryptoProfile(g=3, dh_prime=bytes.fromhex(_DEFAULT_DH_PRIME_HEX))
    profile.validate()
    return profile


@dataclass(slots=True)
class CallKeyMaterial:
    auth_key: bytes
    key_fingerprint: int


@dataclass(slots=True)
class CallCryptoContext:
    profile: CallCryptoProfile
    role: str
    _secret: int
    _public_value: bytes
    _g_a_hash: bytes | None
    _shared_key: bytearray | None = None
    _key_fingerprint: int | None = None

    @classmethod
    def new_outgoing(cls, profile: CallCryptoProfile | None = None) -> CallCryptoContext:
        p = profile if profile is not None else default_crypto_profile()
        p.validate()
        secret = cls._new_secret(p)
        g_a_int = pow(p.g, secret, p.p)
        g_a = _to_be(g_a_int, size=p.p_size)
        return cls(
            profile=p,
            role="outgoing",
            _secret=secret,
            _public_value=g_a,
            _g_a_hash=sha256(g_a),
        )

    @classmethod
    def new_incoming(
        cls,
        g_a_hash: bytes,
        profile: CallCryptoProfile | None = None,
    ) -> CallCryptoContext:
        p = profile if profile is not None else default_crypto_profile()
        p.validate()
        if len(g_a_hash) != 32:
            raise CallCryptoError("phoneCallRequested.g_a_hash must be 32 bytes")
        secret = cls._new_secret(p)
        g_b_int = pow(p.g, secret, p.p)
        g_b = _to_be(g_b_int, size=p.p_size)
        return cls(
            profile=p,
            role="incoming",
            _secret=secret,
            _public_value=g_b,
            _g_a_hash=bytes(g_a_hash),
        )

    @staticmethod
    def _new_secret(profile: CallCryptoProfile) -> int:
        # Secret is sampled in the valid DH range [2, p-2].
        return (secrets.randbits(profile.p_size * 8 + 64) % (profile.p - 3)) + 2

    @property
    def g_a_hash(self) -> bytes:
        if self.role != "outgoing":
            raise CallCryptoError("g_a_hash is only available for outgoing calls")
        if self._g_a_hash is None:
            raise CallCryptoError("missing g_a_hash")
        return self._g_a_hash

    @property
    def g_a(self) -> bytes:
        if self.role != "outgoing":
            raise CallCryptoError("g_a is only available for outgoing calls")
        return self._public_value

    @property
    def g_b(self) -> bytes:
        if self.role != "incoming":
            raise CallCryptoError("g_b is only available for incoming calls")
        return self._public_value

    @property
    def key_material(self) -> CallKeyMaterial | None:
        if self._shared_key is None or self._key_fingerprint is None:
            return None
        return CallKeyMaterial(
            auth_key=bytes(self._shared_key),
            key_fingerprint=self._key_fingerprint,
        )

    def apply_remote_g_b(self, g_b: bytes) -> CallKeyMaterial:
        if self.role != "outgoing":
            raise CallCryptoError("apply_remote_g_b requires outgoing role")
        return self._derive_shared_key(remote_public=g_b)

    def apply_remote_g_a(
        self,
        g_a: bytes,
        *,
        expected_fingerprint: int | None = None,
    ) -> CallKeyMaterial:
        if self.role != "incoming":
            raise CallCryptoError("apply_remote_g_a requires incoming role")
        if self._g_a_hash is not None and sha256(g_a) != self._g_a_hash:
            raise CallCryptoError("incoming g_a does not match g_a_hash")
        material = self._derive_shared_key(remote_public=g_a)
        if expected_fingerprint is not None:
            if material.key_fingerprint != int(expected_fingerprint):
                raise CallCryptoError("key_fingerprint mismatch")
        return material

    def apply_final_public(
        self,
        g_a_or_b: bytes,
        *,
        expected_fingerprint: int,
    ) -> CallKeyMaterial:
        if self.role == "outgoing":
            if self._shared_key is not None and self._key_fingerprint is not None:
                material = CallKeyMaterial(
                    auth_key=bytes(self._shared_key),
                    key_fingerprint=self._key_fingerprint,
                )
            else:
                material = self._derive_shared_key(remote_public=g_a_or_b)
        else:
            material = self.apply_remote_g_a(g_a_or_b, expected_fingerprint=expected_fingerprint)

        if material.key_fingerprint != int(expected_fingerprint):
            raise CallCryptoError("key_fingerprint mismatch")
        return material

    def verify_fingerprint(self, key_fingerprint: int) -> bool:
        if self._key_fingerprint is None:
            return False
        return self._key_fingerprint == int(key_fingerprint)

    def zeroize(self) -> None:
        self._secret = 0
        if self._shared_key is not None:
            for idx in range(len(self._shared_key)):
                self._shared_key[idx] = 0
            self._shared_key = None
        self._key_fingerprint = None

    def _derive_shared_key(self, *, remote_public: bytes) -> CallKeyMaterial:
        self._validate_public_value(remote_public)
        remote = _to_int(remote_public)
        p = self.profile.p
        if self._secret <= 1:
            raise CallCryptoError("local secret is not initialized")

        key_int = pow(remote, self._secret, p)
        auth_key = bytearray(_to_be(key_int, size=self.profile.p_size))
        key_fingerprint = int.from_bytes(sha1(bytes(auth_key))[-8:], "little", signed=True)
        self._shared_key = auth_key
        self._key_fingerprint = key_fingerprint
        # Secret is no longer required after key agreement.
        self._secret = 0
        return CallKeyMaterial(auth_key=bytes(auth_key), key_fingerprint=key_fingerprint)

    def _validate_public_value(self, public_value: bytes) -> None:
        if len(public_value) > self.profile.p_size:
            raise CallCryptoError("remote public value is too large")
        remote = _to_int(public_value)
        p = self.profile.p
        if remote <= 1 or remote >= (p - 1):
            raise CallCryptoError("remote public value is outside DH range")

        # Telegram-style guard: avoid values too close to group edges.
        bits = p.bit_length()
        lower_bound = 1 << max(0, bits - 64)
        upper_bound = p - lower_bound
        if remote <= lower_bound or remote >= upper_bound:
            raise CallCryptoError("remote public value failed security bounds")
