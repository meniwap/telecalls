from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import cast

from telecraft.client.mtproto import ClientInit, MtprotoClient
from telecraft.mtproto.rpc.sender import RpcErrorException
from telecraft.tl.generated.types import (
    AuthAuthorization,
    AuthAuthorizationSignUpRequired,
    AuthSentCode,
)


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _env_str(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        return None
    return v


def _parse_migrate_dc(msg: str) -> int | None:
    # Typical messages: PHONE_MIGRATE_2, USER_MIGRATE_4, NETWORK_MIGRATE_3
    for prefix in ("PHONE_MIGRATE_", "USER_MIGRATE_", "NETWORK_MIGRATE_"):
        if prefix in msg:
            try:
                return int(msg.split(prefix, 1)[1])
            except ValueError:
                return None
    return None


def _default_session_path(*, network: str, dc: int) -> Path:
    return Path(".sessions") / f"{network}_dc{dc}.session.json"


async def _login_flow(
    *,
    network: str,
    dc: int,
    host: str | None,
    port: int,
    framing: str,
    timeout: float,
    session_path: Path,
    init: ClientInit,
    phone_number: str,
    phone_code: str | None,
    args_password: str | None,
    first_name: str | None,
    last_name: str | None,
) -> int:
    client = MtprotoClient(
        network=network,
        dc_id=dc,
        host=host,
        port=port,
        framing=framing,
        session_path=session_path,
        init=init,
    )

    await client.connect(timeout=timeout)
    try:
        sent: AuthSentCode = await client.send_code(phone_number, timeout=timeout)
        print({"sent_code": repr(sent)})

        phone_code_hash = cast(bytes, sent.phone_code_hash)
        if phone_code is None:
            phone_code = input("Enter the code you received: ").strip()

        try:
            auth = await client.sign_in(
                phone_number=phone_number,
                phone_code_hash=phone_code_hash,
                phone_code=phone_code,
                timeout=timeout,
            )
        except RpcErrorException as e:
            if e.message == "SESSION_PASSWORD_NEEDED":
                pw = os.environ.get("TELEGRAM_PASSWORD") if args_password is None else args_password
                if not pw:
                    pw = input("2FA password: ").strip()
                authz = await client.check_password(pw, timeout=timeout)
                print({"authorization": repr(authz)})
                return 0
            raise

        if isinstance(auth, AuthAuthorization):
            print({"authorization": repr(auth)})
            return 0

        if isinstance(auth, AuthAuthorizationSignUpRequired):
            print({"sign_up_required": True, "details": repr(auth)})
            if first_name is None:
                first_name = input("First name: ").strip()
            if last_name is None:
                last_name = input("Last name (optional): ").strip()
            created = await client.sign_up(
                phone_number=phone_number,
                phone_code_hash=phone_code_hash,
                first_name=first_name,
                last_name=last_name or "",
                timeout=timeout,
            )
            print({"authorization": repr(created)})
            return 0

        print({"unexpected": repr(auth)})
        return 1
    finally:
        await client.close()


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    api_id = args.api_id if args.api_id is not None else _env_int("TELEGRAM_API_ID")
    api_hash = args.api_hash if args.api_hash is not None else _env_str("TELEGRAM_API_HASH")
    if api_id is None or api_hash is None:
        print(
            "Missing API credentials. Provide --api-id/--api-hash "
            "or env TELEGRAM_API_ID/TELEGRAM_API_HASH."
        )
        return 2

    phone_number = (
        args.phone if args.phone is not None else input("Phone number (international): ").strip()
    )

    session_path = (
        Path(args.session)
        if args.session is not None
        else _default_session_path(network=args.network, dc=args.dc)
    )
    session_path.parent.mkdir(parents=True, exist_ok=True)

    init = ClientInit(
        api_id=api_id,
        api_hash=api_hash,
        device_model=args.device_model,
        system_version=args.system_version,
        app_version=args.app_version,
        system_lang_code=args.system_lang_code,
        lang_pack=args.lang_pack,
        lang_code=args.lang_code,
    )

    host = args.host
    port = args.port

    # Retry once on DC migrate errors.
    dc = args.dc
    for attempt in range(2):
        try:
            return await _login_flow(
                network=args.network,
                dc=dc,
                host=host,
                port=port,
                framing=args.framing,
                timeout=args.timeout,
                session_path=session_path,
                init=init,
                phone_number=phone_number,
                phone_code=args.code,
                args_password=args.password,
                first_name=args.first_name,
                last_name=args.last_name,
            )
        except RpcErrorException as e:
            migrate_dc = _parse_migrate_dc(e.message)
            if migrate_dc is None or attempt == 1:
                raise

            print({"migrate": {"from_dc": dc, "to_dc": migrate_dc, "reason": e.message}})
            dc = migrate_dc
            session_path = _default_session_path(network=args.network, dc=dc)
            session_path.parent.mkdir(parents=True, exist_ok=True)

    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke-test MTProto user login (sendCode/signIn).")
    p.add_argument("--network", choices=["test", "prod"], default="prod", help="Telegram network")
    p.add_argument("--dc", type=int, default=2, help="Initial DC id")
    p.add_argument("--host", type=str, default=None, help="Override host (disables DC mapping)")
    p.add_argument("--port", type=int, default=443, help="Port (when using --host)")
    p.add_argument("--framing", choices=["intermediate", "abridged"], default="intermediate")
    p.add_argument("--timeout", type=float, default=30.0)

    p.add_argument(
        "--api-id", type=int, default=None, help="Telegram API ID (or env TELEGRAM_API_ID)"
    )
    p.add_argument(
        "--api-hash",
        type=str,
        default=None,
        help="Telegram API hash (or env TELEGRAM_API_HASH)",
    )

    p.add_argument(
        "--session",
        type=str,
        default=None,
        help="Session JSON path (defaults under .sessions/)",
    )
    p.add_argument("--phone", type=str, default=None, help="Phone number in international format")
    p.add_argument("--code", type=str, default=None, help="Code you received (otherwise prompt)")
    p.add_argument(
        "--password",
        type=str,
        default=None,
        help="2FA password (or env TELEGRAM_PASSWORD; otherwise prompt)",
    )

    p.add_argument("--first-name", type=str, default=None, help="First name (if sign-up required)")
    p.add_argument("--last-name", type=str, default=None, help="Last name (if sign-up required)")

    p.add_argument(
        "--device-model", type=str, default="telecraft", help="initConnection.device_model"
    )
    p.add_argument(
        "--system-version", type=str, default="telecraft", help="initConnection.system_version"
    )
    p.add_argument("--app-version", type=str, default="0.0", help="initConnection.app_version")
    p.add_argument(
        "--system-lang-code", type=str, default="en", help="initConnection.system_lang_code"
    )
    p.add_argument("--lang-pack", type=str, default="", help="initConnection.lang_pack")
    p.add_argument("--lang-code", type=str, default="en", help="initConnection.lang_code")

    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
