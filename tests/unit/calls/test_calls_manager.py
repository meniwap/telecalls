from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from telecraft.client.calls.crypto import CallCryptoContext, default_crypto_profile
from telecraft.client.calls.errors import SignalingDataError
from telecraft.client.calls.manager import CallsManager, CallsManagerConfig
from telecraft.client.calls.native_bridge import (
    NATIVE_BACKEND_LEGACY,
    NATIVE_BACKEND_TGCALLS,
)
from telecraft.client.calls.session import CallSession
from telecraft.client.calls.state import CallEndReason, CallState


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
            profile = default_crypto_profile()
            return SimpleNamespace(
                TL_NAME="messages.dhConfig",
                g=profile.g,
                p=profile.dh_prime,
                version=7,
                random=b"",
            )

        if name == "phone.requestCall":
            return SimpleNamespace(
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallWaiting",
                    id=987,
                    access_hash=654,
                    admin_id=777,
                    participant_id=555,
                ),
                users=[],
                chats=[],
            )

        if name == "phone.getCallConfig":
            return SimpleNamespace(
                TL_NAME="dataJSON",
                data=json.dumps(
                    {
                        "protocol": {
                            "udp_p2p": True,
                            "udp_reflector": True,
                            "min_layer": 100,
                            "max_layer": 201,
                            "library_versions": ["remote-lib"],
                        },
                        "call_connect_timeout_ms": 12000,
                    }
                ),
            )

        if name in {"phone.acceptCall", "phone.confirmCall"}:
            return SimpleNamespace(
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallAccepted",
                    id=1,
                    access_hash=2,
                    g_b=b"\x01" * 256,
                ),
                users=[],
                chats=[],
            )

        return SimpleNamespace(ok=True, users=[], chats=[])

    def _ingest_from_updates_result(self, obj: Any) -> None:
        _ = obj


def test_calls_manager_incoming_and_signaling_update() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)

        incoming: list[CallSession] = []
        signaling_packets: list[bytes] = []

        def _on_incoming(session: CallSession) -> None:
            incoming.append(session)
            session.on_signaling_data(lambda data: signaling_packets.append(data))

        manager.on_incoming(_on_incoming)
        await manager.start()

        assert raw._subs
        q = raw._subs[0]

        g_a_hash = b"a" * 32
        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCall",
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCallRequested",
                    id=101,
                    access_hash=202,
                    admin_id=999,
                    participant_id=777,
                    g_a_hash=g_a_hash,
                ),
            )
        )
        await asyncio.sleep(0.02)

        assert len(incoming) == 1
        assert incoming[0].state == CallState.RINGING_IN
        assert incoming[0].crypto is not None
        assert incoming[0].crypto.role == "incoming"

        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCallSignalingData",
                phone_call_id=101,
                data=b"abc",
            )
        )
        await asyncio.sleep(0.02)

        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCallSignalingData",
                phone_call_id=101,
                data=b"abc",
            )
        )
        await asyncio.sleep(0.02)

        assert signaling_packets == [b"abc"]
        assert incoming[0].last_signaling_blob_len == 3
        assert incoming[0].repeated_signaling_blob_count == 1
        await manager.stop()

    asyncio.run(_case())


def test_accept_then_hangup_race_does_not_crash() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        session = CallSession(
            call_id=1,
            access_hash=2,
            incoming=True,
            manager=manager,
            state=CallState.RINGING_IN,
            crypto=CallCryptoContext.new_incoming(b"a" * 32),
        )

        results = await asyncio.gather(session.accept(), session.hangup(), return_exceptions=True)
        assert all(not isinstance(item, Exception) for item in results)
        assert session.state in {
            CallState.CONNECTING,
            CallState.DISCONNECTING,
            CallState.ENDED,
        }

    asyncio.run(_case())


