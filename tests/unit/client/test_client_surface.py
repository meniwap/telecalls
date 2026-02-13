from __future__ import annotations

import asyncio
from typing import Any

from telecraft.client import Client


class _Raw:
    def __init__(self) -> None:
        self.is_connected = False
        self.events: list[str] = []
        self._subscribers: list[asyncio.Queue[Any]] = []

    async def connect(self, *, timeout: float = 30.0) -> None:
        _ = timeout
        self.events.append("connect")
        self.is_connected = True

    async def close(self) -> None:
        self.events.append("close")
        self.is_connected = False

    async def get_me(self, *, timeout: float = 20.0) -> Any:
        _ = timeout
        self.events.append("get_me")
        return {"id": 1}

    async def send_code(self, phone_number: str, *, timeout: float = 20.0) -> Any:
        _ = (phone_number, timeout)
        self.events.append("send_code")
        return {"ok": True}

    async def sign_in(
        self,
        *,
        phone_number: str,
        phone_code_hash: str | bytes,
        phone_code: str,
        timeout: float = 20.0,
    ) -> Any:
        _ = (phone_number, phone_code_hash, phone_code, timeout)
        self.events.append("sign_in")
        return {"ok": True}

    async def check_password(self, password: str, *, timeout: float = 20.0) -> Any:
        _ = (password, timeout)
        self.events.append("check_password")
        return {"ok": True}

    async def start_updates(self, *, timeout: float = 20.0) -> None:
        _ = timeout
        self.events.append("start_updates")

    async def stop_updates(self) -> None:
        self.events.append("stop_updates")

    async def recv_update(self) -> Any:
        self.events.append("recv_update")
        return {"kind": "update"}

    async def resolve_peer(self, ref: Any, *, timeout: float = 20.0) -> Any:
        _ = (ref, timeout)
        self.events.append("resolve_peer")
        return {"peer": "ok"}

    async def resolve_username(
        self,
        username: str,
        *,
        timeout: float = 20.0,
        force: bool = False,
    ) -> Any:
        _ = (username, timeout, force)
        self.events.append("resolve_username")
        return {"peer": "ok"}

    async def resolve_phone(
        self,
        phone: str,
        *,
        timeout: float = 20.0,
        force: bool = False,
    ) -> Any:
        _ = (phone, timeout, force)
        self.events.append("resolve_phone")
        return {"peer": "ok"}

    async def prime_entities(
        self,
        *,
        limit: int = 100,
        folder_id: int | None = None,
        timeout: float = 20.0,
    ) -> None:
        _ = (limit, folder_id, timeout)
        self.events.append("prime_entities")

    def subscribe_updates(self, *, maxsize: int = 1024) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        self.events.append("subscribe_updates")
        return q

    def unsubscribe_updates(self, q: asyncio.Queue[Any]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)
        self.events.append("unsubscribe_updates")



def test_client_surface_and_enable_calls_flow() -> None:
    async def _case() -> None:
        raw = _Raw()
        client = Client(raw=raw, enable_calls=True)

        assert hasattr(client, "calls")
        assert hasattr(client, "updates")
        assert hasattr(client, "peers")

        await client.connect()
        assert raw.is_connected is True

        await client.close()
        assert raw.is_connected is False

        assert "connect" in raw.events
        assert "get_me" in raw.events
        assert "start_updates" in raw.events
        assert "subscribe_updates" in raw.events
        assert "unsubscribe_updates" in raw.events
        assert raw.events[-1] == "close"

    asyncio.run(_case())
