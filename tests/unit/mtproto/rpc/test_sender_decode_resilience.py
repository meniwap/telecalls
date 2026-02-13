from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass

import pytest

from telecraft.mtproto.rpc.sender import (
    MtprotoEncryptedSender,
    RpcDecodeError,
    TLCodecError,
    _PendingCall,
)
from telecraft.tl.codec import dumps
from telecraft.tl.generated.types import Pong

_RPC_RESULT_CONSTRUCTOR_ID = -212046591


@dataclass
class _FakeState:
    server_salt: bytes = b"\x00" * 8

    def decrypt_packet(self, packet: bytes, *, from_server: bool) -> bytes:
        return packet

    def encrypt_inner_message(self, inner: bytes, *, to_server: bool) -> bytes:
        return inner

    def next_seq_no(self, *, content_related: bool) -> int:
        return 0


class _FakeMsgIdGen:
    def __init__(self) -> None:
        self._next = 9000
        self.observed: list[int] = []

    def next(self) -> int:
        self._next += 4
        return self._next

    def observe(self, msg_id: int) -> None:
        self.observed.append(int(msg_id))


class _FakeTransport:
    def __init__(self, packets: list[bytes] | None = None) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        for packet in packets or []:
            self._queue.put_nowait(packet)
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> bytes:
        return await self._queue.get()


def _make_inner_packet(msg_id: int, body: bytes, *, seqno: int = 1) -> bytes:
    return struct.pack("<qii", int(msg_id), int(seqno), len(body)) + body


def _rpc_result_body(req_msg_id: int, result_payload: bytes) -> bytes:
    return (
        struct.pack("<i", _RPC_RESULT_CONSTRUCTOR_ID)
        + struct.pack("<q", int(req_msg_id))
        + result_payload
    )


def _build_sender_with_pending_calls() -> tuple[MtprotoEncryptedSender, _PendingCall, _PendingCall]:
    sender = MtprotoEncryptedSender(
        _FakeTransport(),
        state=_FakeState(),
        msg_id_gen=_FakeMsgIdGen(),
    )

    loop = asyncio.get_running_loop()
    call1 = _PendingCall(req_bytes=b"req1", future=loop.create_future())
    call1.msg_ids.add(101)
    sender._pending[101] = call1
    sender._sent[101] = (1, b"req1")

    call2 = _PendingCall(req_bytes=b"req2", future=loop.create_future())
    call2.msg_ids.add(202)
    sender._pending[202] = call2
    sender._sent[202] = (1, b"req2")

    return sender, call1, call2


def test_sender__decode_error__fails_only_relevant_call() -> None:
    async def _run() -> None:
        sender, call1, call2 = _build_sender_with_pending_calls()

        sender._fail_decode_for_req_ids(
            req_msg_ids={101},
            outer_msg_id=7001,
            error=TLCodecError("unknown constructor"),
        )

        with pytest.raises(RpcDecodeError):
            await call1.future
        assert not call2.future.done()

    asyncio.run(_run())


def test_sender__decode_error__loop_continues_and_next_call_succeeds() -> None:
    async def _run() -> None:
        req1 = 101
        req2 = 202

        bad_body = _rpc_result_body(req1, struct.pack("<i", 6))
        good_body = _rpc_result_body(req2, dumps(Pong(msg_id=req2, ping_id=4040)))

        transport = _FakeTransport(
            [
                _make_inner_packet(7001, bad_body),
                _make_inner_packet(7002, good_body),
            ]
        )
        sender = MtprotoEncryptedSender(
            transport,
            state=_FakeState(),
            msg_id_gen=_FakeMsgIdGen(),
        )

        loop = asyncio.get_running_loop()
        call1 = _PendingCall(req_bytes=b"req1", future=loop.create_future())
        call1.msg_ids.add(req1)
        sender._pending[req1] = call1
        sender._sent[req1] = (1, b"req1")

        call2 = _PendingCall(req_bytes=b"req2", future=loop.create_future())
        call2.msg_ids.add(req2)
        sender._pending[req2] = call2
        sender._sent[req2] = (1, b"req2")

        task = asyncio.create_task(sender._recv_loop())
        try:
            with pytest.raises(RpcDecodeError):
                await asyncio.wait_for(call1.future, timeout=1.0)

            result = await asyncio.wait_for(call2.future, timeout=1.0)
            assert isinstance(result, Pong)
            assert int(result.ping_id) == 4040
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(_run())


def test_sender__decode_error__does_not_drop_all_pending() -> None:
    async def _run() -> None:
        sender, call1, call2 = _build_sender_with_pending_calls()

        sender._fail_decode_for_req_ids(
            req_msg_ids={101},
            outer_msg_id=7001,
            error=TLCodecError("unknown constructor"),
        )

        assert call1.future.done()
        assert 202 in sender._pending
        assert sender._pending[202] is call2
        assert not call2.future.done()

    asyncio.run(_run())
