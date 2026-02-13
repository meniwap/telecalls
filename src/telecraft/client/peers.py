from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

PeerType: TypeAlias = Literal["user", "chat", "channel"]


@dataclass(frozen=True, slots=True)
class Peer:
    """
    High-level peer reference (Telethon/Pyrogram-style abstraction).

    - user: private chat with a user (requires access_hash to build InputPeerUser)
    - chat: basic group chat (InputPeerChat doesn't need access_hash)
    - channel: channel/supergroup (requires access_hash to build InputPeerChannel/InputChannel)
    """

    peer_type: PeerType
    peer_id: int

    @classmethod
    def user(cls, user_id: int) -> Peer:
        return cls("user", int(user_id))

    @classmethod
    def chat(cls, chat_id: int) -> Peer:
        return cls("chat", int(chat_id))

    @classmethod
    def channel(cls, channel_id: int) -> Peer:
        return cls("channel", int(channel_id))


PeerRef: TypeAlias = Peer | tuple[PeerType, int] | str | int


def normalize_username(username: str) -> str:
    u = username.strip()
    if not u:
        return ""
    if u.startswith("@"):
        u = u[1:]
    # Support common link forms:
    # - https://t.me/<username>
    # - t.me/<username>
    # - telegram.me/<username>
    for host in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if u.startswith(host):
            u = u[len(host) :]
            break
    # Trim path/query/fragment
    u = u.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
    return u.strip().lower()


def parse_peer_ref(s: str) -> PeerRef:
    """
    Parse common peer reference strings.

    Accepted:
    - "@username" / "t.me/username" / "https://t.me/username"
    - "+1555..." or "phone:+1555..."
    - "user:123" / "chat:123" / "channel:123"
    """
    raw = s.strip()
    if not raw:
        raise ValueError("empty peer ref")
    for prefix in ("user:", "chat:", "channel:"):
        if raw.startswith(prefix):
            pt = prefix[:-1]
            rest = raw[len(prefix) :].strip()
            return (pt, int(rest))  # type: ignore[return-value]
    if raw.startswith("phone:"):
        return normalize_phone(raw[len("phone:") :])
    if raw.startswith("+"):
        return normalize_phone(raw)
    # treat as username-ish
    u = normalize_username(raw)
    if u:
        return "@" + u
    return raw


def normalize_phone(phone: str) -> str:
    p = phone.strip()
    if not p:
        return p
    # Keep a leading '+' when provided; otherwise keep digits only.
    if p.startswith("+"):
        return "+" + "".join(ch for ch in p[1:] if ch.isdigit())
    return "".join(ch for ch in p if ch.isdigit())


def peer_from_tl_peer(peer_obj: object) -> Peer | None:
    """
    Convert a TL Peer (peerUser/peerChat/peerChannel) into Peer.
    """
    name = getattr(peer_obj, "TL_NAME", None)
    if name == "peerUser":
        v = getattr(peer_obj, "user_id", None)
        return Peer.user(int(v)) if isinstance(v, int) else None
    if name == "peerChat":
        v = getattr(peer_obj, "chat_id", None)
        return Peer.chat(int(v)) if isinstance(v, int) else None
    if name == "peerChannel":
        v = getattr(peer_obj, "channel_id", None)
        return Peer.channel(int(v)) if isinstance(v, int) else None
    return None
