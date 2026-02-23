from __future__ import annotations

from hashlib import sha256

# Stable palette for deterministic visual-key display.
_EMOJI_PALETTE: tuple[str, ...] = (
    "😀",
    "😁",
    "😂",
    "😅",
    "😇",
    "🙂",
    "😉",
    "😍",
    "😘",
    "😎",
    "🤓",
    "🤖",
    "👻",
    "👽",
    "🎃",
    "🔥",
    "⚡",
    "🌈",
    "☀️",
    "🌙",
    "⭐",
    "🌍",
    "🌊",
    "🌱",
    "🌳",
    "🍀",
    "🌸",
    "🍎",
    "🍕",
    "🍩",
    "⚽",
    "🏀",
    "🎯",
    "🎮",
    "🎵",
    "🎧",
    "📷",
    "📚",
    "✈️",
    "🚀",
    "🚲",
    "🚗",
    "🚢",
    "⏰",
    "🔒",
    "🔑",
    "🧩",
    "🧠",
    "💎",
    "🛡️",
    "🧭",
    "🛰️",
    "🧪",
    "💡",
    "🧱",
    "🏔️",
    "🌋",
    "🦊",
    "🐼",
    "🐬",
    "🦉",
    "🐙",
    "🦄",
    "🐢",
)


def derive_call_emojis(auth_key: bytes, *, count: int = 4) -> tuple[str, str, str, str]:
    if not isinstance(auth_key, (bytes, bytearray)) or not auth_key:
        raise ValueError("auth_key must be non-empty bytes")
    if count != 4:
        raise ValueError("count must be 4 for call visual key")

    digest = sha256(bytes(auth_key)).digest()
    out: list[str] = []
    palette_len = len(_EMOJI_PALETTE)
    for idx in range(count):
        offset = idx * 2
        value = int.from_bytes(digest[offset : offset + 2], "big")
        out.append(_EMOJI_PALETTE[value % palette_len])
    return (out[0], out[1], out[2], out[3])


def fingerprint_to_hex(key_fingerprint: int) -> str:
    raw = int(key_fingerprint).to_bytes(8, "little", signed=True)
    return raw.hex()
