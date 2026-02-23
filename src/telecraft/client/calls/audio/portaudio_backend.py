from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from .backend import PcmCallback

paInt16 = 0x00000008
paNoFlag = 0
paNoDevice = -1


class PaStreamParameters(ctypes.Structure):
    _fields_ = [
        ("device", ctypes.c_int),
        ("channelCount", ctypes.c_int),
        ("sampleFormat", ctypes.c_ulong),
        ("suggestedLatency", ctypes.c_double),
        ("hostApiSpecificStreamInfo", ctypes.c_void_p),
    ]


class PaDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("structVersion", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("hostApi", ctypes.c_int),
        ("maxInputChannels", ctypes.c_int),
        ("maxOutputChannels", ctypes.c_int),
        ("defaultLowInputLatency", ctypes.c_double),
        ("defaultLowOutputLatency", ctypes.c_double),
        ("defaultHighInputLatency", ctypes.c_double),
        ("defaultHighOutputLatency", ctypes.c_double),
        ("defaultSampleRate", ctypes.c_double),
    ]


class PortAudioError(RuntimeError):
    pass


class PortAudioBackend:
    def __init__(
        self,
        *,
        sample_rate: int = 48_000,
        channels: int = 1,
        frame_size: int = 960,
        input_device_index: int | None = None,
        output_device_index: int | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frame_size = int(frame_size)
        self.input_device_index = (
            input_device_index
            if input_device_index is not None
            else self._parse_device_env("TELECALLS_PA_INPUT_DEVICE")
        )
        self.output_device_index = (
            output_device_index
            if output_device_index is not None
            else self._parse_device_env("TELECALLS_PA_OUTPUT_DEVICE")
        )

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

    @staticmethod
    def _parse_device_env(name: str) -> int | None:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return None
        try:
            return int(raw.strip())
        except ValueError:
            return None

    def list_devices(self) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        self._ensure_started()
        count = int(self._lib.Pa_GetDeviceCount())
        if count <= 0:
            return devices
        for idx in range(count):
            info_ptr = self._lib.Pa_GetDeviceInfo(idx)
            if not bool(info_ptr):
                continue
            info = info_ptr.contents
            name = ""
            if info.name is not None:
                name = info.name.decode("utf-8", errors="replace")
            devices.append(
                {
                    "index": idx,
                    "name": name,
                    "max_input_channels": int(info.maxInputChannels),
                    "max_output_channels": int(info.maxOutputChannels),
                    "default_sample_rate": float(info.defaultSampleRate),
                }
            )
        return devices

    def _pa_error_text(self, code: int) -> str:
        text_ptr = self._lib.Pa_GetErrorText(int(code))
        if not bool(text_ptr):
            return f"PortAudio error={int(code)}"
        return str(text_ptr.decode("utf-8", errors="replace"))

    def _format_stream_error(
        self,
        *,
        operation: str,
        code: int,
        device_kind: str,
        device_index: int | None,
    ) -> str:
        hint = ""
        if int(code) == -9985:
            hint = (
                " device unavailable: check OS microphone permission, and make sure "
                "another app is not holding the selected/default device."
            )
        return (
            f"{operation} failed with error={int(code)} ({self._pa_error_text(code)}); "
            f"{device_kind}_device_index={device_index!r}.{hint}"
        )

    def _default_device_index(self, *, input_stream: bool) -> int:
        return int(
            self._lib.Pa_GetDefaultInputDevice()
            if input_stream
            else self._lib.Pa_GetDefaultOutputDevice()
        )

    def _device_info(self, index: int) -> PaDeviceInfo | None:
        if int(index) < 0:
            return None
        info_ptr = self._lib.Pa_GetDeviceInfo(int(index))
        if not bool(info_ptr):
            return None
        return info_ptr.contents

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

        selected_index = (
            self.input_device_index
            if self.input_device_index is not None
            else self._default_device_index(input_stream=True)
        )
        if selected_index == paNoDevice:
            raise PortAudioError("No PortAudio input device available")
        device_info = self._device_info(selected_index)
        if device_info is None:
            raise PortAudioError(
                f"Input device index {selected_index} not found in PortAudio device list"
            )
        if int(device_info.maxInputChannels) <= 0:
            raise PortAudioError(
                f"Input device index {selected_index} has no input channels"
            )

        input_params = PaStreamParameters()
        input_params.device = int(selected_index)
        input_params.channelCount = int(self.channels)
        input_params.sampleFormat = paInt16
        input_params.suggestedLatency = float(device_info.defaultLowInputLatency)
        input_params.hostApiSpecificStreamInfo = None

        stream_ptr = ctypes.c_void_p()
        rc = int(
            self._lib.Pa_OpenStream(
                ctypes.byref(stream_ptr),
                ctypes.byref(input_params),
                None,
                float(self.sample_rate),
                ctypes.c_ulong(self.frame_size),
                ctypes.c_ulong(paNoFlag),
                None,
                None,
            )
        )
        if rc < 0:
            raise PortAudioError(
                self._format_stream_error(
                    operation="Pa_OpenStream(input)",
                    code=rc,
                    device_kind="input",
                    device_index=selected_index,
                )
            )

        rc = int(self._lib.Pa_StartStream(stream_ptr))
        if rc < 0:
            self._lib.Pa_CloseStream(stream_ptr)
            raise PortAudioError(
                self._format_stream_error(
                    operation="Pa_StartStream(input)",
                    code=rc,
                    device_kind="input",
                    device_index=selected_index,
                )
            )

        self._capture_stream = stream_ptr

    def _open_output_stream(self) -> None:
        if bool(self._playback_stream):
            return

        selected_index = (
            self.output_device_index
            if self.output_device_index is not None
            else self._default_device_index(input_stream=False)
        )
        if selected_index == paNoDevice:
            raise PortAudioError("No PortAudio output device available")
        device_info = self._device_info(selected_index)
        if device_info is None:
            raise PortAudioError(
                f"Output device index {selected_index} not found in PortAudio device list"
            )
        if int(device_info.maxOutputChannels) <= 0:
            raise PortAudioError(
                f"Output device index {selected_index} has no output channels"
            )

        output_params = PaStreamParameters()
        output_params.device = int(selected_index)
        output_params.channelCount = int(self.channels)
        output_params.sampleFormat = paInt16
        output_params.suggestedLatency = float(device_info.defaultLowOutputLatency)
        output_params.hostApiSpecificStreamInfo = None

        stream_ptr = ctypes.c_void_p()
        rc = int(
            self._lib.Pa_OpenStream(
                ctypes.byref(stream_ptr),
                None,
                ctypes.byref(output_params),
                float(self.sample_rate),
                ctypes.c_ulong(self.frame_size),
                ctypes.c_ulong(paNoFlag),
                None,
                None,
            )
        )
        if rc < 0:
            raise PortAudioError(
                self._format_stream_error(
                    operation="Pa_OpenStream(output)",
                    code=rc,
                    device_kind="output",
                    device_index=selected_index,
                )
            )

        rc = int(self._lib.Pa_StartStream(stream_ptr))
        if rc < 0:
            self._lib.Pa_CloseStream(stream_ptr)
            raise PortAudioError(
                self._format_stream_error(
                    operation="Pa_StartStream(output)",
                    code=rc,
                    device_kind="output",
                    device_index=selected_index,
                )
            )

        self._playback_stream = stream_ptr

    def _stop_stream(self, stream: ctypes.c_void_p) -> None:
        if not bool(stream):
            return
        self._lib.Pa_StopStream(stream)
        self._lib.Pa_CloseStream(stream)

    def _init_bindings(self) -> None:
        self._lib.Pa_Initialize.restype = ctypes.c_int
        self._lib.Pa_Terminate.restype = ctypes.c_int
        self._lib.Pa_GetErrorText.argtypes = [ctypes.c_int]
        self._lib.Pa_GetErrorText.restype = ctypes.c_char_p
        self._lib.Pa_GetDeviceCount.argtypes = []
        self._lib.Pa_GetDeviceCount.restype = ctypes.c_int
        self._lib.Pa_GetDefaultInputDevice.argtypes = []
        self._lib.Pa_GetDefaultInputDevice.restype = ctypes.c_int
        self._lib.Pa_GetDefaultOutputDevice.argtypes = []
        self._lib.Pa_GetDefaultOutputDevice.restype = ctypes.c_int
        self._lib.Pa_GetDeviceInfo.argtypes = [ctypes.c_int]
        self._lib.Pa_GetDeviceInfo.restype = ctypes.POINTER(PaDeviceInfo)

        self._lib.Pa_OpenStream.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(PaStreamParameters),
            ctypes.POINTER(PaStreamParameters),
            ctypes.c_double,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._lib.Pa_OpenStream.restype = ctypes.c_int

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
