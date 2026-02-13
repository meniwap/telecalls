from __future__ import annotations

import math
import random
from dataclasses import dataclass


class PqFactorizationError(Exception):
    pass


def _is_probable_prime(n: int) -> bool:
    if n < 2:
        return False
    small_primes = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
    for p in small_primes:
        if n == p:
            return True
        if n % p == 0:
            return False
    # Miller-Rabin (deterministic for 64-bit using known bases)
    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1

    def check(a: int) -> bool:
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            return True
        for _ in range(s - 1):
            x = (x * x) % n
            if x == n - 1:
                return True
        return False

    for a in [2, 325, 9375, 28178, 450775, 9780504, 1795265022]:
        if a % n == 0:
            continue
        if not check(a):
            return False
    return True


def _pollard_rho(n: int) -> int:
    if n % 2 == 0:
        return 2
    if n % 3 == 0:
        return 3

    while True:
        c = random.randrange(1, n - 1)
        x = random.randrange(0, n - 1)
        y = x
        d = 1

        def f(v: int) -> int:
            return (pow(v, 2, n) + c) % n

        while d == 1:
            x = f(x)
            y = f(f(y))
            d = math.gcd(abs(x - y), n)
        if d != n:
            return d


def factorize_pq(pq: int) -> tuple[int, int]:
    """
    Factorize pq into (p, q) with p < q.

    Telegram's auth usually returns pq where p and q fit in 32-bit.
    """

    if pq <= 1:
        raise PqFactorizationError("pq must be > 1")
    if _is_probable_prime(pq):
        raise PqFactorizationError("pq is prime (expected composite)")

    # quick trial division for small factors
    for p in [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]:
        if pq % p == 0:
            q = pq // p
            return (p, q) if p < q else (q, p)

    n = pq
    factor = _pollard_rho(n)
    p = factor
    q = n // factor
    if p * q != pq:
        raise PqFactorizationError("factorization failed")
    if not _is_probable_prime(p):
        # try to fully factor (rare)
        p = _pollard_rho(p)
    if not _is_probable_prime(q):
        q = _pollard_rho(q)
    if p * q != pq:
        raise PqFactorizationError("factorization refinement failed")
    return (p, q) if p < q else (q, p)


@dataclass(frozen=True, slots=True)
class Pq:
    pq: int
    p: int
    q: int
