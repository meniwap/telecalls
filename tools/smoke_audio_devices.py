from __future__ import annotations

import argparse
import json
import time

from telecraft.client.calls.audio.portaudio_backend import PortAudioBackend


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List PortAudio devices and validate open capture/playback streams."
    )
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--frame-size", type=int, default=960)
    parser.add_argument("--duration-seconds", type=float, default=2.0)
    args = parser.parse_args()

    if not PortAudioBackend.is_available():
        print(json.dumps({"result": "error", "error": "portaudio library not available"}))
        return 2

    backend: PortAudioBackend | None = None
    try:
        backend = PortAudioBackend(
            sample_rate=args.sample_rate,
            channels=args.channels,
            frame_size=args.frame_size,
        )
        devices = backend.list_devices()
        print(
            json.dumps(
                {
                    "result": "devices",
                    "input_device_index": backend.input_device_index,
                    "output_device_index": backend.output_device_index,
                    "count": len(devices),
                    "devices": devices,
                }
            )
        )

        frame_bytes = args.frame_size * args.channels * 2
        silence = b"\x00" * frame_bytes
        backend.start_capture(lambda _payload: None)
        backend.start_playback(lambda: silence)
        time.sleep(max(0.1, float(args.duration_seconds)))
        backend.stop()
        print(json.dumps({"result": "ok"}))
        return 0
    except Exception as exc:  # noqa: BLE001
        if backend is not None:
            try:
                backend.stop()
            except Exception:
                pass
        print(json.dumps({"result": "error", "error": repr(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
