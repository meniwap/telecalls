from __future__ import annotations

import secrets


def random_bytes(n: int) -> bytes:
    if n < 0:
        raise ValueError("n must be >= 0")
    return secrets.token_bytes(n)
