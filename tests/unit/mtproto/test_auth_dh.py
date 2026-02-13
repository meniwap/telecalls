from __future__ import annotations

from telecraft.mtproto.auth.dh import make_dh_result


def test_make_dh_result_basic() -> None:
    # Small toy parameters (NOT secure) just to sanity-check math.
    # p = 23, g = 5, g_a = 8  (pretend server chose a so that g^a mod p = 8)
    res = make_dh_result(g=5, dh_prime=(23).to_bytes(1, "big"), g_a=(8).to_bytes(1, "big"))
    assert isinstance(res.auth_key, (bytes, bytearray))
    assert len(res.auth_key_id) == 8
    assert isinstance(res.g_b, (bytes, bytearray))
