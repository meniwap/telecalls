from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class CallStats:
    rtt_ms: float | None = None
    loss: float | None = None
    bitrate_kbps: float | None = None
    jitter_ms: float | None = None
    updated_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, float | None]:
        return {
            "rtt_ms": self.rtt_ms,
            "loss": self.loss,
            "bitrate_kbps": self.bitrate_kbps,
            "jitter_ms": self.jitter_ms,
        }
