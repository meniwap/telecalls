from __future__ import annotations

from telecraft.client.calls.native_bridge import NativeBridge


def test_native_bridge_mock_roundtrip() -> None:
    bridge = NativeBridge(enabled=False, test_mode=True)
    bridge.ensure_session(call_id=77, incoming=True, video=False)
    bridge.set_allow_p2p(False)

    bridge.push_signaling(77, b"abc")
    payload = bridge.pull_signaling(77)

    assert payload == b"abc"
    stats = bridge.poll_stats(77)
    assert stats is not None
    assert stats.bitrate_kbps is not None
    assert bridge.set_mute(77, True) is True
    assert bridge.set_bitrate_hint(77, 32) is True

    bridge.stop(77)
