from __future__ import annotations

from pathlib import Path

import pytest


def test_schema_files_present() -> None:
    root = Path(__file__).resolve().parents[1]
    sources = root / "src" / "telecraft" / "schema" / "sources"
    if not (sources / "api.tl").exists() or not (sources / "mtproto.tl").exists():
        pytest.skip("Schema not fetched yet. Run: python tools/fetch_schema.py")


def test_layer_pinned() -> None:
    from telecraft.schema import LAYER

    if LAYER <= 0:
        pytest.skip("Layer not pinned yet. Run: python tools/fetch_schema.py")
