from __future__ import annotations

from telecraft.client.calls.native_bridge import (
    NATIVE_BACKEND_AUTO,
    NATIVE_BACKEND_TGCALLS,
    NativeBridge,
)


def test_native_bridge_mock_roundtrip() -> None:
    bridge = NativeBridge(enabled=False, test_mode=True)
    bridge.ensure_session(call_id=77, incoming=True, video=False)
    bridge.set_allow_p2p(False)
    bridge.set_protocol_config(
        protocol_version=9,
        min_protocol_version=3,
        min_layer=65,
        max_layer=92,
    )
    bridge.set_network_type("wifi")

    bridge.push_signaling(77, b"abc")
    payload = bridge.pull_signaling(77)

    assert payload == b"abc"
    stats = bridge.poll_stats(77)
    assert stats is not None
    assert stats.bitrate_kbps is not None
    assert stats.native_backend == "legacy"
    debug = bridge.poll_debug(77)
    assert debug is not None
    assert debug["raw_packets_recv"] >= 1
    assert debug["raw_packets_sent"] >= 1
    assert debug["signaling_proto_decode_failures"] == 0
    assert debug["signaling_decrypt_ctr_failures"] == 0
    assert debug["signaling_decrypt_short_failures"] == 0
    assert debug["signaling_decrypt_ctr_header_invalid"] == 0
    assert debug["signaling_last_decrypt_mode"] == 0
    assert debug["signaling_last_decrypt_direction"] == 0
    assert debug["signaling_decrypt_last_error_code"] == 0
    assert debug["signaling_decrypt_last_error_stage"] == 0
    assert debug["signaling_proto_last_error_code"] == 0
    assert debug["signaling_candidate_winner_index"] == -1
    assert bridge.set_mute(77, True) is True
    assert bridge.set_bitrate_hint(77, 32) is True
    assert bridge.backend == "legacy"
    assert bridge.supports_rtc_servers is False

    bridge.stop(77)


def test_native_bridge_explicit_tgcalls_backend_in_mock_mode() -> None:
    bridge = NativeBridge(enabled=False, test_mode=True, backend=NATIVE_BACKEND_TGCALLS)
    bridge.ensure_session(call_id=88, incoming=False, video=False)
    assert bridge.backend == NATIVE_BACKEND_TGCALLS
    assert bridge.supports_rtc_servers is False
    assert bridge.set_rtc_servers(
        88,
        [
            {
                "host": "127.0.0.1",
                "port": 3478,
                "username": "u",
                "password": "p",
                "is_turn": True,
                "is_tcp": False,
            }
        ],
    )
    bridge.stop(88)


def test_native_bridge_auto_backend_normalizes_unknown_value() -> None:
    bridge = NativeBridge(enabled=False, test_mode=True, backend="unexpected")
    assert bridge.backend == "legacy"
    bridge2 = NativeBridge(enabled=False, test_mode=True, backend=NATIVE_BACKEND_AUTO)
    assert bridge2.backend == "legacy"
