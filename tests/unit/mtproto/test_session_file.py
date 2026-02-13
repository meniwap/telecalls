from __future__ import annotations

import pytest

from telecraft.mtproto.session.file import (
    MtprotoSession,
    SessionError,
    load_session_file,
    save_session_file,
)


def test_session_file_roundtrip(tmp_path) -> None:
    p = tmp_path / "session.json"
    sess = MtprotoSession(
        dc_id=2,
        host="149.154.167.40",
        port=443,
        framing="intermediate",
        auth_key=b"\x11" * 256,
        server_salt=b"\x22" * 8,
        session_id=b"\x33" * 8,
    )

    save_session_file(p, sess)
    loaded = load_session_file(p)

    assert loaded.dc_id == sess.dc_id
    assert loaded.host == sess.host
    assert loaded.port == sess.port
    assert loaded.framing == sess.framing
    assert loaded.auth_key == sess.auth_key
    assert loaded.server_salt == sess.server_salt
    assert loaded.session_id == sess.session_id


def test_session_file_rejects_invalid_salt() -> None:
    with pytest.raises(SessionError):
        MtprotoSession(
            dc_id=2,
            host="x",
            port=443,
            framing="intermediate",
            auth_key=b"\x11" * 256,
            server_salt=b"\x22" * 7,
        ).validate()
