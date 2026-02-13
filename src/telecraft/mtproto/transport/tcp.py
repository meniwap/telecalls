from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .base import Endpoint, Framing, TransportError


@dataclass(slots=True)
class TcpTransport:
    endpoint: Endpoint
    framing: Framing
    connect_timeout: float = 10.0

    _reader: asyncio.StreamReader | None = None
    _writer: asyncio.StreamWriter | None = None
    _rx_buf: bytearray = field(default_factory=bytearray)

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.endpoint.host, self.endpoint.port),
                timeout=self.connect_timeout,
            )
            self._reader, self._writer = reader, writer
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"Failed to connect to {self.endpoint}") from e

        # Some MTProto transport variants require a connection header.
        header = getattr(self.framing, "CONNECT_HEADER", b"")
        if header:
            if not isinstance(header, (bytes, bytearray)):
                raise TransportError("Framing CONNECT_HEADER must be bytes")
            writer.write(bytes(header))
            await writer.drain()

    async def close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass
        self._writer = None
        self._reader = None
        self._rx_buf.clear()

    async def send(self, payload: bytes) -> None:
        if self._writer is None:
            raise TransportError("Not connected.")
        framed = self.framing.encode(payload)
        self._writer.write(framed)
        await self._writer.drain()

    async def recv(self) -> bytes:
        if self._reader is None:
            raise TransportError("Not connected.")
        while True:
            payload = self.framing.decode_from_buffer(self._rx_buf)
            if payload is not None:
                return payload
            chunk = await self._reader.read(4096)
            if not chunk:
                raise TransportError("Connection closed.")
            self._rx_buf.extend(chunk)
