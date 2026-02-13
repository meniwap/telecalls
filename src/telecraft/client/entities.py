from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from telecraft.client.peers import Peer, PeerType, normalize_phone, normalize_username
from telecraft.tl.generated.types import (
    InputChannel,
    InputPeerChannel,
    InputPeerChat,
    InputPeerSelf,
    InputPeerUser,
    InputUser,
)


class EntityCacheError(Exception):
    pass


class EntityCacheStorageError(Exception):
    pass


_ENTITY_CACHE_VERSION = 2


def _decode_str(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


@dataclass(slots=True)
class EntityCache:
    """
    Minimal entity cache for building InputPeer*.

    For users/channels, Telegram requires `access_hash` to build InputPeerUser/InputPeerChannel.
    """

    user_access_hash: dict[int, int] = field(default_factory=dict)
    channel_access_hash: dict[int, int] = field(default_factory=dict)
    # High-level maps to enable resolve-by-string without hitting the network.
    username_to_peer: dict[str, tuple[PeerType, int]] = field(default_factory=dict)
    phone_to_user_id: dict[str, int] = field(default_factory=dict)

    def ingest_users(self, users: list[Any]) -> None:
        for u in users:
            uid = getattr(u, "id", None)
            if not isinstance(uid, int):
                continue
            ah = getattr(u, "access_hash", None)
            if isinstance(ah, int) and ah != 0:
                self.user_access_hash[int(uid)] = int(ah)
            uname = _decode_str(getattr(u, "username", None))
            if uname:
                self.username_to_peer[normalize_username(uname)] = ("user", int(uid))
            # Some layers provide `usernames: Vector<Username>`; best-effort ingest.
            usernames = getattr(u, "usernames", None)
            if isinstance(usernames, list):
                for un in usernames:
                    s = _decode_str(getattr(un, "username", None))
                    if s:
                        self.username_to_peer[normalize_username(s)] = ("user", int(uid))
            phone = _decode_str(getattr(u, "phone", None))
            if phone:
                self.phone_to_user_id[normalize_phone(phone)] = int(uid)

    def ingest_chats(self, chats: list[Any]) -> None:
        for c in chats:
            # channels need access_hash; basic chats don't.
            tl_name = getattr(c, "TL_NAME", None)
            if tl_name in {"channel", "channelForbidden"}:
                cid = getattr(c, "id", None)
                if not isinstance(cid, int):
                    continue
                ah = getattr(c, "access_hash", None)
                if isinstance(ah, int) and ah != 0:
                    self.channel_access_hash[int(cid)] = int(ah)
                uname = _decode_str(getattr(c, "username", None))
                if uname:
                    self.username_to_peer[normalize_username(uname)] = ("channel", int(cid))
                usernames = getattr(c, "usernames", None)
                if isinstance(usernames, list):
                    for un in usernames:
                        s = _decode_str(getattr(un, "username", None))
                        if s:
                            self.username_to_peer[normalize_username(s)] = ("channel", int(cid))

    def input_peer_self(self) -> InputPeerSelf:
        return InputPeerSelf()

    def input_peer(self, peer: Peer) -> Any:
        if peer.peer_type == "user":
            return self.input_peer_user(peer.peer_id)
        if peer.peer_type == "channel":
            return self.input_peer_channel(peer.peer_id)
        if peer.peer_type == "chat":
            return self.input_peer_chat(peer.peer_id)
        raise EntityCacheError(f"Unknown peer_type: {peer.peer_type!r}")

    def input_peer_user(self, user_id: int) -> InputPeerUser:
        ah = self.user_access_hash.get(int(user_id))
        if ah is None:
            raise EntityCacheError(f"Unknown user access_hash for user_id={user_id}")
        return InputPeerUser(user_id=int(user_id), access_hash=int(ah))

    def input_user(self, user_id: int) -> InputUser:
        """
        Build InputUser (used by some methods like channels.editAdmin).
        """
        ah = self.user_access_hash.get(int(user_id))
        if ah is None:
            raise EntityCacheError(f"Unknown user access_hash for user_id={user_id}")
        return InputUser(user_id=int(user_id), access_hash=int(ah))

    def input_peer_channel(self, channel_id: int) -> InputPeerChannel:
        ah = self.channel_access_hash.get(int(channel_id))
        if ah is None:
            raise EntityCacheError(f"Unknown channel access_hash for channel_id={channel_id}")
        return InputPeerChannel(channel_id=int(channel_id), access_hash=int(ah))

    def input_channel(self, channel_id: int) -> InputChannel:
        """
        Build InputChannel (used for updates.getChannelDifference and some channels.* methods).
        """
        ah = self.channel_access_hash.get(int(channel_id))
        if ah is None:
            raise EntityCacheError(f"Unknown channel access_hash for channel_id={channel_id}")
        return InputChannel(channel_id=int(channel_id), access_hash=int(ah))

    def input_peer_chat(self, chat_id: int) -> InputPeerChat:
        return InputPeerChat(chat_id=int(chat_id))

    def peer_from_username(self, username: str) -> Peer | None:
        key = normalize_username(username)
        tup = self.username_to_peer.get(key)
        if tup is None:
            return None
        pt, pid = tup
        return Peer(peer_type=pt, peer_id=int(pid))

    def peer_from_phone(self, phone: str) -> Peer | None:
        key = normalize_phone(phone)
        uid = self.phone_to_user_id.get(key)
        if uid is None:
            return None
        return Peer.user(int(uid))

    def to_json_dict(self) -> dict[str, object]:
        return {
            "version": _ENTITY_CACHE_VERSION,
            "user_access_hash": {str(k): int(v) for k, v in self.user_access_hash.items()},
            "channel_access_hash": {str(k): int(v) for k, v in self.channel_access_hash.items()},
            "username_to_peer": {
                str(k): [str(pt), int(pid)] for k, (pt, pid) in self.username_to_peer.items()
            },
            "phone_to_user_id": {str(k): int(v) for k, v in self.phone_to_user_id.items()},
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, object]) -> EntityCache:
        try:
            version_obj = data.get("version", _ENTITY_CACHE_VERSION)
            if not isinstance(version_obj, (int, str)):
                raise EntityCacheStorageError("Invalid version")
            version = int(version_obj)
            if version not in {1, _ENTITY_CACHE_VERSION}:
                raise EntityCacheStorageError(f"Unsupported entity cache version: {version}")

            ua = data.get("user_access_hash", {})
            ca = data.get("channel_access_hash", {})
            if not isinstance(ua, dict) or not isinstance(ca, dict):
                raise EntityCacheStorageError("Invalid cache dicts")

            user_access_hash: dict[int, int] = {}
            for k, v in ua.items():
                if not isinstance(k, str) or not isinstance(v, (int, str)):
                    continue
                user_access_hash[int(k)] = int(v)

            channel_access_hash: dict[int, int] = {}
            for k, v in ca.items():
                if not isinstance(k, str) or not isinstance(v, (int, str)):
                    continue
                channel_access_hash[int(k)] = int(v)

            username_to_peer: dict[str, tuple[PeerType, int]] = {}
            phone_to_user_id: dict[str, int] = {}

            if version >= 2:
                utp = data.get("username_to_peer", {})
                if isinstance(utp, dict):
                    for k, v in utp.items():
                        if not isinstance(k, str) or not isinstance(v, list) or len(v) != 2:
                            continue
                        pt, pid = v[0], v[1]
                        if pt not in {"user", "chat", "channel"}:
                            continue
                        if not isinstance(pid, (int, str)):
                            continue
                        username_to_peer[str(k)] = (cast(PeerType, pt), int(pid))

                ptu = data.get("phone_to_user_id", {})
                if isinstance(ptu, dict):
                    for k, v in ptu.items():
                        if not isinstance(k, str) or not isinstance(v, (int, str)):
                            continue
                        phone_to_user_id[str(k)] = int(v)
        except Exception as e:  # noqa: BLE001
            raise EntityCacheStorageError("Invalid entity cache JSON shape") from e

        out = cls()
        out.user_access_hash = user_access_hash
        out.channel_access_hash = channel_access_hash
        out.username_to_peer = username_to_peer
        out.phone_to_user_id = phone_to_user_id
        return out


def load_entity_cache_file(path: str | Path) -> EntityCache:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise EntityCacheStorageError(f"Failed to parse entity cache JSON: {p}") from e
    if not isinstance(data, dict):
        raise EntityCacheStorageError("Entity cache JSON must be an object")
    return EntityCache.from_json_dict(data)


def save_entity_cache_file(path: str | Path, cache: EntityCache) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(cache.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)
