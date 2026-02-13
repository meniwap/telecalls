from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

from telecraft.mtproto.core.msg_id import MsgIdGenerator
from telecraft.mtproto.core.unencrypted import (
    UnencryptedMessage,
    UnencryptedMessageError,
    unpack_unencrypted,
)
from telecraft.mtproto.crypto.aes_ige import AesIge
from telecraft.mtproto.crypto.hashes import sha1
from telecraft.mtproto.crypto.random import random_bytes
from telecraft.mtproto.crypto.rsa import RsaPublicKey
from telecraft.tl.codec import dumps, loads
from telecraft.tl.generated.functions import ReqDhParams, ReqPqMulti, SetClientDhParams
from telecraft.tl.generated.types import (
    ClientDhInnerData,
    DhGenFail,
    DhGenOk,
    DhGenRetry,
    PQInnerData,
    ResPq,
    ServerDhInnerData,
    ServerDhParamsFail,
    ServerDhParamsOk,
)

from .dh import make_dh_result
from .kdf import new_nonce_hash, server_salt, tmp_aes_key_iv
from .pq import factorize_pq

logger = logging.getLogger(__name__)

_UNENCRYPTED_ENVELOPE_MIN_LEN = 8 + 8 + 4  # auth_key_id + msg_id + length


class AuthHandshakeError(Exception):
    pass


