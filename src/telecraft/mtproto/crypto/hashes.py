from __future__ import annotations

import hashlib


def sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()  # noqa: S324 (required by MTProto)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
