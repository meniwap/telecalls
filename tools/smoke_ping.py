from __future__ import annotations

import argparse
import asyncio
import logging

from telecraft.client.mtproto import MtprotoClient

TEST_DCS: dict[int, tuple[str, int]] = {
    1: ("149.154.175.10", 443),
    2: ("149.154.167.40", 443),
    3: ("149.154.175.117", 443),
}


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

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
    )
    await client.connect(timeout=args.timeout)
    try:
        pong = await client.ping(timeout=args.timeout)
        print({"pong": repr(pong)})
        return 0
    finally:
        await client.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Smoke-test encrypted MTProto ping (auth_key + MTProto v2)."
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
        "--session",
        type=str,
        default=None,
        help="Session JSON path (reuse auth_key/server_salt)",
    )
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