def test_receiver_never_crashes_and_marks_session_failed_on_unexpected_update() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        session = CallSession(
            call_id=404,
            access_hash=505,
            incoming=True,
            manager=manager,
            state=CallState.CONNECTING,
        )
        manager._sessions[404] = session

        def _raise(_phone_call: Any) -> int:
            raise RuntimeError("bad update payload")

        manager._extract_access_hash = _raise  # type: ignore[method-assign]

        await manager.start()
        q = raw._subs[0]
        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCall",
                phone_call=SimpleNamespace(
                    TL_NAME="phoneCall",
                    id=404,
                    access_hash=505,
                ),
            )
        )
        await asyncio.sleep(0.02)

        assert session.state == CallState.FAILED
        assert session.end_reason == CallEndReason.FAILED_INTERNAL

        await manager.stop()

    asyncio.run(_case())


def test_outgoing_call_moves_to_connecting() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)

        session = await manager.call("@someone")

        assert session.call_id == 987
        assert session.access_hash == 654
        assert session.state == CallState.CONNECTING
        assert session.crypto is not None
        assert manager._dh_config_version == 7
        assert manager._protocol_settings.udp_p2p is False
        assert manager._protocol_settings.udp_reflector is True
        assert manager._protocol_settings.min_layer == 65
        assert manager._protocol_settings.max_layer == 92
        assert manager._protocol_settings.library_versions == ("9.0.0",)
        assert manager._config.native_backend == NATIVE_BACKEND_LEGACY

    asyncio.run(_case())


def test_stop_call_tones_is_idempotent() -> None:
    raw = _Raw()
    manager = CallsManager(raw=raw, enabled=True)
    session = CallSession(call_id=909, access_hash=1010, incoming=True, manager=manager)
    session.ring_tone_active = True

    manager._stop_call_tones(session, reason="test1")
    first_ringback_stop = session.ringback_stopped_at
    first_ring_tone_stop = session.ring_tone_stopped_at

    manager._stop_call_tones(session, reason="test2")
    assert session.ring_tone_active is False
    assert session.ringback_stopped_at == first_ringback_stop
    assert session.ring_tone_stopped_at == first_ring_tone_stop


def test_outgoing_protocol_layers_come_from_calls_config() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(
            raw=raw,
            enabled=True,
            config=CallsManagerConfig(
                protocol_min_layer=70,
                protocol_max_layer=88,
                library_versions=("9.0.0", "8.0.0"),
                allow_p2p=False,
                force_udp_reflector=True,
            ),
        )

        await manager.call("@someone")
        assert manager._protocol_settings.min_layer == 70
        assert manager._protocol_settings.max_layer == 88
        assert manager._protocol_settings.library_versions == ("9.0.0",)

    asyncio.run(_case())


def test_connect_timeout_marks_session_failed() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(
            raw=raw,
            enabled=True,
            config=CallsManagerConfig(connect_timeout=0.02, max_retries=0),
        )
        session = await manager.call("@someone")
        await asyncio.sleep(0.05)
        assert session.state == CallState.FAILED
        assert session.end_reason == CallEndReason.FAILED_TIMEOUT
        await manager.stop()

    asyncio.run(_case())


def test_failed_protocol_discard_arms_version_fallback_once() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(
            raw=raw,
            enabled=True,
            config=CallsManagerConfig(library_versions=("11.0.0", "10.0.0", "9.0.0")),
        )
        session = CallSession(
            call_id=4040,
            access_hash=5050,
            incoming=False,
            manager=manager,
            state=CallState.CONNECTING,
        )
        manager._sessions[session.call_id] = session

        phone_call = SimpleNamespace(
            TL_NAME="phoneCallDiscarded",
            id=session.call_id,
            access_hash=session.access_hash,
            reason=SimpleNamespace(TL_NAME="phoneCallDiscardReasonDisconnect"),
        )
        await manager._dispatch_phone_call_event(session, phone_call)

        assert session.end_reason == CallEndReason.FAILED_PROTOCOL
        assert session.disconnect_reason_raw == "phoneCallDiscardReasonDisconnect"
        assert manager._active_library_version_index == 1
        assert manager._protocol_settings.library_versions == ("10.0.0",)

    asyncio.run(_case())


