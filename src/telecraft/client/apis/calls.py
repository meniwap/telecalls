from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telecraft.client.calls import CallSession, CallsManager, PhoneCallRef
from telecraft.client.calls.signaling import CallSignalingAdapter
from telecraft.client.peers import PeerRef


class CallsAPI:
    def __init__(
        self,
        raw: Any,
        *,
        enabled: bool = False,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._raw = raw
        self._manager = CallsManager(raw=raw, enabled=enabled, config=config)
        self._signaling = CallSignalingAdapter(raw)

    @property
    def enabled(self) -> bool:
        return self._manager.enabled

    def set_enabled(self, value: bool) -> None:
        self._manager.set_enabled(value)

    async def start(self) -> None:
        await self._manager.start()

    async def stop(self) -> None:
        await self._manager.stop()

    def on_incoming(self, handler: Callable[[CallSession], Any]) -> None:
        self._manager.on_incoming(handler)

    async def call(
        self,
        peer: PeerRef,
        *,
        video: bool = False,
        timeout: float = 20.0,
    ) -> CallSession:
        return await self._manager.call(peer, video=video, timeout=timeout)

    async def get_config(self, *, timeout: float = 20.0) -> Any:
        return await self._signaling.get_call_config(timeout=timeout)

    async def send_signaling_data(
        self,
        call_ref: PhoneCallRef | CallSession,
        data: bytes,
        *,
        timeout: float = 20.0,
    ) -> Any:
        return await self._manager.send_signaling_data(call_ref, data, timeout=timeout)

    async def confirm(
        self,
        call_ref: PhoneCallRef | CallSession,
        key_fingerprint: int,
        *,
        g_a: bytes = b"",
        timeout: float = 20.0,
    ) -> Any:
        return await self._manager.confirm(
            call_ref,
            key_fingerprint=key_fingerprint,
            g_a=g_a,
            timeout=timeout,
        )

    async def received(self, call_ref: PhoneCallRef, *, timeout: float = 20.0) -> Any:
        return await self._signaling.received_call(call_ref, timeout=timeout)
