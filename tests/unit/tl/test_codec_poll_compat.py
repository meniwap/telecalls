from __future__ import annotations

from pathlib import Path

from telecraft.tl.codec import TLWriter, loads
from telecraft.tl.generated.types import (
    MessageMediaPoll,
    Poll,
    PollAnswer,
    PollResults,
    TextWithEntities,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tl"


def _fixture_bytes(name: str) -> bytes:
    return (_FIXTURES_DIR / name).read_bytes()


def _sample_text(text: str) -> TextWithEntities:
    return TextWithEntities(text=text.encode("utf-8"), entities=[])


def test_codec__poll_message__does_not_misconsume_poll_results_constructor() -> None:
    writer = TLWriter()
    writer.write_int(MessageMediaPoll.TL_ID)

    # Craft a poll where flags say close_date exists, but close_date is intentionally omitted.
    writer.write_int(Poll.TL_ID)
    writer.write_long(777)
    writer.write_int(1 << 5)
    writer.write_object(_sample_text("Question"))
    writer.write_value(
        "Vector<PollAnswer>",
        [
            PollAnswer(
                text=_sample_text("Answer"),
                option=b"A",
            )
        ],
    )

    # Next object starts immediately with pollResults constructor.
    writer.write_object(
        PollResults(
            flags=0,
            min=False,
            results=None,
            total_voters=None,
            recent_voters=None,
            solution=None,
            solution_entities=None,
        )
    )

    media = loads(writer.to_bytes())
    assert getattr(media, "TL_NAME", "") == "messageMediaPoll"
    assert media.poll.close_date is None
    assert int(media.poll.flags) & (1 << 5) == 0
    assert getattr(media.results, "TL_NAME", "") == "pollResults"


def test_codec__poll_results__bare_fallback_when_boxed_fails() -> None:
    payload = _fixture_bytes("bad_payload_20260212T222834Z_1.bin")
    decoded = loads(payload)

    assert getattr(decoded, "TL_NAME", "") == "messages.channelMessages"
    poll_medias = [
        m.media
        for m in getattr(decoded, "messages", [])
        if getattr(getattr(m, "media", None), "TL_NAME", "") == "messageMediaPoll"
    ]
    assert poll_medias
    assert all(getattr(media.results, "TL_NAME", "") == "pollResults" for media in poll_medias)
    assert any(int(getattr(media.results, "flags", 0)) == 6 for media in poll_medias)
