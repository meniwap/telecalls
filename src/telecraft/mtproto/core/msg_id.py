from __future__ import annotations

import time


class MsgIdGenerator:
    """
    Generate MTProto message ids.

    Properties required by Telegram:
    - strictly increasing per session
    - divisible by 4
    - roughly based on unix time
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last = 0

    def observe(self, remote_msg_id: int) -> None:
        """
        Observe a remote message id and ensure future ids are higher.

        Telegram servers can return msg_id slightly ahead of local clock.
        Many flows (including the auth key exchange) require client msg_id
        to be strictly increasing over the whole session, so we bump our
        internal last value based on the highest observed remote msg_id.
        """

        # Client msg_id must be divisible by 4; servers' msg_id may be 1/2/3 mod 4.
        # Using floor(remote) keeps divisibility, and next() will add +4 if needed.
        remote_floor = int(remote_msg_id) & ~3
        if remote_floor > self._last:
            self._last = remote_floor

    def next(self) -> int:
        now = time.time()
        msg_id = int(now * (2**32))
        msg_id &= ~3  # divisible by 4
        if msg_id <= self._last:
            msg_id = self._last + 4
        self._last = msg_id
        return msg_id
