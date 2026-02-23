from __future__ import annotations

import pytest

from telecraft.client.calls.native_bridge import ENDPOINT_FLAG_RELAY, NativeBridge


def _silence_frame(samples: int = 960) -> bytes:
    return b"\x00\x00" * samples


def test_legacy_audio_push_uses_udp_counters_not_signaling() -> None:
    bridge = NativeBridge(enabled=True, test_mode=False, backend="legacy")
    if not getattr(bridge, "_available", False):
        pytest.skip("legacy native engine not available")

    call_id = 424242
    bridge.ensure_session(call_id=call_id, incoming=False, video=False)

    key = bytes(range(256))
    assert bridge.set_keys(call_id, key, 123456789, is_outgoing=True) is True
    assert bridge.set_remote_endpoints(
        call_id,
        [
            {
                "id": 1,
                "ip": "127.0.0.1",
                "ipv6": "",
                "port": 9,
                "peer_tag": b"",
                "flags": ENDPOINT_FLAG_RELAY,
                "priority": 10,
            }
        ],
    )

    # Drain any signaling emitted during init so baseline counters are stable.
    while bridge.pull_signaling(call_id) is not None:
        pass

    before = bridge.poll_debug(call_id)
    assert before is not None

    ok = bridge.push_audio_frame(call_id, _silence_frame())
    assert ok is True

    after = bridge.poll_debug(call_id)
    assert after is not None
    stats = bridge.poll_stats(call_id)
    assert stats is not None

    assert int(after["udp_out_frames"]) >= int(before["udp_out_frames"]) + 1
    assert int(after["udp_tx_bytes"]) > int(before["udp_tx_bytes"])
    assert int(after["signaling_out_frames"]) == int(before["signaling_out_frames"])
    assert int(after["raw_media_packets_sent"]) >= 1
    assert (stats.media_packets_sent or 0) >= 1

    bridge.stop(call_id)
