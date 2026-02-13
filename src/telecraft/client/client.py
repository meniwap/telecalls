from __future__ import annotations

from pathlib import Path
from typing import Any

from telecraft.client.apis import CallsAPI, PeersAPI, UpdatesAPI
from telecraft.client.mtproto import ClientInit, MtprotoClient


class Client:
    """
    Telecalls high-level client facade.

    Exposes only call-focused namespaces:
    - calls
    - updates
    - peers
    """

    def __init__(
        self,
        *,
        network: str = "test",
        dc_id: int = 2,
        host: str | None = None,
        port: int = 443,
        framing: str = "intermediate",
        session_path: str | Path | None = None,
        init: ClientInit | None = None,
        raw: MtprotoClient | None = None,
        enable_calls: bool = False,
        calls_config: dict[str, Any] | None = None,
    ) -> None:
        _ = calls_config
        self.raw = (
            raw
            if raw is not None
            else MtprotoClient(
                network=network,
                dc_id=dc_id,
                host=host,
                port=port,
                framing=framing,
                session_path=session_path,
                init=init,
            )
        )

        self.peers = PeersAPI(self.raw)
        self.updates = UpdatesAPI(self.raw)
        self.calls = CallsAPI(self.raw, enabled=enable_calls)

    @property
    def is_connected(self) -> bool:
        return self.raw.is_connected

    async def connect(self, *, timeout: float = 30.0) -> None:
        await self.raw.connect(timeout=timeout)
        if self.calls.enabled:
            # Best-effort self identity to classify incoming/outgoing call updates.
            try:
                await self.raw.get_me(timeout=timeout)
            except Exception:
                pass
            await self.calls.start()

    async def close(self) -> None:
        await self.calls.stop()
        await self.raw.close()

    async def get_me(self, *, timeout: float = 20.0) -> Any:
        return await self.raw.get_me(timeout=timeout)

    async def send_code(self, phone_number: str, *, timeout: float = 20.0) -> Any:
        return await self.raw.send_code(phone_number, timeout=timeout)

    async def sign_in(
        self,
        *,
        phone_number: str,
        phone_code_hash: str | bytes,
        phone_code: str,
        timeout: float = 20.0,
    ) -> Any:
        return await self.raw.sign_in(
            phone_number=phone_number,
            phone_code_hash=phone_code_hash,
            phone_code=phone_code,
            timeout=timeout,
        )

    async def check_password(self, password: str, *, timeout: float = 20.0) -> Any:
        return await self.raw.check_password(password, timeout=timeout)

    async def __aenter__(self) -> Client:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = (exc_type, exc, tb)
        await self.close()
