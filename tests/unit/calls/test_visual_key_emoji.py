from __future__ import annotations

from telecraft.client.calls.visual_key import derive_call_emojis, fingerprint_to_hex


def test_derive_call_emojis_is_deterministic() -> None:
    key = bytes(range(1, 65))
    first = derive_call_emojis(key)
    second = derive_call_emojis(key)
    assert first == second
    assert len(first) == 4


def test_fingerprint_to_hex_signed_little_endian() -> None:
    fp = -9223372036854775808
    assert fingerprint_to_hex(fp) == "0000000000000080"
