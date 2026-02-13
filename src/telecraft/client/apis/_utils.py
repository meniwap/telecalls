from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from telecraft.client.entities import EntityCacheError
from telecraft.client.peers import PeerRef
from telecraft.tl.generated.types import InputDialogPeer, InputPeerSelf


async def resolve_input_peer(raw: Any, peer: PeerRef, *, timeout: float) -> Any:
    resolved = await raw.resolve_peer(peer, timeout=timeout)
    try:
        return raw.entities.input_peer(resolved)
    except EntityCacheError:
        await raw.prime_entities(limit=200, timeout=timeout)
        return raw.entities.input_peer(resolved)


async def resolve_input_peer_or_self(raw: Any, peer: PeerRef | str, *, timeout: float) -> Any:
    if isinstance(peer, str) and peer.strip().lower() == "self":
        return InputPeerSelf()
    return await resolve_input_peer(raw, peer, timeout=timeout)


async def resolve_input_user(raw: Any, user: PeerRef, *, timeout: float) -> Any:
    resolved = await raw.resolve_peer(user, timeout=timeout)
    if getattr(resolved, "peer_type", None) != "user":
        raise ValueError("Expected user peer")
    user_id = int(getattr(resolved, "peer_id"))
    try:
        return raw.entities.input_user(user_id)
    except EntityCacheError:
        await raw.prime_entities(limit=200, timeout=timeout)
        return raw.entities.input_user(user_id)


async def resolve_input_channel(raw: Any, channel: PeerRef, *, timeout: float) -> Any:
    resolved = await raw.resolve_peer(channel, timeout=timeout)
    if getattr(resolved, "peer_type", None) != "channel":
        raise ValueError("Expected channel peer")
    channel_id = int(getattr(resolved, "peer_id"))
    try:
        return raw.entities.input_channel(channel_id)
    except EntityCacheError:
        await raw.prime_entities(limit=200, timeout=timeout)
        return raw.entities.input_channel(channel_id)


async def resolve_input_dialog_peer(raw: Any, peer: PeerRef, *, timeout: float) -> InputDialogPeer:
    input_peer = await resolve_input_peer(raw, peer, timeout=timeout)
    return InputDialogPeer(peer=input_peer)


async def resolve_input_dialog_peers(
    raw: Any,
    peers: Sequence[PeerRef],
    *,
    timeout: float,
) -> list[InputDialogPeer]:
    return [await resolve_input_dialog_peer(raw, peer, timeout=timeout) for peer in peers]
