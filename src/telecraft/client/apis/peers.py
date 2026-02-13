from __future__ import annotations

from typing import TYPE_CHECKING

from telecraft.client.peers import Peer, PeerRef

if TYPE_CHECKING:
    from telecraft.client.mtproto import MtprotoClient


class PeersAPI:
    def __init__(self, raw: MtprotoClient) -> None:
        self._raw = raw

    async def resolve(self, ref: PeerRef, *, timeout: float = 20.0) -> Peer:
        return await self._raw.resolve_peer(ref, timeout=timeout)

    async def resolve_username(
        self,
        username: str,
        *,
        timeout: float = 20.0,
        force: bool = False,
    ) -> Peer:
        return await self._raw.resolve_username(username, timeout=timeout, force=force)

    async def resolve_phone(
        self,
        phone: str,
        *,
        timeout: float = 20.0,
        force: bool = False,
    ) -> Peer:
        return await self._raw.resolve_phone(phone, timeout=timeout, force=force)

    async def prime(
        self,
        *,
        limit: int = 100,
        folder_id: int | None = None,
        timeout: float = 20.0,
    ) -> None:
        await self._raw.prime_entities(limit=limit, folder_id=folder_id, timeout=timeout)
