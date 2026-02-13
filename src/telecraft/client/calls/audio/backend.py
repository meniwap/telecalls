from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

PcmCallback = Callable[[bytes], None]


class AudioBackend(Protocol):
    def start_capture(self, cb_pcm: PcmCallback) -> None: ...

    def start_playback(self, source_pcm: Callable[[], bytes | None]) -> None: ...

    def stop(self) -> None: ...


class NullAudioBackend:
    def __init__(self) -> None:
        self._running = False

    def start_capture(self, cb_pcm: PcmCallback) -> None:
        _ = cb_pcm
        self._running = True

    def start_playback(self, source_pcm: Callable[[], bytes | None]) -> None:
        _ = source_pcm
        self._running = True

    def stop(self) -> None:
        self._running = False
