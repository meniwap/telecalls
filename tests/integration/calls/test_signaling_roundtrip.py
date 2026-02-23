from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from telecraft.client.calls.manager import CallsManager, CallsManagerConfig
from telecraft.client.calls.native_bridge import TC_ENGINE_STATE_ESTABLISHED
from telecraft.client.calls.state import CallState
from telecraft.client.calls.stats import CallStats


class _Entities:
    def input_user(self, user_id: int) -> Any:
        return SimpleNamespace(TL_NAME="inputUser", user_id=int(user_id), access_hash=111)


class _Raw:
    def __init__(self) -> None:
        self.self_user_id = 777
        self.entities = _Entities()
        self._subs: list[asyncio.Queue[Any]] = []
        self.calls: list[str] = []

    async def start_updates(self, *, timeout: float = 20.0) -> None:
        _ = timeout

    def subscribe_updates(self, *, maxsize: int = 1024) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        return q

    def unsubscribe_updates(self, q: asyncio.Queue[Any]) -> None:
        if q in self._subs:
            self._subs.remove(q)

    async def resolve_peer(self, ref: Any, *, timeout: float = 20.0) -> Any:
        _ = (ref, timeout)
        return SimpleNamespace(peer_type="user", peer_id=555)

    async def prime_entities(self, *, limit: int = 100, timeout: float = 20.0) -> None:
        _ = (limit, timeout)

    async def invoke_api(self, req: Any, *, timeout: float = 20.0) -> Any:
        _ = timeout
        name = getattr(req, "TL_NAME", type(req).__name__)
        self.calls.append(str(name))

        if name == "messages.getDhConfig":
            return SimpleNamespace(
                TL_NAME="messages.dhConfig",
                g=3,
                p=bytes.fromhex(
                    "C71CAEB9C6B1C9048E6C522F70F13F73980D40238E3E21C14934D037"
                    "563D930F48198A0AA7C14058229493D22530F4DBFA336F6E0AC92513"
                    "9543AED44CCE7C3720FD51F69458705AC68CD4FE6B6B13ABDC974651"
                    "2969328454F18FAF8C595F642477FE96BB2A941D5BCD1D4AC8CC4988"
                    "0708FA9B378E3C4F3A9060BEE67CF9A4A4A695811051907E162753B5"
                    "6B0F6B410DBA74D8A84B2A14B3144E0EF1284754FD17ED950D5965B4"
                    "B9DD46582DB1178D169C6BC465B0D6FF9CA3928FEF5B9AE4E418FC15"
                    "E83EBEA0F87FA9FF5EED70050DED2849F47BF959D956850CE929851F"
                    "0D8115F635B105EE2E4E15D04B2454BF6F4FADF034B10403119CD8E3"
                    "B92FCC5B"
                ),
                version=1,
                random=b"",
            )

        if name == "phone.getCallConfig":
            return SimpleNamespace(TL_NAME="dataJSON", data="{}")

        if name == "phone.requestCall":
            return SimpleNamespace(
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallWaiting",
                    id=321,
                    access_hash=654,
                    admin_id=777,
                    participant_id=555,
                ),
                users=[],
                chats=[],
            )

        return SimpleNamespace(ok=True)

    def _ingest_from_updates_result(self, obj: Any) -> None:
        _ = obj


class _FakeNativeBridge:
    def __init__(self) -> None:
        self._outgoing = [b"blob-a", b"blob-a"]
        self._state_drained = False
        self.backend = "legacy"
        self.supports_rtc_servers = False

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

    def set_mute(self, call_id: int, muted: bool) -> bool:
        _ = (call_id, muted)
        return True

    def set_keys(
        self,
        call_id: int,
        auth_key: bytes,
        key_fingerprint: int,
        *,
        is_outgoing: bool,
    ) -> bool:
        _ = (call_id, auth_key, key_fingerprint, is_outgoing)
        return True

    def set_remote_endpoints(self, call_id: int, endpoints: list[dict[str, Any]]) -> bool:
        _ = (call_id, endpoints)
        return True

    def set_rtc_servers(self, call_id: int, rtc_servers: list[dict[str, Any]]) -> bool:
        _ = (call_id, rtc_servers)
        return True

    def push_signaling(self, call_id: int, data: bytes) -> bool:
        _ = (call_id, data)
        return True

    def pull_signaling(self, call_id: int) -> bytes | None:
        _ = call_id
        if not self._outgoing:
            return None
        return self._outgoing.pop(0)

    def pull_state_events(self, call_id: int) -> tuple[int, ...]:
        _ = call_id
        if self._state_drained:
            return ()
        self._state_drained = True
        return (TC_ENGINE_STATE_ESTABLISHED,)

    def pull_error_events(self, call_id: int) -> tuple[tuple[int, str], ...]:
        _ = call_id
        return ()

    def poll_stats(self, call_id: int) -> CallStats:
        _ = call_id
        return CallStats(
            rtt_ms=22.0,
            loss=0.0,
            bitrate_kbps=19.0,
            jitter_ms=3.0,
            packets_sent=8,
            packets_recv=9,
            send_loss=0.0,
            recv_loss=0.0,
            endpoint_id=1001,
        )

    def stop(self, call_id: int) -> None:
        _ = call_id



def test_signaling_roundtrip_does_not_drop_retransmits_and_reaches_in_call() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        manager._native_bridge = _FakeNativeBridge()  # type: ignore[assignment]

        await manager.start()
        session = await manager.call("@someone")
        session.server_ready = True

        await manager._flush_native_signaling()
        manager._refresh_native_stats()

        signaling_calls = [name for name in raw.calls if name == "phone.sendSignalingData"]
        assert len(signaling_calls) == 2
        assert session.state == CallState.IN_CALL
        assert session.stats()["packets_sent"] == 8.0
        assert session.stats()["packets_recv"] == 9.0

        await manager.stop()

    asyncio.run(_case())


def test_tgcalls_backend_skips_python_audio_pipeline() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(
            raw=raw,
            enabled=True,
            config=CallsManagerConfig(
                native_backend="tgcalls",
                audio_enabled=True,
                audio_backend="null",
                strict_media_ready=True,
            ),
        )
        bridge = _FakeNativeBridge()
        bridge.backend = "tgcalls"
        bridge.supports_rtc_servers = True
        manager._native_bridge = bridge  # type: ignore[assignment]
        manager._native_audio_managed = True

        await manager.start()
        session = await manager.call("@someone")
        session.server_ready = True
        manager._refresh_native_stats()

        assert session.state == CallState.IN_CALL
        assert session.audio_backend is None

        await manager.stop()

    asyncio.run(_case())
