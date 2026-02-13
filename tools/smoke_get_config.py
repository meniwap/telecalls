from __future__ import annotations

import argparse
import asyncio
import logging
import os

from telecraft.client.mtproto import ClientInit, MtprotoClient
from telecraft.schema.pinned_layer import LAYER

TEST_DCS: dict[int, tuple[str, int]] = {
    1: ("149.154.175.10", 443),
    2: ("149.154.167.40", 443),
    3: ("149.154.175.117", 443),
}


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

    api_id = args.api_id if args.api_id is not None else _env_int("TELEGRAM_API_ID")
    if api_id is None:
        print(
            "Missing --api-id (or env TELEGRAM_API_ID). "
            "This is required for initConnection/invokeWithLayer."
        )
        return 2

    if args.host is not None:
        host, port = args.host, args.port
    else:
        host, port = TEST_DCS[args.dc]

    client = MtprotoClient(
        dc_id=args.dc,
        host=host if args.host is not None else None,
        port=port,
        framing=args.framing,
        session_path=args.session,
        init=ClientInit(
            api_id=api_id,
            device_model=args.device_model,
            system_version=args.system_version,
            app_version=args.app_version,
            system_lang_code=args.system_lang_code,
            lang_pack=args.lang_pack,
            lang_code=args.lang_code,
        ),
    )
    await client.connect(timeout=args.timeout)
    try:
        cfg = client.config
        print({"layer": LAYER, "api_id": api_id, "config": repr(cfg)})
        return 0
    finally:
        await client.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Smoke-test invokeWithLayer(initConnection(help.getConfig()))."
    )
    p.add_argument(
        "--dc",
        type=int,
        choices=sorted(TEST_DCS.keys()),
        default=2,
        help="Test DC number",
    )
    p.add_argument("--host", type=str, default=None, help="Override host (disables --dc mapping)")
    p.add_argument("--port", type=int, default=443, help="Port (when using --host)")
    p.add_argument(
        "--framing",
        choices=["intermediate", "abridged"],
        default="intermediate",
        help="Transport framing mode",
    )
    p.add_argument("--timeout", type=float, default=20.0, help="Timeout (seconds)")
    p.add_argument(
        "--api-id",
        type=int,
        default=None,
        help="Telegram API ID (or env TELEGRAM_API_ID)",
    )
    p.add_argument(
        "--session",
        type=str,
        default=None,
        help="Session JSON path (reuse auth_key/server_salt)",
    )

    # initConnection() fields (keep overridable for debugging).
    p.add_argument("--device-model", type=str, default="telecraft", help="device_model")
    p.add_argument("--system-version", type=str, default="telecraft", help="system_version")
    p.add_argument("--app-version", type=str, default="0.0", help="app_version")
    p.add_argument("--system-lang-code", type=str, default="en", help="system_lang_code")
    p.add_argument("--lang-pack", type=str, default="", help="lang_pack")
    p.add_argument("--lang-code", type=str, default="en", help="lang_code")

    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