def test_dead_letter_signaling_for_unknown_session() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        await manager.start()
        q = raw._subs[0]
        await q.put(
            SimpleNamespace(
                TL_NAME="updatePhoneCallSignalingData",
                phone_call_id=999_123,
                data=b"x",
            )
        )
        await asyncio.sleep(0.02)
        assert any("signaling_without_session" in item for item in manager.dead_letters)
        await manager.stop()

    asyncio.run(_case())


def test_send_signaling_data_validates_payload() -> None:
    async def _case() -> None:
        raw = _Raw()
        manager = CallsManager(raw=raw, enabled=True)
        with pytest.raises(SignalingDataError):
            await manager.send_signaling_data(
                CallSession(
                    call_id=1,
                    access_hash=2,
                    incoming=False,
                    manager=manager,
                ),
                b"",
            )

    asyncio.run(_case())


def test_extract_rtc_servers_from_webrtc_connections() -> None:
    raw = _Raw()
    manager = CallsManager(raw=raw, enabled=True)
    phone_call = SimpleNamespace(
        connections=[
            SimpleNamespace(
                TL_NAME="phoneConnectionWebrtc",
                ip=b"149.154.167.51",
                ipv6=b"",
                port=443,
                username=b"user-a",
                password=b"pass-a",
                turn=True,
                stun=False,
            ),
            SimpleNamespace(
                TL_NAME="phoneConnectionWebrtc",
                ip="149.154.167.51",
                ipv6="",
                port=443,
                username="user-a",
                password="pass-a",
                turn=True,
                stun=False,
            ),
            SimpleNamespace(
                TL_NAME="phoneConnectionWebrtc",
                ip="149.154.167.55",
                ipv6="",
                port=3478,
                username="",
                password="",
                turn=False,
                stun=True,
            ),
            SimpleNamespace(
                TL_NAME="phoneConnection",
                ip="149.154.167.99",
                ipv6="",
                port=443,
                peer_tag=b"",
                id=1,
                tcp=False,
            ),
        ]
    )
    servers = manager._extract_rtc_servers(phone_call)
    assert servers == [
        {
            "host": "149.154.167.51",
            "port": 443,
            "username": "user-a",
            "password": "pass-a",
            "is_turn": True,
            "is_tcp": False,
        },
        {
            "host": "149.154.167.55",
            "port": 3478,
            "username": "",
            "password": "",
            "is_turn": False,
            "is_tcp": False,
        },
    ]


def test_extract_rtc_servers_prefers_ipv6_when_ipv4_missing() -> None:
    raw = _Raw()
    manager = CallsManager(raw=raw, enabled=True)
    phone_call = SimpleNamespace(
        connections=[
            SimpleNamespace(
                TL_NAME="phoneConnectionWebrtc",
                ip=b"",
                ipv6=b"2001:db8::10",
                port=3478,
                username=b"u6",
                password=b"p6",
                turn=True,
                stun=False,
            )
        ]
    )
    servers = manager._extract_rtc_servers(phone_call)
    assert servers == [
        {
            "host": "2001:db8::10",
            "port": 3478,
            "username": "u6",
            "password": "p6",
            "is_turn": True,
            "is_tcp": False,
        }
    ]


def test_manager_skips_python_audio_when_tgcalls_backend_selected() -> None:
    raw = _Raw()
    manager = CallsManager(
        raw=raw,
        enabled=True,
        config=CallsManagerConfig(
            native_bridge_enabled=False,
            native_backend=NATIVE_BACKEND_TGCALLS,
            audio_enabled=True,
            audio_backend="portaudio",
        ),
    )
    session = CallSession(
        call_id=42,
        access_hash=24,
        incoming=False,
        manager=manager,
        state=CallState.CONNECTING,
    )
    manager._start_audio_session(session)
    assert session.audio_backend is None
