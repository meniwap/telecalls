from __future__ import annotations

import argparse
import asyncio
import os

from telecraft.client.mtproto import ClientInit, MtprotoClient


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


async def _run(args: argparse.Namespace) -> int:
    api_id = args.api_id if args.api_id is not None else _env_int("TELEGRAM_API_ID")
    api_hash = args.api_hash if args.api_hash is not None else os.environ.get("TELEGRAM_API_HASH")
    if api_id is None or api_hash is None:
        print("Need TELEGRAM_API_ID/TELEGRAM_API_HASH (or --api-id/--api-hash).")
        return 2

    client = MtprotoClient(
        network=args.network,
        dc_id=args.dc,
        framing=args.framing,
        session_path=args.session,
        init=ClientInit(api_id=api_id, api_hash=api_hash),
    )

    await client.connect(timeout=args.timeout)
    try:
        await client.start_updates(timeout=args.timeout)
        print("Listening for updates (Ctrl+C to stop)...")
        while True:
            u = await client.recv_update()
            print(repr(u))
    finally:
        await client.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Smoke-test updates receiver (requires logged-in session)."
    )
    p.add_argument("--network", choices=["test", "prod"], default="prod")
    p.add_argument("--dc", type=int, default=2)
    p.add_argument("--framing", choices=["intermediate", "abridged"], default="intermediate")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--session", type=str, default=".sessions/prod_dc2.session.json")
    p.add_argument("--api-id", type=int, default=None)
    p.add_argument("--api-hash", type=str, default=None)
    args = p.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
