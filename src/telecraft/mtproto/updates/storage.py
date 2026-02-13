from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from telecraft.mtproto.updates.state import UpdatesState

_UPDATES_STATE_VERSION = 1


class UpdatesStateStorageError(Exception):
    pass


@dataclass(slots=True)
class PersistedUpdatesState:
    """
    Persistent representation of UpdatesState.

    Stored separately from the MTProto session so we can evolve it independently.
    """

    pts: int
    qts: int
    date: int
    seq: int
    version: int = _UPDATES_STATE_VERSION

    def validate(self) -> None:
        if self.version != _UPDATES_STATE_VERSION:
            raise UpdatesStateStorageError(f"Unsupported updates state version: {self.version}")
        for name in ("pts", "qts", "date", "seq"):
            v = getattr(self, name)
            if not isinstance(v, int) or v < 0:
                raise UpdatesStateStorageError(f"Invalid {name}: {v!r}")

    def to_updates_state(self) -> UpdatesState:
        self.validate()
        return UpdatesState(pts=self.pts, qts=self.qts, date=self.date, seq=self.seq)

    @classmethod
    def from_updates_state(cls, state: UpdatesState) -> PersistedUpdatesState:
        return cls(pts=int(state.pts), qts=int(state.qts), date=int(state.date), seq=int(state.seq))

    def to_json_dict(self) -> dict[str, int]:
        self.validate()
        return {
            "version": int(self.version),
            "pts": int(self.pts),
            "qts": int(self.qts),
            "date": int(self.date),
            "seq": int(self.seq),
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, object]) -> PersistedUpdatesState:
        try:
            version_obj = data.get("version", _UPDATES_STATE_VERSION)
            if not isinstance(version_obj, (int, str)):
                raise UpdatesStateStorageError("Invalid version")
            version = int(version_obj)

            def _need_int(k: str) -> int:
                v = data[k]
                if not isinstance(v, (int, str)):
                    raise UpdatesStateStorageError(f"Invalid {k}")
                return int(v)

            pts = _need_int("pts")
            qts = _need_int("qts")
            date = _need_int("date")
            seq = _need_int("seq")
        except KeyError as e:
            raise UpdatesStateStorageError(f"Missing field: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise UpdatesStateStorageError("Invalid updates state JSON shape") from e

        out = cls(pts=pts, qts=qts, date=date, seq=seq, version=version)
        out.validate()
        return out


def load_updates_state_file(path: str | Path) -> UpdatesState:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise UpdatesStateStorageError(f"Failed to parse updates state JSON: {p}") from e
    if not isinstance(data, dict):
        raise UpdatesStateStorageError("Updates state JSON must be an object")
    return PersistedUpdatesState.from_json_dict(data).to_updates_state()


def save_updates_state_file(path: str | Path, state: UpdatesState) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    payload = PersistedUpdatesState.from_updates_state(state)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(payload.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)
