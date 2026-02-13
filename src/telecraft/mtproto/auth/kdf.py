from __future__ import annotations

from telecraft.mtproto.crypto.hashes import sha1


class KdfError(Exception):
    pass


def tmp_aes_key_iv(*, new_nonce: bytes, server_nonce: bytes) -> tuple[bytes, bytes]:
    """
    Temporary AES key/iv used during auth key exchange (before auth_key exists).

    As per MTProto docs (SHA1-based):
    - tmp_aes_key = sha1(new_nonce + server_nonce) + sha1(server_nonce + new_nonce)[:12]
    - tmp_aes_iv  = sha1(server_nonce + new_nonce)[12:20]
                   + sha1(new_nonce + new_nonce)
                   + new_nonce[:4]
    """

    if len(new_nonce) != 32:
        raise KdfError("new_nonce must be 32 bytes (int256)")
    if len(server_nonce) != 16:
        raise KdfError("server_nonce must be 16 bytes (int128)")

    h1 = sha1(new_nonce + server_nonce)
    h2 = sha1(server_nonce + new_nonce)
    h3 = sha1(new_nonce + new_nonce)
    key = h1 + h2[:12]
    iv = h2[12:20] + h3 + new_nonce[:4]
    if len(key) != 32 or len(iv) != 32:
        raise KdfError("unexpected key/iv size")
    return key, iv


def server_salt(*, new_nonce: bytes, server_nonce: bytes) -> bytes:
    """
    server_salt = first 8 bytes of new_nonce XOR first 8 bytes of server_nonce.
    """

    if len(new_nonce) < 8 or len(server_nonce) < 8:
        raise KdfError("nonces too short")
    a = int.from_bytes(new_nonce[:8], "little", signed=False)
    b = int.from_bytes(server_nonce[:8], "little", signed=False)
    return (a ^ b).to_bytes(8, "little", signed=False)


def auth_key_aux_hash(auth_key: bytes) -> bytes:
    """
    auth_key_aux_hash = sha1(auth_key)[:8]
    """

    return sha1(auth_key)[:8]


def new_nonce_hash(*, new_nonce: bytes, auth_key: bytes, number: int) -> bytes:
    """
    new_nonce_hash{1,2,3} = sha1(new_nonce + bytes([number]) + auth_key_aux_hash)[4:20]
    (16 bytes, used as int128 in dh_gen_ok/retry/fail).
    """

    if number not in (1, 2, 3):
        raise KdfError("number must be 1, 2, or 3")
    if len(new_nonce) != 32:
        raise KdfError("new_nonce must be 32 bytes")
    aux = auth_key_aux_hash(auth_key)
    return sha1(new_nonce + bytes([number]) + aux)[4:20]
