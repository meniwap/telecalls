from __future__ import annotations

from pathlib import Path

from telecraft.tl.codec import RpcResult, UnknownTLObject, loads

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tl"


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURES_DIR / name).read_bytes()


def test_codec__account_themes__resync_and_unknown_entry_instead_of_crash() -> None:
    payload = _fixture_bytes("bad_themes_payload_20260212T223808Z_1.bin")
    decoded = loads(payload)

    assert getattr(decoded, "TL_NAME", "") == "account.themes"
    unknown_entries = [x for x in decoded.themes if isinstance(x, UnknownTLObject)]

    assert unknown_entries
    assert len(decoded.themes) == 8
    assert all(entry.expected_type == "Theme" for entry in unknown_entries)
    assert all(entry.raw for entry in unknown_entries)


def test_codec__account_themes__returns_usable_vector_with_unknown_objects() -> None:
    payload = _fixture_bytes("bad_themes_payload_20260212T223808Z_2.bin")
    wrapped = loads(payload)

    assert isinstance(wrapped, RpcResult)
    assert getattr(wrapped.result, "TL_NAME", "") == "account.themes"

    themes = list(getattr(wrapped.result, "themes", []))
    assert themes
    assert any(getattr(item, "TL_NAME", "") == "theme" for item in themes)
    assert any(isinstance(item, UnknownTLObject) for item in themes)
