from __future__ import annotations

import json

import pytest

from telecraft.mtproto.updates.state import UpdatesState
from telecraft.mtproto.updates.storage import (
    UpdatesStateStorageError,
    load_updates_state_file,
    save_updates_state_file,
)


def test_updates_state_storage_roundtrip(tmp_path) -> None:
    p = tmp_path / "x.updates.json"
    st = UpdatesState(pts=123, qts=456, date=789, seq=10)
    save_updates_state_file(p, st)
    got = load_updates_state_file(p)
    assert got == st


def test_updates_state_storage_rejects_bad_version(tmp_path) -> None:
    p = tmp_path / "x.updates.json"
    p.write_text(
        json.dumps({"version": 999, "pts": 1, "qts": 2, "date": 3, "seq": 4}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(UpdatesStateStorageError):
        _ = load_updates_state_file(p)
