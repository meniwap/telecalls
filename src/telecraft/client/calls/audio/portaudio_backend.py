from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time
from collections.abc import Callable
from typing import Any

from .backend import PcmCallback

paInt16 = 0x00000008


class PortAudioError(RuntimeError):
    pass


class PortAudioBackend:
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

        self._lib = self._load_library()
        self._init_bindings()

        self._started = False
        self._capture_stream = ctypes.c_void_p()
        self._playback_stream = ctypes.c_void_p()
        self._capture_thread: threading.Thread | None = None
        self._playback_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        return ctypes.util.find_library("portaudio") is not None

    def start_capture(self, cb_pcm: PcmCallback) -> None:
        self._ensure_started()

        with self._lock:
            if self._capture_thread is not None:
                return
            self._open_input_stream()
            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                args=(cb_pcm,),
                name="telecalls-pa-capture",
                daemon=True,
            )
            self._capture_thread.start()

    def start_playback(self, source_pcm: Callable[[], bytes | None]) -> None:
        self._ensure_started()

        with self._lock:
            if self._playback_thread is not None:
                return
            self._open_output_stream()
            self._playback_thread = threading.Thread(
                target=self._playback_loop,
                args=(source_pcm,),
                name="telecalls-pa-playback",
                daemon=True,
            )
            self._playback_thread.start()

    def stop(self) -> None:
        self._stop_event.set()

        capture_thread = self._capture_thread
        playback_thread = self._playback_thread
        if capture_thread is not None:
            capture_thread.join(timeout=1.0)
        if playback_thread is not None:
            playback_thread.join(timeout=1.0)

        self._capture_thread = None
        self._playback_thread = None

        with self._lock:
            self._stop_stream(self._capture_stream)
            self._stop_stream(self._playback_stream)

            self._capture_stream = ctypes.c_void_p()
            self._playback_stream = ctypes.c_void_p()

            if self._started:
                rc = int(self._lib.Pa_Terminate())
                self._started = False
                if rc < 0:
                    raise PortAudioError(f"Pa_Terminate failed with error={rc}")

    def _capture_loop(self, cb_pcm: PcmCallback) -> None:
        sample_count = self.frame_size * self.channels
        buffer_type = ctypes.c_int16 * sample_count
        frame_buffer = buffer_type()

        while not self._stop_event.is_set():
            rc = int(
                self._lib.Pa_ReadStream(
                    self._capture_stream,
                    ctypes.byref(frame_buffer),
                    self.frame_size,
                )
            )
            if rc < 0:
                time.sleep(0.01)
                continue
            payload = bytes(frame_buffer)
            cb_pcm(payload)

    def _playback_loop(self, source_pcm: Callable[[], bytes | None]) -> None:
        sample_count = self.frame_size * self.channels
        bytes_per_frame = sample_count * ctypes.sizeof(ctypes.c_int16)

        while not self._stop_event.is_set():
            payload = source_pcm()
            if payload is None:
                payload = b""
            if len(payload) != bytes_per_frame:
                payload = payload[:bytes_per_frame].ljust(bytes_per_frame, b"\x00")
            c_payload = (ctypes.c_int16 * sample_count).from_buffer_copy(payload)
            rc = int(
                self._lib.Pa_WriteStream(
                    self._playback_stream,
                    ctypes.byref(c_payload),
                    self.frame_size,
                )
            )
            if rc < 0:
                time.sleep(0.01)

    def _ensure_started(self) -> None:
        self._stop_event.clear()
        if self._started:
            return
        rc = int(self._lib.Pa_Initialize())
        if rc < 0:
            raise PortAudioError(f"Pa_Initialize failed with error={rc}")
        self._started = True

    def _open_input_stream(self) -> None:
        if bool(self._capture_stream):
            return

        stream_ptr = ctypes.c_void_p()
        rc = int(
            self._lib.Pa_OpenDefaultStream(
                ctypes.byref(stream_ptr),
                self.channels,
                0,
                paInt16,
                float(self.sample_rate),
                ctypes.c_ulong(self.frame_size),
                None,
                None,
            )
        )
        if rc < 0:
            raise PortAudioError(f"Pa_OpenDefaultStream(input) failed with error={rc}")

        rc = int(self._lib.Pa_StartStream(stream_ptr))
        if rc < 0:
            self._lib.Pa_CloseStream(stream_ptr)
            raise PortAudioError(f"Pa_StartStream(input) failed with error={rc}")

        self._capture_stream = stream_ptr

    def _open_output_stream(self) -> None:
        if bool(self._playback_stream):
            return

        stream_ptr = ctypes.c_void_p()
        rc = int(
            self._lib.Pa_OpenDefaultStream(
                ctypes.byref(stream_ptr),
                0,
                self.channels,
                paInt16,
                float(self.sample_rate),
                ctypes.c_ulong(self.frame_size),
                None,
                None,
            )
        )
        if rc < 0:
            raise PortAudioError(f"Pa_OpenDefaultStream(output) failed with error={rc}")

        rc = int(self._lib.Pa_StartStream(stream_ptr))
        if rc < 0:
            self._lib.Pa_CloseStream(stream_ptr)
            raise PortAudioError(f"Pa_StartStream(output) failed with error={rc}")

        self._playback_stream = stream_ptr

    def _stop_stream(self, stream: ctypes.c_void_p) -> None:
        if not bool(stream):
            return
        self._lib.Pa_StopStream(stream)
        self._lib.Pa_CloseStream(stream)

    def _init_bindings(self) -> None:
        self._lib.Pa_Initialize.restype = ctypes.c_int
        self._lib.Pa_Terminate.restype = ctypes.c_int

        self._lib.Pa_OpenDefaultStream.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_double,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._lib.Pa_OpenDefaultStream.restype = ctypes.c_int

        self._lib.Pa_StartStream.argtypes = [ctypes.c_void_p]
        self._lib.Pa_StartStream.restype = ctypes.c_int

        self._lib.Pa_StopStream.argtypes = [ctypes.c_void_p]
        self._lib.Pa_StopStream.restype = ctypes.c_int

        self._lib.Pa_CloseStream.argtypes = [ctypes.c_void_p]
        self._lib.Pa_CloseStream.restype = ctypes.c_int

        self._lib.Pa_ReadStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        self._lib.Pa_ReadStream.restype = ctypes.c_int

        self._lib.Pa_WriteStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        self._lib.Pa_WriteStream.restype = ctypes.c_int

    def _load_library(self) -> Any:
        path = ctypes.util.find_library("portaudio")
        if path is None:
            raise PortAudioError("PortAudio library was not found on this host")
        return ctypes.CDLL(path)
