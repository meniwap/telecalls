from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from telecraft.mtproto.crypto.rsa import RsaPublicKey


def _der_pkcs1_from_pem(pem: bytes) -> bytes:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey):
        raise TypeError("Expected RSA public key")
    return key.public_bytes(encoding=Encoding.DER, format=PublicFormat.PKCS1)


@dataclass(frozen=True, slots=True)
class ServerKeyRing:
    """
    Known Telegram RSA public keys (for auth key exchange).

    Fingerprints are stored as **signed int64**, matching TL "long" decoding.
    """

    keys_by_fingerprint: dict[int, RsaPublicKey]

    def select(self, server_fingerprints: list[int]) -> RsaPublicKey:
        for fp in server_fingerprints:
            key = self.keys_by_fingerprint.get(fp)
            if key is not None:
                return key
        raise KeyError(f"No matching RSA key for server fingerprints: {server_fingerprints!r}")


TELEGRAM_TEST_RSA_PEM = b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAyMEdY1aR+sCR3ZSJrtztKTKqigvO/vBfqACJLZtS7QMgCGXJ6XIR
yy7mx66W0/sOFa7/1mAZtEoIokDP3ShoqF4fVNb6XeqgQfaUHd8wJpDWHcR2OFwv
plUUI1PLTktZ9uW2WE23b+ixNwJjJGwBDJPQEQFBE+vfmH0JP503wr5INS1poWg/
j25sIWeYPHYeOrFp/eXaqhISP6G+q2IeTaWTXpwZj4LzXq5YOpk4bYEQ6mvRq7D1
aHWfYmlEGepfaYR8Q0YqvvhYtMte3ITnuSJs171+GDqpdKcSwHnd6FudwGO4pcCO
j4WcDuXc2CTHgH8gFTNhp/Y8/SpDOhvn9QIDAQAB
-----END RSA PUBLIC KEY-----
"""

# IMPORTANT (real-world gotcha):
#
# During the auth key exchange, servers return *multiple* RSA key fingerprints in resPQ.
# Not every fingerprint in that list is guaranteed to be usable for clients.
#
# We hit a practical failure mode on Telegram test DC2:
# - server returns fingerprints including 0xb25898df208d2603 (signed: -5595554452916591101)
# - selecting it causes req_DH_params to receive a quick-ack and then no ServerDhParamsOk/Fail
#   (client times out, looks like "network issue" but it's key selection).
#
# Telethon handles this by trying multiple bundled keys (incl. legacy/old ones) until encryption
# succeeds and the server responds. To avoid the "picked a matching-but-nonworking key" trap,
# our DEFAULT_SERVER_KEYRING prefers the current primary keys used by official clients and
# keeps TELEGRAM_MAIN_RSA_PEM as fallback.
#
# If auth exchange starts timing out again after a change: check key selection order first.

TELEGRAM_PRIMARY_RSA_KEYS_PEM: tuple[bytes, ...] = (
    # Current Telegram RSA keys used during auth key exchange (from official client sources,
    # also mirrored in Telethon's bundled keys).
    b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAruw2yP/BCcsJliRoW5eBVBVle9dtjJw+OYED160Wybum9SXtBBLX
riwt4rROd9csv0t0OHCaTmRqBcQ0J8fxhN6/cpR1GWgOZRUAiQxoMnlt0R93LCX/
j1dnVa/gVbCjdSxpbrfY2g2L4frzjJvdl84Kd9ORYjDEAyFnEA7dD556OptgLQQ2
e2iVNq8NZLYTzLp5YpOdO1doK+ttrltggTCy5SrKeLoCPPbOgGsdxJxyz5KKcZnS
Lj16yE5HvJQn0CNpRdENvRUXe6tBP78O39oJ8BTHp9oIjd6XWXAsp2CvK45Ol8wF
XGF710w9lwCGNbmNxNYhtIkdqfsEcwR5JwIDAQAB
-----END RSA PUBLIC KEY-----
""",
    b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAvfLHfYH2r9R70w8prHblWt/nDkh+XkgpflqQVcnAfSuTtO05lNPs
