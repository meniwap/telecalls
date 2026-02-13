from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass

from telecraft.mtproto.core.msg_id import MsgIdGenerator
from telecraft.mtproto.crypto.aes_ige import AesIge
from telecraft.mtproto.crypto.hashes import sha1, sha256


class MtprotoStateError(Exception):
    pass


def auth_key_id_u64(auth_key: bytes) -> int:
    """
    Telegram's auth_key_id/key_id: last 8 bytes of SHA1(auth_key), interpreted as uint64 LE.
    """

    h = sha1(auth_key)
    return int.from_bytes(h[-8:], "little", signed=False)


def _calc_key_iv_mtproto2(*, auth_key: bytes, msg_key: bytes, client: bool) -> tuple[bytes, bytes]:
    """
    MTProto 2.0 key derivation.

    See: https://core.telegram.org/mtproto/description#defining-aes-key-and-initialization-vector
    """

    if len(msg_key) != 16:
        raise MtprotoStateError("msg_key must be 16 bytes")
    x = 0 if client else 8
    sha256a = sha256(msg_key + auth_key[x : x + 36])
    sha256b = sha256(auth_key[x + 40 : x + 76] + msg_key)

    aes_key = sha256a[:8] + sha256b[8:24] + sha256a[24:32]
    aes_iv = sha256b[:8] + sha256a[8:24] + sha256b[24:32]
    return aes_key, aes_iv


@dataclass(slots=True)
class MtprotoState:
    """
    Minimal MTProto 2.0 state for encrypting/decrypting messages.
    """

    auth_key: bytes
    server_salt: bytes  # 8 bytes (little-endian)
    msg_id_gen: MsgIdGenerator
    session_id: bytes = b""
    _seq: int = 0

    def __post_init__(self) -> None:
        if len(self.server_salt) != 8:
            raise MtprotoStateError("server_salt must be 8 bytes")
        if not self.session_id:
            self.session_id = secrets.token_bytes(8)
        if len(self.session_id) != 8:
            raise MtprotoStateError("session_id must be 8 bytes")

    @property
    def auth_key_id(self) -> int:
        return auth_key_id_u64(self.auth_key)

    def next_seq_no(self, *, content_related: bool) -> int:
        if content_related:
            out = self._seq * 2 + 1
            self._seq += 1
            return out
        return self._seq * 2

    def encrypt_inner_message(self, inner: bytes, *, to_server: bool = True) -> bytes:
        """
        Encrypt an MTProto inner message (msg_id+seqno+len+body) into an encrypted packet:
          auth_key_id (8) + msg_key (16) + aes_ige(ciphertext...)
        """

        if len(inner) % 4 != 0:
            raise MtprotoStateError("inner message must be 4-byte aligned")

        # Prepend salt + session_id.
        data = self.server_salt + self.session_id + inner

        # MTProto 2.0 padding: length multiple of 16, and at least 12 bytes padding.
        pad_len = (-(len(data) + 12) % 16) + 12
        padding = secrets.token_bytes(pad_len)

        # MTProto 2.0 uses different auth_key slices depending on direction:
        # - client -> server: auth_key[88:120]
        # - server -> client: auth_key[96:128]
        auth_slice = self.auth_key[88 : 88 + 32] if to_server else self.auth_key[96 : 96 + 32]
        msg_key_large = sha256(auth_slice + data + padding)
        msg_key = msg_key_large[8:24]

        aes_key, aes_iv = _calc_key_iv_mtproto2(
            auth_key=self.auth_key,
            msg_key=msg_key,
            client=to_server,
        )
        ct = AesIge(key=aes_key, iv=aes_iv).encrypt(data + padding)

        return struct.pack("<Q", self.auth_key_id) + msg_key + ct

    def decrypt_packet(self, packet: bytes, *, from_server: bool = True) -> bytes:
        """
        Decrypt an incoming MTProto encrypted packet and return the inner message bytes
        (msg_id+seqno+len+body), after validating msg_key and session_id.
        """

        if len(packet) < 8 + 16:
            raise MtprotoStateError("encrypted packet too short")

        key_id = struct.unpack_from("<Q", packet, 0)[0]
        if key_id != self.auth_key_id:
            raise MtprotoStateError("auth_key_id mismatch in incoming packet")

        msg_key = packet[8:24]
        aes_key, aes_iv = _calc_key_iv_mtproto2(
            auth_key=self.auth_key,
            msg_key=msg_key,
            client=not from_server,
        )
        plain = AesIge(key=aes_key, iv=aes_iv).decrypt(packet[24:])

        # Validate msg_key (MTProto security guidelines).
        auth_slice = self.auth_key[96 : 96 + 32] if from_server else self.auth_key[88 : 88 + 32]
        expected = sha256(auth_slice + plain)[8:24]
        if expected != msg_key:
            raise MtprotoStateError("msg_key mismatch after decryption")

        if len(plain) < 16:
            raise MtprotoStateError("decrypted payload too short")

        salt = plain[:8]
        session_id = plain[8:16]
        if session_id != self.session_id:
            raise MtprotoStateError("session_id mismatch in incoming packet")

        # Salt can change (bad_server_salt), so we do not enforce it here.
        _ = salt

        return plain[16:]
