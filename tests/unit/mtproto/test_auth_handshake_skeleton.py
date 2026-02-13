from __future__ import annotations

from telecraft.mtproto.auth.handshake import build_pq_inner_data
from telecraft.tl.generated.types import ResPq


def test_build_pq_inner_data_factorizes() -> None:
    # pq = 17 * 19 = 323 => 0x0143
    pq_bytes = (17 * 19).to_bytes(2, "big")
    res = ResPq(
        nonce=b"\x01" * 16,
        server_nonce=b"\x02" * 16,
        pq=pq_bytes,
        server_public_key_fingerprints=[123456789],
    )
    st = build_pq_inner_data(res)
    assert int.from_bytes(st.p, "big") == 17
    assert int.from_bytes(st.q, "big") == 19
    assert st.pq == pq_bytes
    assert st.public_key_fingerprint == 123456789
    assert st.inner_data.p == st.p
    assert st.inner_data.q == st.q
