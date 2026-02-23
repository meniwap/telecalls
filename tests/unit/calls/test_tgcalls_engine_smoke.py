from __future__ import annotations

import importlib

import pytest

from telecraft.client.calls.native_bridge import NATIVE_BACKEND_TGCALLS, NativeBridge


def test_tgcalls_backend_mock_mode_accepts_rtc_servers() -> None:
    bridge = NativeBridge(enabled=False, test_mode=True, backend=NATIVE_BACKEND_TGCALLS)
    bridge.ensure_session(call_id=9001, incoming=False, video=False)
    assert bridge.set_rtc_servers(
        9001,
        [
            {
                "host": "149.154.167.51",
                "port": 443,
                "username": "u",
                "password": "p",
                "is_turn": True,
                "is_tcp": False,
            }
        ],
    )
    bridge.stop(9001)


def test_tgcalls_backend_compiled_module_roundtrip_when_available() -> None:
    try:
        importlib.import_module("telecraft.client.calls._tgcalls_cffi")
    except Exception:
        pytest.skip("tgcalls cffi module is not built in this environment")

    bridge = NativeBridge(enabled=True, test_mode=False, backend=NATIVE_BACKEND_TGCALLS)
    if bridge.backend != NATIVE_BACKEND_TGCALLS:
        pytest.skip("tgcalls backend not selected")

    bridge.ensure_session(call_id=9002, incoming=False, video=False)
    assert bridge.set_keys(9002, b"\x01" * 256, 123, is_outgoing=True)
    assert bridge.set_remote_endpoints(
        9002,
        [
            {
                "id": 1,
                "ip": "149.154.167.51",
                "ipv6": "",
                "port": 443,
                "peer_tag": b"",
                "flags": 1,
                "priority": 10,
            }
        ],
    )
    assert bridge.set_rtc_servers(
        9002,
        [
            {
                "host": "149.154.167.51",
                "port": 443,
                "username": "u",
                "password": "p",
                "is_turn": True,
                "is_tcp": False,
            }
        ],
    )
    assert bridge.push_signaling(9002, b"hello")
    _ = bridge.pull_signaling(9002)
    bridge.stop(9002)