pQmL8Y2XjVT4t8cT6xAkdgfmmvnvRPOOKPi0OfJXoRVylFzAQG/j83u5K3kRLbae
7fLccVhKZhY46lvsueI1hQdLgNV9n1cQ3TDS2pQOCtovG4eDl9wacrXOJTG2990V
jgnIKNA0UMoP+KF03qzryqIt3oTvZq03DyWdGK+AZjgBLaDKSnC6qD2cFY81UryR
WOab8zKkWAnhw2kFpcqhI0jdV5QaSCExvnsjVaX0Y1N0870931/5Jb9ICe4nweZ9
kSDF/gip3kWLG0o8XQpChDfyvsqB9OLV/wIDAQAB
-----END RSA PUBLIC KEY-----
""",
    b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAs/ditzm+mPND6xkhzwFIz6J/968CtkcSE/7Z2qAJiXbmZ3UDJPGr
zqTDHkO30R8VeRM/Kz2f4nR05GIFiITl4bEjvpy7xqRDspJcCFIOcyXm8abVDhF+
th6knSU0yLtNKuQVP6voMrnt9MV1X92LGZQLgdHZbPQz0Z5qIpaKhdyA8DEvWWvS
Uwwc+yi1/gGaybwlzZwqXYoPOhwMebzKUk0xW14htcJrRrq+PXXQbRzTMynseCoP
Ioke0dtCodbA3qQxQovE16q9zz4Otv2k4j63cz53J+mhkVWAeWxVGI0lltJmWtEY
K6er8VqqWot3nqmWMXogrgRLggv/NbbooQIDAQAB
-----END RSA PUBLIC KEY-----
""",
    b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAvmpxVY7ld/8DAjz6F6q05shjg8/4p6047bn6/m8yPy1RBsvIyvuD
uGnP/RzPEhzXQ9UJ5Ynmh2XJZgHoE9xbnfxL5BXHplJhMtADXKM9bWB11PU1Eioc
3+AXBB8QiNFBn2XI5UkO5hPhbb9mJpjA9Uhw8EdfqJP8QetVsI/xrCEbwEXe0xvi
fRLJbY08/Gp66KpQvy7g8w7VB8wlgePexW3pT13Ap6vuC+mQuJPyiHvSxjEKHgqe
Pji9NP3tJUFQjcECqcm0yV7/2d0t/pbCm+ZH1sadZspQCEPPrtbkQBlvHb4OLiIW
PGHKSMeRFvp3IWcmdJqXahxLCUS1Eh6MAQIDAQAB
-----END RSA PUBLIC KEY-----
""",
)

TELEGRAM_MAIN_RSA_PEM = b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAwVACPi9w23mF3tBkdZz+zwrzKOaaQdr01vAbU4E1pvkfj4sqDsm6
lyDONS789sVoD/xCS9Y0hkkC3gtL1tSfTlgCMOOul9lcixlEKzwKENj1Yz/s7daS
an9tqw3bfUV/nqgbhGX81v/+7RFAEd+RwFnK7a+XYl9sluzHRyVVaTTveB2GazTw
Efzk2DWgkBluml8OREmvfraX3bkHZJTKX4EQSjBbbdJ2ZXIsRrYOXfaA+xayEGB+
8hdlLmAjbCVfaigxX0CDqWeR1yFL9kwd9P0NsZRPsmoqVwMbMu7mStFai6aIhc3n
Slv8kg9qv1m6XHVQY3PnEw+QQtqSIXklHwIDAQAB
-----END RSA PUBLIC KEY-----
"""

_TEST_KEY = RsaPublicKey(der_spki=_der_pkcs1_from_pem(TELEGRAM_TEST_RSA_PEM))
_MAIN_KEY = RsaPublicKey(der_spki=_der_pkcs1_from_pem(TELEGRAM_MAIN_RSA_PEM))
_PRIMARY_KEYS = [
    RsaPublicKey(der_spki=_der_pkcs1_from_pem(pem)) for pem in TELEGRAM_PRIMARY_RSA_KEYS_PEM
]

# Fingerprints in Telegram are TL `long` (signed). If you need the canonical *unsigned* form:
#   fp_u64 = fp if fp >= 0 else fp + 2**64
DEFAULT_SERVER_KEYRING = ServerKeyRing(
    keys_by_fingerprint={
        # Prefer current keys first, fall back to the legacy main key.
        **{k.fingerprint: k for k in _PRIMARY_KEYS},
        _MAIN_KEY.fingerprint: _MAIN_KEY,
    }
)
