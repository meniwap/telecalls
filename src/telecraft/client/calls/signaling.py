from __future__ import annotations

from secrets import randbits
from typing import Any

from telecraft.client.entities import EntityCacheError
from telecraft.client.mtproto import MtprotoClient
from telecraft.client.peers import PeerRef
from telecraft.tl.generated.functions import (
    MessagesGetDhConfig,
    PhoneAcceptCall,
    PhoneConfirmCall,
    PhoneDiscardCall,
    PhoneGetCallConfig,
    PhoneReceivedCall,
    PhoneRequestCall,
    PhoneSendSignalingData,
)
from telecraft.tl.generated.types import (
    InputUser,
    PhoneCallDiscardReasonBusy,
    PhoneCallDiscardReasonHangup,
)

from .types import CallProtocolSettings, PhoneCallRef, build_input_phone_call, default_protocol


class CallSignalingAdapter:
    def __init__(self, raw: MtprotoClient) -> None:
        self._raw = raw

    async def get_dh_config(
        self,
        *,
        version: int = 0,
        random_length: int = 0,
        timeout: float = 20.0,
    ) -> Any:
        return await self._raw.invoke_api(
            MessagesGetDhConfig(
                version=int(version),
                random_length=int(random_length),
            ),
            timeout=timeout,
        )

    async def get_call_config(self, *, timeout: float = 20.0) -> Any:
        return await self._raw.invoke_api(PhoneGetCallConfig(), timeout=timeout)

    async def request_call(
        self,
        peer: PeerRef,
        *,
        g_a_hash: bytes = b"",
        video: bool = False,
        protocol: CallProtocolSettings | None = None,
        timeout: float = 20.0,
    ) -> Any:
        user = await self._resolve_input_user(peer, timeout=timeout)
        res = await self._raw.invoke_api(
            PhoneRequestCall(
                flags=1 if video else 0,
                video=True if video else None,
                user_id=user,
                random_id=randbits(31),
                g_a_hash=bytes(g_a_hash),
                protocol=default_protocol(protocol),
            ),
            timeout=timeout,
        )
        self._raw._ingest_from_updates_result(res)
        return res

    async def accept_call(
        self,
        ref: PhoneCallRef,
        *,
        g_b: bytes = b"",
        protocol: CallProtocolSettings | None = None,
        timeout: float = 20.0,
    ) -> Any:
        res = await self._raw.invoke_api(
            PhoneAcceptCall(
                peer=build_input_phone_call(ref),
                g_b=bytes(g_b),
                protocol=default_protocol(protocol),
            ),
            timeout=timeout,
        )
        self._raw._ingest_from_updates_result(res)
        return res

    async def confirm_call(
        self,
        ref: PhoneCallRef,
        *,
        key_fingerprint: int,
        g_a: bytes = b"",
        protocol: CallProtocolSettings | None = None,
        timeout: float = 20.0,
    ) -> Any:
        res = await self._raw.invoke_api(
            PhoneConfirmCall(
                peer=build_input_phone_call(ref),
                g_a=bytes(g_a),
                key_fingerprint=int(key_fingerprint),
                protocol=default_protocol(protocol),
            ),
            timeout=timeout,
        )
        self._raw._ingest_from_updates_result(res)
        return res

    async def received_call(self, ref: PhoneCallRef, *, timeout: float = 20.0) -> Any:
        return await self._raw.invoke_api(
            PhoneReceivedCall(peer=build_input_phone_call(ref)),
            timeout=timeout,
        )

    async def send_signaling_data(
        self,
        ref: PhoneCallRef,
        data: bytes,
        *,
        timeout: float = 20.0,
    ) -> Any:
        return await self._raw.invoke_api(
            PhoneSendSignalingData(peer=build_input_phone_call(ref), data=bytes(data)),
            timeout=timeout,
        )

    async def discard_call(
        self,
        ref: PhoneCallRef,
        *,
        reason: Any | None = None,
        duration: int = 0,
        connection_id: int = 0,
        video: bool = False,
        timeout: float = 20.0,
    ) -> Any:
        payload_reason = reason if reason is not None else PhoneCallDiscardReasonHangup()
        res = await self._raw.invoke_api(
            PhoneDiscardCall(
                flags=1 if video else 0,
                video=True if video else None,
                peer=build_input_phone_call(ref),
                duration=int(duration),
                reason=payload_reason,
                connection_id=int(connection_id),
            ),
            timeout=timeout,
        )
        self._raw._ingest_from_updates_result(res)
        return res

    async def reject_call(self, ref: PhoneCallRef, *, timeout: float = 20.0) -> Any:
        return await self.discard_call(
            ref,
            reason=PhoneCallDiscardReasonBusy(),
            timeout=timeout,
        )

    async def hangup_call(self, ref: PhoneCallRef, *, timeout: float = 20.0) -> Any:
        return await self.discard_call(
            ref,
            reason=PhoneCallDiscardReasonHangup(),
            timeout=timeout,
        )

    async def _resolve_input_user(self, peer: PeerRef, *, timeout: float) -> InputUser:
        resolved = await self._raw.resolve_peer(peer, timeout=timeout)
        if resolved.peer_type != "user":
            raise ValueError("calls.request_call expects a user peer")

        user_id = int(resolved.peer_id)
        try:
            return self._raw.entities.input_user(user_id)
        except EntityCacheError:
            await self._raw.prime_entities(limit=200, timeout=timeout)
            return self._raw.entities.input_user(user_id)
