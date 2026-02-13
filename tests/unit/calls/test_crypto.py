from __future__ import annotations

import pytest

from telecraft.client.calls.crypto import (
    CallCryptoContext,
    CallCryptoError,
    CallCryptoProfile,
    default_crypto_profile,
)


def test_crypto_roundtrip_outgoing_incoming() -> None:
    outgoing = CallCryptoContext.new_outgoing()
    incoming = CallCryptoContext.new_incoming(outgoing.g_a_hash)

    incoming_material = incoming.apply_remote_g_a(outgoing.g_a)
    outgoing_material = outgoing.apply_remote_g_b(incoming.g_b)

    assert incoming_material.auth_key == outgoing_material.auth_key
    assert incoming_material.key_fingerprint == outgoing_material.key_fingerprint
    assert outgoing.verify_fingerprint(incoming_material.key_fingerprint)
    assert incoming.verify_fingerprint(outgoing_material.key_fingerprint)


def test_incoming_rejects_wrong_g_a_hash() -> None:
    outgoing = CallCryptoContext.new_outgoing()
    incoming = CallCryptoContext.new_incoming(b"b" * 32)

    with pytest.raises(CallCryptoError):
        incoming.apply_remote_g_a(outgoing.g_a)


def test_apply_final_public_verifies_fingerprint() -> None:
    outgoing = CallCryptoContext.new_outgoing()
    incoming = CallCryptoContext.new_incoming(outgoing.g_a_hash)
    outgoing_material = outgoing.apply_remote_g_b(incoming.g_b)

    with pytest.raises(CallCryptoError):
        outgoing.apply_final_public(
            incoming.g_b,
            expected_fingerprint=outgoing_material.key_fingerprint + 1,
        )


def test_invalid_profile_is_rejected() -> None:
    with pytest.raises(CallCryptoError):
        CallCryptoContext.new_outgoing(
            CallCryptoProfile(g=3, dh_prime=(1).to_bytes(256, "big"))
        )


def test_default_profile_uses_telegram_good_prime() -> None:
    profile = default_crypto_profile()
    assert profile.g == 3
    assert profile.p_size == 256
    assert profile.dh_prime[:16].hex() == "c71caeb9c6b1c9048e6c522f70f13f73"