class PacketTransport(Protocol):
    async def send(self, payload: bytes) -> None: ...
    async def recv(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class HandshakeState:
    nonce: bytes  # int128
    server_nonce: bytes  # int128
    new_nonce: bytes  # int256
    pq: bytes
    p: bytes
    q: bytes
    public_key_fingerprint: int
    inner_data: PQInnerData


def _pq_bytes_to_int(pq: bytes) -> int:
    return int.from_bytes(pq, "big", signed=False)


def _int_to_bytes_be(n: int) -> bytes:
    ln = (n.bit_length() + 7) // 8 or 1
    return n.to_bytes(ln, "big", signed=False)


async def send_req_pq_multi(transport: PacketTransport, msg_id_gen: MsgIdGenerator) -> ResPq:
    """
    First step of MTProto auth key exchange:
    send req_pq_multi and receive resPQ (unencrypted).
    """

    nonce = random_bytes(16)
    req = ReqPqMulti(nonce=nonce)
    logger.debug("Sending req_pq_multi...")
    obj = await _send_unencrypted_request(transport, msg_id_gen, req)
    logger.debug(f"Received response: {type(obj)}")
    if hasattr(obj, "msg_id"):
        # Not all TL objects have msg_id field, but UnencryptedMessage wrapper does.
        # Wait, obj is the *body* (ResPq). ResPq doesn't have msg_id field.
        # The msg_id was in the UnencryptedMessage envelope which we unwrapped inside _send.
        pass

    if not isinstance(obj, ResPq):
        raise AuthHandshakeError(f"Unexpected response: {type(obj)}")
    return obj


def build_pq_inner_data(res_pq: ResPq) -> HandshakeState:
    """
    Compute p/q from pq and prepare PQInnerData.

    Note: this does not perform RSA encryption yet.
    """

    nonce = res_pq.nonce
    server_nonce = res_pq.server_nonce
    pq_bytes = res_pq.pq

    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != 16:
        raise AuthHandshakeError("resPQ.nonce is not int128 bytes")
    if not isinstance(server_nonce, (bytes, bytearray)) or len(server_nonce) != 16:
        raise AuthHandshakeError("resPQ.server_nonce is not int128 bytes")
    if not isinstance(pq_bytes, (bytes, bytearray)):
        raise AuthHandshakeError("resPQ.pq is not bytes")
    pq_int = _pq_bytes_to_int(bytes(pq_bytes))
    logger.debug(f"Factorizing pq: {pq_int} (hex: {pq_int:x})")
    p_int, q_int = factorize_pq(pq_int)
    logger.debug(f"Factorized: p={p_int}, q={q_int}")
    p_bytes = _int_to_bytes_be(p_int)
    q_bytes = _int_to_bytes_be(q_int)

    new_nonce = random_bytes(32)

    fingerprints_obj: Any = res_pq.server_public_key_fingerprints
    if not isinstance(fingerprints_obj, list) or not fingerprints_obj:
        raise AuthHandshakeError("No server public key fingerprints provided")
    if not all(isinstance(x, int) for x in fingerprints_obj):
        raise AuthHandshakeError("server_public_key_fingerprints is not a list[int]")
    fingerprints = cast(list[int], fingerprints_obj)
    fingerprint = fingerprints[0]

    # We keep inner data as TL object so later we can dumps() it and RSA encrypt.
    inner = PQInnerData(
        pq=bytes(pq_bytes),
        p=p_bytes,
        q=q_bytes,
        nonce=bytes(nonce),
        server_nonce=bytes(server_nonce),
        new_nonce=new_nonce,
    )

    return HandshakeState(
        nonce=bytes(nonce),
        server_nonce=bytes(server_nonce),
        new_nonce=new_nonce,
        pq=bytes(pq_bytes),
        p=p_bytes,
        q=q_bytes,
        public_key_fingerprint=fingerprint,
        inner_data=inner,
    )


def rsa_encrypt_inner_data(inner: PQInnerData, key: RsaPublicKey) -> bytes:
    """
    RSA encrypt the p_q_inner_data (dumps(inner)) for req_DH_params.encrypted_data.
    """

    if key.fingerprint == 0:
        raise AuthHandshakeError("Invalid RSA key fingerprint")
    data = dumps(inner)
    return key.encrypt_raw(data)


def decrypt_server_dh_inner(server_dh: ServerDhParamsOk, *, new_nonce: bytes) -> ServerDhInnerData:
    """
    Decrypt server_DH_inner_data from server_DH_params_ok.encrypted_answer.
    """

    enc = server_dh.encrypted_answer
    if not isinstance(enc, (bytes, bytearray)):
        raise AuthHandshakeError("encrypted_answer is not bytes")
    server_nonce = server_dh.server_nonce
    if not isinstance(server_nonce, (bytes, bytearray)) or len(server_nonce) != 16:
        raise AuthHandshakeError("server_nonce is not int128 bytes")
    key, iv = tmp_aes_key_iv(new_nonce=new_nonce, server_nonce=bytes(server_nonce))
    aes = AesIge(key=key, iv=iv)
    dec = aes.decrypt(bytes(enc))
    if len(dec) < 20:
        raise AuthHandshakeError("Decrypted server DH inner data too short")
    # server_DH_inner_data is serialized as: sha1(inner_data) + inner_data + random_padding
    inner = loads(dec[20:])
    if not isinstance(inner, ServerDhInnerData):
        raise AuthHandshakeError(f"Unexpected decrypted object: {type(inner)}")
    return inner


@dataclass(frozen=True, slots=True)
class AuthKeyExchangeResult:
    nonce: bytes  # int128
    server_nonce: bytes  # int128
    new_nonce: bytes  # int256

    rsa_fingerprint: int  # TL long (signed)

    g: int
    dh_prime: bytes
    g_a: bytes
    g_b: bytes

    server_time: int
    server_salt: bytes  # 8 bytes

    auth_key: bytes
    auth_key_id: bytes  # 8 bytes (last 8 bytes of sha1(auth_key))


async def _send_unencrypted_request(
    transport: PacketTransport,
    msg_id_gen: MsgIdGenerator,
    req: Any,
    *,
    recv_timeout: float | None = None,
    max_frames: int = 128,
    max_ignored_small_frames: int = 64,
) -> Any:
    body = dumps(req)
    msg_id = msg_id_gen.next()
    logger.debug(f"Sending unencrypted request: msg_id={msg_id}, type={type(req)}")
    msg = UnencryptedMessage(msg_id=msg_id, body=body)
    await transport.send(msg.pack())
    ignored_small = 0

    # Some transports may interleave "quick ack" packets (4 bytes). Ignore anything
    # that can't possibly be an unencrypted MTProto envelope.
    for _ in range(max_frames):
        try:
            if recv_timeout is None:
                payload = await transport.recv()
            else:
                payload = await asyncio.wait_for(transport.recv(), timeout=recv_timeout)
        except TimeoutError as e:
            raise AuthHandshakeError(
                f"Timed out waiting for unencrypted response (timeout={recv_timeout}s)"
            ) from e

        if len(payload) < _UNENCRYPTED_ENVELOPE_MIN_LEN:
            ignored_small += 1
            if ignored_small > max_ignored_small_frames:
                raise AuthHandshakeError(
                    "Too many small frames while waiting for unencrypted response "
                    f"(ignored_small={ignored_small}, min_len={_UNENCRYPTED_ENVELOPE_MIN_LEN})"
                )
            preview = payload.hex()
            logger.debug(
                "Ignoring small frame while waiting for unencrypted response: %d bytes (hex=%s)",
                len(payload),
                preview,
            )
            continue

        try:
            resp = unpack_unencrypted(payload)
            logger.debug(
                "Received unencrypted message: msg_id=%d, length=%d",
                resp.msg_id,
                len(resp.body),
            )
        except UnencryptedMessageError as e:
            preview = payload[:32].hex()
            raise AuthHandshakeError(
                f"Failed to parse unencrypted response (len={len(payload)}, first32={preview}): {e}"
            ) from e

        # Keep outgoing msg_ids ahead of the server-provided msg_id.
        msg_id_gen.observe(int(resp.msg_id))

        return loads(resp.body)

    raise AuthHandshakeError(
        "No valid unencrypted response received "
        f"(max_frames={max_frames}, ignored_small={ignored_small})"
    )


def _require_bytes(value: object, *, name: str, length: int | None = None) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise AuthHandshakeError(f"{name} is not bytes")
    b = bytes(value)
    if length is not None and len(b) != length:
        raise AuthHandshakeError(f"{name} length mismatch: got {len(b)}, expected {length}")
    return b


async def exchange_auth_key(
    transport: PacketTransport,
    *,
    rsa_keys: list[RsaPublicKey],
    msg_id_gen: MsgIdGenerator | None = None,
) -> AuthKeyExchangeResult:
    """
    Perform the unencrypted MTProto auth key exchange:
      req_pq_multi -> req_DH_params -> set_client_DH_params

    Returns the negotiated auth_key + derived values, or raises AuthHandshakeError.
    """

    if msg_id_gen is None:
        msg_id_gen = MsgIdGenerator()

    res_pq = await send_req_pq_multi(transport, msg_id_gen)
    st = build_pq_inner_data(res_pq)

    fps_obj: Any = res_pq.server_public_key_fingerprints
    if not isinstance(fps_obj, list) or not all(isinstance(x, int) for x in fps_obj):
        raise AuthHandshakeError("server_public_key_fingerprints is not a list[int]")
    fps = cast(list[int], fps_obj)

    logger.debug(f"Server returned fingerprints: {fps}")

    key = next((k for k in rsa_keys if k.fingerprint in fps), None)
    if key is None:
        have = [k.fingerprint for k in rsa_keys]
        logger.error(
            "FATAL: No matching key found! Server wants %s, we have %s",
            fps,
            have,
        )
        raise AuthHandshakeError(f"No matching RSA key for server fingerprints: {fps!r}")

    logger.debug(f"Selected RSA key fingerprint: {key.fingerprint}")

    encrypted_inner = rsa_encrypt_inner_data(st.inner_data, key)
    logger.debug(f"Encrypted inner data length: {len(encrypted_inner)}")

    req_dh = ReqDhParams(
        nonce=st.nonce,
        server_nonce=st.server_nonce,
        p=st.p,
        q=st.q,
        public_key_fingerprint=key.fingerprint,
        encrypted_data=encrypted_inner,
    )

    req_blob = dumps(req_dh)
    logger.debug(f"ReqDhParams serialized length: {len(req_blob)} bytes")
    logger.debug(f"ReqDhParams raw hex prefix: {req_blob[:64].hex()}")

    dh_params = await _send_unencrypted_request(transport, msg_id_gen, req_dh)
    logger.debug(f"Received dh_params response type: {type(dh_params)}")

    if isinstance(dh_params, ServerDhParamsFail):
        raise AuthHandshakeError("Server returned server_DH_params_fail")
    if not isinstance(dh_params, ServerDhParamsOk):
        raise AuthHandshakeError(f"Unexpected response to req_DH_params: {type(dh_params)}")

    server_inner = decrypt_server_dh_inner(dh_params, new_nonce=st.new_nonce)
    server_inner_nonce = _require_bytes(
        server_inner.nonce,
        name="server_DH_inner_data.nonce",
        length=16,
    )
    server_inner_server_nonce = _require_bytes(
        server_inner.server_nonce,
        name="server_DH_inner_data.server_nonce",
        length=16,
    )
    if server_inner_nonce != st.nonce or server_inner_server_nonce != st.server_nonce:
        raise AuthHandshakeError("server_DH_inner_data nonce mismatch")

    if not isinstance(server_inner.g, int):
        raise AuthHandshakeError("server_DH_inner_data.g is not int")
    if not isinstance(server_inner.dh_prime, (bytes, bytearray)):
        raise AuthHandshakeError("server_DH_inner_data.dh_prime is not bytes")
    if not isinstance(server_inner.g_a, (bytes, bytearray)):
        raise AuthHandshakeError("server_DH_inner_data.g_a is not bytes")
    if not isinstance(server_inner.server_time, int):
        raise AuthHandshakeError("server_DH_inner_data.server_time is not int")

    dh_res = make_dh_result(
        g=int(server_inner.g),
        dh_prime=bytes(server_inner.dh_prime),
        g_a=bytes(server_inner.g_a),
    )

    client_inner = ClientDhInnerData(
        nonce=st.nonce,
        server_nonce=st.server_nonce,
        retry_id=0,
        g_b=dh_res.g_b,
    )
    client_data = dumps(client_inner)
    plain = sha1(client_data) + client_data
    plain += random_bytes((-len(plain)) % 16)

    tmp_key, tmp_iv = tmp_aes_key_iv(new_nonce=st.new_nonce, server_nonce=st.server_nonce)
    enc = AesIge(key=tmp_key, iv=tmp_iv).encrypt(plain)

    set_dh = SetClientDhParams(nonce=st.nonce, server_nonce=st.server_nonce, encrypted_data=enc)
    ans = await _send_unencrypted_request(transport, msg_id_gen, set_dh)

    if isinstance(ans, DhGenOk):
        expected = new_nonce_hash(new_nonce=st.new_nonce, auth_key=dh_res.auth_key, number=1)
        if (
            _require_bytes(
                ans.new_nonce_hash1,
                name="dh_gen_ok.new_nonce_hash1",
                length=16,
            )
            != expected
        ):
            raise AuthHandshakeError("dh_gen_ok new_nonce_hash1 mismatch")
    elif isinstance(ans, DhGenRetry):
        expected = new_nonce_hash(new_nonce=st.new_nonce, auth_key=dh_res.auth_key, number=2)
        if (
            _require_bytes(
                ans.new_nonce_hash2,
                name="dh_gen_retry.new_nonce_hash2",
                length=16,
            )
            != expected
        ):
            raise AuthHandshakeError("dh_gen_retry new_nonce_hash2 mismatch")
        raise AuthHandshakeError("Server requested dh_gen_retry (not supported in smoke flow yet)")
    elif isinstance(ans, DhGenFail):
        expected = new_nonce_hash(new_nonce=st.new_nonce, auth_key=dh_res.auth_key, number=3)
        if (
            _require_bytes(
                ans.new_nonce_hash3,
                name="dh_gen_fail.new_nonce_hash3",
                length=16,
            )
            != expected
        ):
            raise AuthHandshakeError("dh_gen_fail new_nonce_hash3 mismatch")
        raise AuthHandshakeError("Server returned dh_gen_fail")
    else:
        raise AuthHandshakeError(f"Unexpected response to set_client_DH_params: {type(ans)}")

    salt = server_salt(new_nonce=st.new_nonce, server_nonce=st.server_nonce)

    return AuthKeyExchangeResult(
        nonce=st.nonce,
        server_nonce=st.server_nonce,
        new_nonce=st.new_nonce,
        rsa_fingerprint=key.fingerprint,
        g=int(server_inner.g),
        dh_prime=bytes(server_inner.dh_prime),
        g_a=bytes(server_inner.g_a),
        g_b=dh_res.g_b,
        server_time=int(server_inner.server_time),
        server_salt=salt,
        auth_key=dh_res.auth_key,
        auth_key_id=dh_res.auth_key_id,
    )
