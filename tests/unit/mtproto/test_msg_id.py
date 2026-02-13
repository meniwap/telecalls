from __future__ import annotations

from telecraft.mtproto.core.msg_id import MsgIdGenerator


def test_msg_id_monotonic_and_divisible_by_4() -> None:
    gen = MsgIdGenerator()
    ids = [gen.next() for _ in range(100)]
    assert all(x % 4 == 0 for x in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_msg_id_observe_bumps_past_remote_ids() -> None:
    gen = MsgIdGenerator()
    first = gen.next()
    # Simulate server msg_id being ahead (can be 1/2/3 mod 4)
    gen.observe(first + 1_000_000_001)  # not divisible by 4
    nxt = gen.next()
    assert nxt % 4 == 0
    assert nxt > first + 1_000_000_001
