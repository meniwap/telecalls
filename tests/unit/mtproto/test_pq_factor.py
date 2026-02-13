from __future__ import annotations

import pytest

from telecraft.mtproto.auth.pq import PqFactorizationError, factorize_pq


def test_factorize_small() -> None:
    p, q = factorize_pq(17 * 19)
    assert (p, q) == (17, 19)


def test_factorize_medium() -> None:
    p, q = factorize_pq(10007 * 10009)
    assert p * q == 10007 * 10009
    assert p < q


def test_reject_prime() -> None:
    with pytest.raises(PqFactorizationError):
        factorize_pq(101)
