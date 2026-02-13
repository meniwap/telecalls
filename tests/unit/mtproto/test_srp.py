from __future__ import annotations

from telecraft.mtproto.auth.srp import make_input_check_password_srp
from telecraft.tl.generated.types import (
    AccountPassword,
    InputCheckPasswordSrp,
    PasswordKdfAlgoSha256Sha256Pbkdf2Hmacsha512iter100000Sha256ModPow,
    SecurePasswordKdfAlgoSha512,
)


def test_srp_builds_input_check_password() -> None:
    # Minimal-but-valid shapes for our SRP helper.
    p = b"\xff" * 64
    algo = PasswordKdfAlgoSha256Sha256Pbkdf2Hmacsha512iter100000Sha256ModPow(
        salt1=b"salt1",
        salt2=b"salt2",
        g=2,
        p=p,
    )
    pw_state = AccountPassword(
        flags=(1 << 2),  # has_password
        has_recovery=False,
        has_secure_values=False,
        has_password=True,
        current_algo=algo,
        srp_b=b"\x01" * 64,
        srp_id=123,
        hint=None,
        email_unconfirmed_pattern=None,
        new_algo=algo,
        new_secure_algo=SecurePasswordKdfAlgoSha512(salt=b"x"),
        secure_random=b"\x00" * 32,
        pending_reset_date=None,
        login_email_pattern=None,
    )

    fixed = b"\x02" * 256
    out = make_input_check_password_srp(
        password="pw",
        password_state=pw_state,
        random_bytes=lambda n: fixed[:n],
    )

    assert isinstance(out, InputCheckPasswordSrp)
    assert out.srp_id == 123
    assert isinstance(out.a, (bytes, bytearray))
    assert len(out.a) == 64
    assert isinstance(out.m1, (bytes, bytearray))
    assert len(out.m1) == 32


def test_srp_changes_with_password() -> None:
    p = b"\xff" * 64
    algo = PasswordKdfAlgoSha256Sha256Pbkdf2Hmacsha512iter100000Sha256ModPow(
        salt1=b"salt1",
        salt2=b"salt2",
        g=2,
        p=p,
    )
    pw_state = AccountPassword(
        flags=(1 << 2),
        has_recovery=False,
        has_secure_values=False,
        has_password=True,
        current_algo=algo,
        srp_b=b"\x01" * 64,
        srp_id=1,
        hint=None,
        email_unconfirmed_pattern=None,
        new_algo=algo,
        new_secure_algo=SecurePasswordKdfAlgoSha512(salt=b"x"),
        secure_random=b"\x00" * 32,
        pending_reset_date=None,
        login_email_pattern=None,
    )

    fixed = b"\x02" * 256
    a = make_input_check_password_srp(
        password="pw1", password_state=pw_state, random_bytes=lambda n: fixed[:n]
    )
    b = make_input_check_password_srp(
        password="pw2", password_state=pw_state, random_bytes=lambda n: fixed[:n]
    )
    assert a.m1 != b.m1
