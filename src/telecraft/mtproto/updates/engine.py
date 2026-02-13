from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from telecraft.mtproto.updates.state import UpdatesState
from telecraft.tl.generated.functions import (
    UpdatesGetChannelDifference,
    UpdatesGetDifference,
    UpdatesGetState,
)
from telecraft.tl.generated.types import (
    ChannelMessagesFilterEmpty,
    UpdateChannel,
    Updates,
    UpdatesChannelDifference,
    UpdatesChannelDifferenceEmpty,
    UpdatesChannelDifferenceTooLong,
    UpdatesCombined,
    UpdatesDifference,
    UpdatesDifferenceEmpty,
    UpdatesDifferenceSlice,
    UpdatesDifferenceTooLong,
    UpdateShort,
    UpdateShortChatMessage,
    UpdateShortMessage,
    UpdatesTooLong,
)
from telecraft.tl.generated.types import (
    UpdatesState as TlUpdatesState,
)

logger = logging.getLogger(__name__)


class UpdatesEngineError(Exception):
    pass


@dataclass(slots=True)
class AppliedUpdates:
    updates: list[Any]
    new_messages: list[Any]
    users: list[Any]
    chats: list[Any]


class UpdatesEngine:
    """
    Minimal MTProto updates engine:
    - Initializes state with updates.getState
    - Applies incoming updates objects, detecting simple pts gaps
    - Fills gaps using updates.getDifference (handles slice/tooLong)
    - Fetches per-channel updates using updates.getChannelDifference when server sends updateChannel

    This is intentionally minimal (no full per-channel state persistence yet).
    """

    def __init__(
        self,
        *,
        invoke_api: Callable[[Any], Awaitable[Any]],
        resolve_input_channel: Callable[[int], Any] | None = None,
        pts_total_limit: int | None = None,
    ) -> None:
        self._invoke_api = invoke_api
        self._resolve_input_channel = resolve_input_channel
        self._pts_total_limit = pts_total_limit
        self.state: UpdatesState | None = None
        self._channel_pts: dict[int, int] = {}

    async def initialize(self, *, initial_state: UpdatesState | None = None) -> UpdatesState:
        """
        Initialize updates engine state.

        - If initial_state is provided (e.g. loaded from disk), it will be used directly.
        - Otherwise we fetch updates.getState from the server.
        """
        if initial_state is not None:
            self.state = initial_state
            return self.state
        res = await self._invoke_api(UpdatesGetState())
        self.state = UpdatesState.from_tl(res)
        return self.state

    async def apply(self, obj: Any) -> AppliedUpdates:
        if self.state is None:
            raise UpdatesEngineError("Updates engine not initialized (call initialize())")

        # updatesTooLong: must fetch difference.
        if isinstance(obj, UpdatesTooLong):
            return await self._fetch_difference()

        # updateChannel: indicates we should fetch channelDifference for channel-specific updates.
        if isinstance(obj, UpdateChannel):
            cid = getattr(obj, "channel_id", None)
            if isinstance(cid, int):
                logger.info("updateChannel received; fetching channelDifference channel_id=%s", cid)
                return await self._fetch_channel_difference(int(cid))
            return AppliedUpdates(updates=[obj], new_messages=[], users=[], chats=[])

        # updateShortMessage / updateShortChatMessage: contains pts/pts_count.
        if isinstance(obj, (UpdateShortMessage, UpdateShortChatMessage)):
            pts = int(cast(int, obj.pts))
            pts_count = int(cast(int, obj.pts_count))
            if pts != self.state.pts + pts_count:
                logger.info(
                    "PTS gap detected (have=%s, got=%s, count=%s); fetching difference",
                    self.state.pts,
                    pts,
                    pts_count,
                )
                return await self._fetch_difference()
            self.state.pts = pts
            self.state.date = int(cast(int, obj.date))
            return AppliedUpdates(updates=[obj], new_messages=[], users=[], chats=[])

        # updateShort: wraps a single Update + date.
        if isinstance(obj, UpdateShort):
            self.state.date = int(cast(int, obj.date))
            inner = obj.update
            return await self.apply(inner)

        # updates / updatesCombined: list of updates, plus seq+date.
        if isinstance(obj, (Updates, UpdatesCombined)):
            self.state.date = int(cast(int, obj.date))
            self.state.seq = int(cast(int, obj.seq))
            updates_list = cast(list[Any], obj.updates)
            users = cast(list[Any], getattr(obj, "users", []))
            chats = cast(list[Any], getattr(obj, "chats", []))

            out_updates: list[Any] = []
            out_messages: list[Any] = []
            out_users: list[Any] = list(users)
            out_chats: list[Any] = list(chats)

            # If we see updateChannel, pull channelDifference and inline its results.
            for u in updates_list:
                if isinstance(u, UpdateChannel):
                    cid = getattr(u, "channel_id", None)
                    if isinstance(cid, int):
                        logger.info(
                            "updateChannel received (in Updates); fetching channelDifference channel_id=%s",  # noqa: E501
                            cid,
                        )
                        cd = await self._fetch_channel_difference(int(cid))
                        out_updates.extend(cd.updates)
                        out_messages.extend(cd.new_messages)
                        out_users.extend(cd.users)
                        out_chats.extend(cd.chats)
                    continue

            # If we see qts jumping forward, we likely missed participant/admin updates.
            max_qts: int | None = None
            for u in updates_list:
                q = getattr(u, "qts", None)
                if isinstance(q, int):
                    max_qts = q if max_qts is None else max(max_qts, q)
            if max_qts is not None and max_qts > (self.state.qts + 1):
                logger.info(
                    "QTS gap detected (have=%s, got=%s); fetching difference",
                    self.state.qts,
                    max_qts,
                )
                return await self._fetch_difference()

            # Best-effort pts tracking for updates that carry pts/pts_count.
            for u in updates_list:
                if isinstance(u, UpdateChannel):
                    continue
                self._apply_pts_from_update(u)
                self._apply_qts_from_update(u)
                out_updates.append(u)

            return AppliedUpdates(
                updates=out_updates,
                new_messages=out_messages,
                users=out_users,
                chats=out_chats,
            )

        # Best-effort: single Update that carries pts/pts_count.
        qts = getattr(obj, "qts", None)
        if isinstance(qts, int) and qts > (self.state.qts + 1):
            logger.info(
                "QTS gap detected (have=%s, got=%s); fetching difference",
                self.state.qts,
                qts,
            )
            return await self._fetch_difference()
        self._apply_pts_from_update(obj)
        self._apply_qts_from_update(obj)
        return AppliedUpdates(updates=[obj], new_messages=[], users=[], chats=[])

    def _apply_pts_from_update(self, update: Any) -> None:
        if self.state is None:
            return
        pts = getattr(update, "pts", None)
        pts_count = getattr(update, "pts_count", None)
        if isinstance(pts, int) and isinstance(pts_count, int):
            expected = self.state.pts + pts_count
            if pts != expected:
                # Gap will be handled when we see an updatesTooLong or higher-level wrapper.
                logger.debug(
                    "PTS mismatch in update (%s): have=%s expected=%s got=%s",
                    getattr(update, "TL_NAME", type(update).__name__),
                    self.state.pts,
                    expected,
                    pts,
                )
                return
            self.state.pts = pts
            return

    def _apply_qts_from_update(self, update: Any) -> None:
        """
        Best-effort qts tracking for updates that carry qts (e.g. participant updates).

        We intentionally keep this permissive:
        - if qts increases, accept it
        - we do not enforce strict +1 sequencing yet (minimal engine)
        """
        if self.state is None:
            return
        qts = getattr(update, "qts", None)
        if not isinstance(qts, int):
            return
        if qts > self.state.qts:
            self.state.qts = int(qts)
            return

    async def _fetch_difference(self) -> AppliedUpdates:
        if self.state is None:
            raise UpdatesEngineError("No state")

        out_updates: list[Any] = []
        out_messages: list[Any] = []
        out_users: list[Any] = []
        out_chats: list[Any] = []

        while True:
            req = UpdatesGetDifference(
                flags=0 if self._pts_total_limit is None else 1,
                pts=self.state.pts,
                pts_limit=None,
                pts_total_limit=self._pts_total_limit,
                date=self.state.date,
                qts=self.state.qts,
                qts_limit=None,
            )
            diff = await self._invoke_api(req)

            if isinstance(diff, UpdatesDifferenceEmpty):
                self.state.date = int(cast(int, diff.date))
                self.state.seq = int(cast(int, diff.seq))
                return AppliedUpdates(
                    updates=out_updates,
                    new_messages=out_messages,
                    users=out_users,
                    chats=out_chats,
                )

            if isinstance(diff, UpdatesDifferenceTooLong):
                # Reset state via getState (simple & safe).
                logger.warning("differenceTooLong received; reinitializing updates state")
                _ = int(cast(int, diff.pts))
                await self.initialize()
                return AppliedUpdates(
                    updates=out_updates,
                    new_messages=out_messages,
                    users=out_users,
                    chats=out_chats,
                )

            if isinstance(diff, UpdatesDifference):
                out_messages.extend(cast(list[Any], diff.new_messages))
                out_updates.extend(cast(list[Any], diff.other_updates))
                out_users.extend(cast(list[Any], diff.users))
                out_chats.extend(cast(list[Any], diff.chats))
                self.state = UpdatesState.from_tl(cast(TlUpdatesState, diff.state))
                return AppliedUpdates(
                    updates=out_updates,
                    new_messages=out_messages,
                    users=out_users,
                    chats=out_chats,
                )

            if isinstance(diff, UpdatesDifferenceSlice):
                out_messages.extend(cast(list[Any], diff.new_messages))
                out_updates.extend(cast(list[Any], diff.other_updates))
                out_users.extend(cast(list[Any], diff.users))
                out_chats.extend(cast(list[Any], diff.chats))
                self.state = UpdatesState.from_tl(cast(TlUpdatesState, diff.intermediate_state))
                continue

            raise UpdatesEngineError(
                f"Unexpected updates.getDifference result: {type(diff).__name__}"
            )

    async def _fetch_channel_difference(self, channel_id: int) -> AppliedUpdates:
        """
        Fetch channel-specific updates (used when server sends updateChannel).
        """
        if self._resolve_input_channel is None:
            logger.info(
                "No InputChannel resolver; skipping channelDifference channel_id=%s",
                channel_id,
            )
            return AppliedUpdates(updates=[], new_messages=[], users=[], chats=[])
        try:
            input_channel = self._resolve_input_channel(int(channel_id))
        except Exception as ex:  # noqa: BLE001
            logger.info("Failed to resolve InputChannel; skipping channelDifference", exc_info=ex)
            return AppliedUpdates(updates=[], new_messages=[], users=[], chats=[])

        pts = int(self._channel_pts.get(int(channel_id), 1))
        force = int(channel_id) not in self._channel_pts

        out_updates: list[Any] = []
        out_messages: list[Any] = []
        out_users: list[Any] = []
        out_chats: list[Any] = []

        while True:
            logger.info(
                "getChannelDifference(channel_id=%s, pts=%s, force=%s)",
                channel_id,
                pts,
                force,
            )
            try:
                diff = await self._invoke_api(
                    UpdatesGetChannelDifference(
                        flags=0,
                        force=force,
                        channel=input_channel,
                        filter=ChannelMessagesFilterEmpty(),
                        pts=int(pts),
                        limit=100,
                    )
                )
            except Exception as ex:  # noqa: BLE001
                logger.info("getChannelDifference failed (channel_id=%s)", channel_id, exc_info=ex)
                return AppliedUpdates(updates=[], new_messages=[], users=[], chats=[])

            if isinstance(diff, UpdatesChannelDifferenceEmpty):
                logger.info(
                    "channelDifferenceEmpty(channel_id=%s, pts=%s, final=%s)",
                    channel_id,
                    getattr(diff, "pts", None),
                    getattr(diff, "final", None),
                )
                pts = int(cast(int, diff.pts))
                self._channel_pts[int(channel_id)] = int(pts)
                return AppliedUpdates(
                    updates=out_updates,
                    new_messages=out_messages,
                    users=out_users,
                    chats=out_chats,
                )

            if isinstance(diff, UpdatesChannelDifference):
                logger.info(
                    "channelDifference(channel_id=%s, pts=%s, final=%s, msgs=%s, upd=%s)",
                    channel_id,
                    getattr(diff, "pts", None),
                    getattr(diff, "final", None),
                    len(cast(list[Any], diff.new_messages)),
                    len(cast(list[Any], diff.other_updates)),
                )
                pts = int(cast(int, diff.pts))
                out_messages.extend(cast(list[Any], diff.new_messages))
                out_updates.extend(cast(list[Any], diff.other_updates))
                out_chats.extend(cast(list[Any], diff.chats))
                out_users.extend(cast(list[Any], diff.users))
                self._channel_pts[int(channel_id)] = int(pts)
                if bool(getattr(diff, "final", False)):
                    return AppliedUpdates(
                        updates=out_updates,
                        new_messages=out_messages,
                        users=out_users,
                        chats=out_chats,
                    )
                force = False
                continue

            if isinstance(diff, UpdatesChannelDifferenceTooLong):
                logger.info(
                    "channelDifferenceTooLong(channel_id=%s, final=%s, msgs=%s)",
                    channel_id,
                    getattr(diff, "final", None),
                    len(cast(list[Any], diff.messages)),
                )
                # Best-effort: use dialog.pts if present.
                dlg = getattr(diff, "dialog", None)
                dlg_pts = getattr(dlg, "pts", None)
                if isinstance(dlg_pts, int):
                    pts = int(dlg_pts)
                    self._channel_pts[int(channel_id)] = int(pts)
                out_messages.extend(cast(list[Any], diff.messages))
                out_chats.extend(cast(list[Any], diff.chats))
                out_users.extend(cast(list[Any], diff.users))
                # If we managed to recover a pts, immediately try again to get other_updates.
                if isinstance(dlg_pts, int):
                    force = False
                    continue
                return AppliedUpdates(
                    updates=out_updates,
                    new_messages=out_messages,
                    users=out_users,
                    chats=out_chats,
                )

            raise UpdatesEngineError(
                f"Unexpected updates.getChannelDifference result: {type(diff).__name__}"
            )
