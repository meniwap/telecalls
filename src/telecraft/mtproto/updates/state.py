from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from telecraft.tl.generated.types import UpdatesState as TlUpdatesState


@dataclass(slots=True)
class UpdatesState:
    """
    Minimal updates state needed for getDifference.
    """

    pts: int
    qts: int
    date: int
    seq: int

    @classmethod
    def from_tl(cls, tl: Any) -> UpdatesState:
        if not isinstance(tl, TlUpdatesState):
            raise TypeError(f"Expected updates.state, got: {type(tl).__name__}")
        return cls(
            pts=int(cast(int, tl.pts)),
            qts=int(cast(int, tl.qts)),
            date=int(cast(int, tl.date)),
            seq=int(cast(int, tl.seq)),
        )
