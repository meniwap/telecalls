from __future__ import annotations

from telecraft.client.calls.proto import (
    PacketHeader,
    ack_mask_contains,
    decode_short_packet,
    encode_short_packet,
    parse_extras_blob,
    update_rx_seq,
)


def test_short_packet_encode_decode_roundtrip() -> None:
    extras = bytes(
        [
            2,  # count
            2,
            1,
            0x07,
            2,
            4,
            0x02,
        ]
    )
    payload = b"voice-bytes"
    header = PacketHeader(pkt_type=1, ack_id=19, seq=20, recent_mask=0xA5, flags=0)

    encoded = encode_short_packet(header, payload, extras=extras)
    decoded_header, decoded_extras, decoded_payload = decode_short_packet(encoded)

    assert decoded_header.pkt_type == 1
    assert decoded_header.ack_id == 19
    assert decoded_header.seq == 20
    assert decoded_header.recent_mask == 0xA5
    assert decoded_payload == payload
    assert parse_extras_blob(decoded_extras) == [(1, b"\x07"), (4, b"\x02")]


def test_update_rx_seq_tracks_lost_and_duplicates() -> None:
    first = update_rx_seq(0, 0, 100)
    assert first.current_last_seq == 100
    assert first.advanced is True

    jump = update_rx_seq(first.current_last_seq, first.current_recent_mask, 104)
    assert jump.current_last_seq == 104
    assert jump.lost_count_increment == 3

    duplicate = update_rx_seq(jump.current_last_seq, jump.current_recent_mask, 104)
    assert duplicate.duplicate_or_old is True


def test_ack_mask_contains_recent_packets() -> None:
    ack_seq = 500
    mask = 0b101  # seq 499 and 497 acked, 498 missing

    assert ack_mask_contains(ack_seq, mask, 500)
    assert ack_mask_contains(ack_seq, mask, 499)
    assert not ack_mask_contains(ack_seq, mask, 498)
    assert ack_mask_contains(ack_seq, mask, 497)
