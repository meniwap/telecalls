from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

PcmCallback = Callable[[bytes], None]


class AudioBackend(Protocol):
    def start_capture(self, cb_pcm: PcmCallback) -> None: ...

    def start_playback(self, source_pcm: Callable[[], bytes | None]) -> None: ...

    def stop(self) -> None: ...


class NullAudioBackend:
    def __init__(
        self,
        *,
        sample_rate: int = 48_000,
        channels: int = 1,
        frame_size: int = 960,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frame_size = int(frame_size)
        self._running = False
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._playback_thread: threading.Thread | None = None

    def start_capture(self, cb_pcm: PcmCallback) -> None:
        if self._capture_thread is not None:
            return
        self._running = True
        self._stop_event.clear()
        silence = b"\x00" * (self.frame_size * self.channels * 2)

        def _capture_loop() -> None:
            while not self._stop_event.is_set():
                cb_pcm(silence)
                time.sleep(0.02)

        self._capture_thread = threading.Thread(
            target=_capture_loop,
            name="telecalls-null-capture",
            daemon=True,
        )
        self._capture_thread.start()

    def start_playback(self, source_pcm: Callable[[], bytes | None]) -> None:
        if self._playback_thread is not None:
            return
        self._running = True
        self._stop_event.clear()

        def _playback_loop() -> None:
            while not self._stop_event.is_set():
                _ = source_pcm()
                time.sleep(0.02)

        self._playback_thread = threading.Thread(
            target=_playback_loop,
            name="telecalls-null-playback",
            daemon=True,
        )
        self._playback_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=1.0)
        if self._playback_thread is not None:
            self._playback_thread.join(timeout=1.0)
        self._capture_thread = None
        self._playback_thread = None
        self._running = False
