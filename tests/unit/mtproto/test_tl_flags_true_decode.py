from __future__ import annotations

from telecraft.tl.codec import dumps, loads
from telecraft.tl.generated.types import MessagePeerReaction, PeerUser, ReactionEmoji


def test_flags_true_decodes_to_false_when_bit_absent() -> None:
    obj = MessagePeerReaction(
        flags=0,
        big=False,
        unread=False,
        my=False,
        peer_id=PeerUser(user_id=123),
        date=0,
        reaction=ReactionEmoji(emoticon="❤️"),
    )
    roundtripped = loads(dumps(obj))
    assert isinstance(roundtripped, MessagePeerReaction)
    assert roundtripped.my is False


def test_flags_true_decodes_to_true_when_bit_present() -> None:
    obj = MessagePeerReaction(
        flags=0,
        big=False,
        unread=False,
        my=True,
        peer_id=PeerUser(user_id=123),
        date=0,
        reaction=ReactionEmoji(emoticon="❤️"),
    )
    roundtripped = loads(dumps(obj))
    assert isinstance(roundtripped, MessagePeerReaction)
    assert roundtripped.my is True
