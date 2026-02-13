from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from telecraft.mtproto.auth.handshake import AuthHandshakeError, send_req_pq_multi
from telecraft.mtproto.core.msg_id import MsgIdGenerator
from telecraft.mtproto.core.unencrypted import UnencryptedMessage
from telecraft.tl.codec import dumps
from telecraft.tl.generated.types import ResPq


@dataclass(slots=True)
class FakeTransport:
    payloads: list[bytes]
    sent: list[bytes] = field(default_factory=list)

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> bytes:
        await asyncio.sleep(0)  # allow scheduling/cancellation in tests
        if not self.payloads:
            raise RuntimeError("No more payloads queued")
        return self.payloads.pop(0)


def _pack_unencrypted(obj: object, *, msg_id: int = 4) -> bytes:
    body = dumps(obj)
    return UnencryptedMessage(msg_id=msg_id, body=body).pack()


def test_send_req_pq_multi_ignores_quick_ack_frame() -> None:
    res = ResPq(
        nonce=b"\x01" * 16,
        server_nonce=b"\x02" * 16,
        pq=b"\x01\x43",
        server_public_key_fingerprints=[123],
    )
    transport = FakeTransport(payloads=[b"\x00\x00\x00\x00", _pack_unencrypted(res, msg_id=8)])
    out = asyncio.run(send_req_pq_multi(transport, MsgIdGenerator()))
    assert out == res
    assert transport.sent, "Expected at least one outgoing packet"


def test_send_req_pq_multi_fails_after_too_many_small_frames() -> None:
    # Quick-ack style small frames forever should not loop forever.
    transport = FakeTransport(payloads=[b"\x00\x00\x00\x00"] * 1000)
    with pytest.raises(AuthHandshakeError):
        asyncio.run(send_req_pq_multi(transport, MsgIdGenerator()))
