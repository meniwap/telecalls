from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from telecraft.client.calls.manager import CallsManager, CallsManagerConfig
from telecraft.client.calls.session import CallSession
from telecraft.client.calls.state import CallState


class _Raw:
    def __init__(self) -> None:
        self.self_user_id = 777

    async def start_updates(self, *, timeout: float = 20.0) -> None:
        _ = timeout

    def subscribe_updates(self, *, maxsize: int = 1024) -> asyncio.Queue[Any]:
        _ = maxsize
        return asyncio.Queue()

    def unsubscribe_updates(self, q: asyncio.Queue[Any]) -> None:
        _ = q

    async def invoke_api(self, req: Any, *, timeout: float = 20.0) -> Any:
        _ = (req, timeout)
        return SimpleNamespace(TL_NAME="ok")


class _CaptureBridge:
    def __init__(self) -> None:
        self.backend = "tgcalls"
        self.supports_rtc_servers = True
        self.last_endpoints: list[dict[str, Any]] = []
        self.last_rtc_servers: list[dict[str, Any]] = []

    def set_allow_p2p(self, value: bool) -> None:
        _ = value

    def set_protocol_config(
        self,
        *,
        protocol_version: int,
        min_protocol_version: int,
        min_layer: int,
        max_layer: int,
    ) -> None:
        _ = (protocol_version, min_protocol_version, min_layer, max_layer)

    def set_network_type(self, value: str) -> None:
        _ = value

    def ensure_session(self, *, call_id: int, incoming: bool, video: bool) -> None:
        _ = (call_id, incoming, video)

    def set_bitrate_hint(self, call_id: int, bitrate_kbps: int) -> bool:
        _ = (call_id, bitrate_kbps)
        return True

    def set_remote_endpoints(self, call_id: int, endpoints: list[dict[str, Any]]) -> bool:
        _ = call_id
        self.last_endpoints = list(endpoints)
        return True

    def set_rtc_servers(self, call_id: int, rtc_servers: list[dict[str, Any]]) -> bool:
        _ = call_id
        self.last_rtc_servers = list(rtc_servers)
        return True

    def push_signaling(self, call_id: int, data: bytes) -> bool:
        _ = (call_id, data)
        return True

    def pull_signaling(self, call_id: int) -> bytes | None:
        _ = call_id
        return None

    def pull_state_events(self, call_id: int) -> tuple[int, ...]:
        _ = call_id
        return ()

    def pull_error_events(self, call_id: int) -> tuple[tuple[int, str], ...]:
        _ = call_id
        return ()

    def poll_stats(self, call_id: int) -> None:
        _ = call_id
        return None

    def stop(self, call_id: int) -> None:
        _ = call_id


def test_manager_pushes_webrtc_rtc_servers_into_native_bridge() -> None:
    async def _case() -> None:
        manager = CallsManager(
            raw=_Raw(),
            enabled=True,
            config=CallsManagerConfig(
                native_bridge_enabled=False,
                native_backend="tgcalls",
                strict_media_ready=False,
            ),
        )
        capture = _CaptureBridge()
        manager._native_bridge = capture  # type: ignore[assignment]

        session = CallSession(
            call_id=1001,
            access_hash=2002,
            incoming=False,
            manager=manager,
            state=CallState.CONNECTING,
        )
        manager._sessions[session.call_id] = session

        phone_call = SimpleNamespace(
            TL_NAME="phoneCall",
            id=session.call_id,
            access_hash=session.access_hash,
            key_fingerprint=12345,
            protocol=SimpleNamespace(
                TL_NAME="phoneCallProtocol",
                library_versions=["9.0.0"],
                min_layer=65,
                max_layer=92,
                udp_p2p=False,
                udp_reflector=True,
            ),
            connections=[
                SimpleNamespace(
                    TL_NAME="phoneConnectionWebrtc",
                    id=1,
                    ip="149.154.167.51",
                    ipv6="",
                    port=443,
                    turn=True,
                    stun=False,
                    username="user-x",
                    password="pass-x",
                    peer_tag=b"",
                )
            ],
        )

        await manager._dispatch_phone_call_event(session, phone_call)

        assert capture.last_endpoints
        assert capture.last_endpoints[0]["kind"] == "phoneConnectionWebrtc"
        assert capture.last_rtc_servers == [
            {
                "host": "149.154.167.51",
                "port": 443,
                "username": "user-x",
                "password": "pass-x",
                "is_turn": True,
                "is_tcp": False,
            }
        ]

    asyncio.run(_case())
