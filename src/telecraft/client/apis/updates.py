from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telecraft.client.mtproto import MtprotoClient


class UpdatesAPI:
    def __init__(self, raw: MtprotoClient) -> None:
        self._raw = raw

    async def start(self, *, timeout: float = 20.0) -> None:
        await self._raw.start_updates(timeout=timeout)

    async def stop(self) -> None:
        await self._raw.stop_updates()

    async def recv(self) -> Any:
        return await self._raw.recv_update()
