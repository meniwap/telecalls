from __future__ import annotations

import asyncio
from typing import Any

from telecraft.mtproto.updates.engine import UpdatesEngine
from telecraft.mtproto.updates.state import UpdatesState
from telecraft.tl.generated.types import (
    ChannelMessagesFilterEmpty,
    InputChannel,
    UpdateChannel,
    UpdatesChannelDifferenceEmpty,
)


def test_update_channel_triggers_get_channel_difference() -> None:
    calls: list[Any] = []

    async def invoke(req: Any) -> Any:
        calls.append(req)
        # Return "empty" diff with same pts.
        return UpdatesChannelDifferenceEmpty(flags=0, final=True, pts=1, timeout=None)

    eng = UpdatesEngine(
        invoke_api=invoke,
        resolve_input_channel=lambda cid: InputChannel(channel_id=int(cid), access_hash=123),
    )
    eng.state = UpdatesState(pts=1, qts=1, date=0, seq=0)

    async def _run() -> None:
        await eng.apply(UpdateChannel(channel_id=100))

    asyncio.run(_run())

    assert calls, "expected getChannelDifference call"
    req0 = calls[0]
    assert getattr(req0, "TL_NAME", None) == "updates.getChannelDifference"
    assert isinstance(getattr(req0, "filter", None), ChannelMessagesFilterEmpty)
