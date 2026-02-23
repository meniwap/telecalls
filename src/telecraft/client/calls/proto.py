from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_VERSION = 9
MIN_PROTOCOL_VERSION = 3

PKT_INIT = 1
PKT_INIT_ACK = 2
PKT_STREAM_STATE = 3
PKT_STREAM_DATA = 4
PKT_PING = 6
PKT_PONG = 7
PKT_NOP = 14

XPFLAG_HAS_EXTRA = 1
XPFLAG_HAS_RECV_TS = 2

EXTRA_TYPE_STREAM_FLAGS = 1
EXTRA_TYPE_NETWORK_CHANGED = 4


@dataclass(frozen=True, slots=True)
class PacketHeader:
    pkt_type: int
    ack_id: int
    seq: int
    recent_mask: int
    flags: int = 0


@dataclass(frozen=True, slots=True)
class RxSeqUpdate:
    previous_last_seq: int
    current_last_seq: int
    current_recent_mask: int
    lost_count_increment: int
    advanced: bool
    duplicate_or_old: bool


def encode_short_packet(
    header: PacketHeader,
    payload: bytes,
    *,
    extras: bytes = b"",
) -> bytes:
    flags = header.flags | (XPFLAG_HAS_EXTRA if extras else 0)
    return (
        bytes([header.pkt_type & 0xFF])
        + int(header.ack_id & 0xFFFFFFFF).to_bytes(4, "little")
        + int(header.seq & 0xFFFFFFFF).to_bytes(4, "little")
        + int(header.recent_mask & 0xFFFFFFFF).to_bytes(4, "little")
        + bytes([flags & 0xFF])
        + bytes(extras)
        + bytes(payload)
    )


def decode_short_packet(packet: bytes) -> tuple[PacketHeader, bytes, bytes]:
    if len(packet) < 14:
        raise ValueError("short packet is too small")

    pkt_type = packet[0]
    ack_id = int.from_bytes(packet[1:5], "little")
    seq = int.from_bytes(packet[5:9], "little")
    recent_mask = int.from_bytes(packet[9:13], "little")
    flags = packet[13]

    cursor = 14
    extras = b""
    if flags & XPFLAG_HAS_EXTRA:
        if cursor >= len(packet):
            raise ValueError("short packet missing extras")
        count = packet[cursor]
        cursor += 1
        start = cursor - 1
        for _ in range(count):
            if cursor >= len(packet):
                raise ValueError("short packet malformed extras")
            item_len = packet[cursor]
            cursor += 1
            if item_len == 0 or cursor + item_len > len(packet):
                raise ValueError("short packet malformed extras length")
            cursor += item_len
        extras = packet[start:cursor]

    payload = packet[cursor:]
    return (
        PacketHeader(
            pkt_type=pkt_type,
            ack_id=ack_id,
            seq=seq,
            recent_mask=recent_mask,
            flags=flags,
        ),
        extras,
        payload,
    )


def parse_extras_blob(blob: bytes) -> list[tuple[int, bytes]]:
    if not blob:
        return []
    cursor = 0
    if cursor >= len(blob):
        raise ValueError("extras blob is empty")
    count = blob[cursor]
    cursor += 1

    out: list[tuple[int, bytes]] = []
    for _ in range(count):
        if cursor >= len(blob):
            raise ValueError("extras blob truncated")
        item_len = blob[cursor]
        cursor += 1
        if item_len == 0 or cursor + item_len > len(blob):
            raise ValueError("extras blob malformed")
        item_type = blob[cursor]
        cursor += 1
        data_len = item_len - 1
        data = blob[cursor : cursor + data_len]
        cursor += data_len
        out.append((item_type, data))

    if cursor != len(blob):
        raise ValueError("extras blob trailing bytes")
    return out


def update_rx_seq(last_remote_seq: int, recent_mask: int, incoming_seq: int) -> RxSeqUpdate:
    previous = int(last_remote_seq)
    current_last = int(last_remote_seq)
    current_mask = int(recent_mask) & 0xFFFFFFFF
    lost_increment = 0
    advanced = False
    duplicate_or_old = False

    if current_last == 0 and current_mask == 0:
        return RxSeqUpdate(
            previous_last_seq=previous,
            current_last_seq=int(incoming_seq),
            current_recent_mask=0,
            lost_count_increment=0,
            advanced=True,
            duplicate_or_old=False,
        )

    if incoming_seq > current_last:
        delta = incoming_seq - current_last
        if delta >= 32:
            current_mask = 1
        else:
            current_mask = ((current_mask << delta) | (1 << (delta - 1))) & 0xFFFFFFFF
        if delta > 1:
            lost_increment = delta - 1
        current_last = incoming_seq
        advanced = True
    else:
        back = current_last - incoming_seq
        if back == 0:
            duplicate_or_old = True
        elif back <= 32:
            current_mask = current_mask | (1 << (back - 1))
        else:
            duplicate_or_old = True

    return RxSeqUpdate(
        previous_last_seq=previous,
        current_last_seq=current_last,
        current_recent_mask=current_mask,
        lost_count_increment=lost_increment,
        advanced=advanced,
        duplicate_or_old=duplicate_or_old,
    )


def ack_mask_contains(ack_seq: int, ack_mask: int, candidate_seq: int) -> bool:
    if candidate_seq == ack_seq:
        return True
    if candidate_seq > ack_seq:
        return False
    delta = ack_seq - candidate_seq
    if delta <= 0 or delta > 32:
        return False
    return bool((ack_mask >> (delta - 1)) & 1)
