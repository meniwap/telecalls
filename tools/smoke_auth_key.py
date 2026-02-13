from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
from pathlib import Path

from telecraft.mtproto.auth.handshake import exchange_auth_key
from telecraft.mtproto.auth.server_keys import DEFAULT_SERVER_KEYRING
from telecraft.mtproto.transport.abridged import AbridgedFraming
from telecraft.mtproto.transport.base import Endpoint
from telecraft.mtproto.transport.intermediate import IntermediateFraming
from telecraft.mtproto.transport.tcp import TcpTransport

TEST_DCS: dict[int, tuple[str, int]] = {
    1: ("149.154.175.10", 443),
    2: ("149.154.167.40", 443),
    3: ("149.154.175.117", 443),
}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.host is not None:
        host, port = args.host, args.port
    else:
        host, port = TEST_DCS[args.dc]

    framing = IntermediateFraming() if args.framing == "intermediate" else AbridgedFraming()
    transport = TcpTransport(endpoint=Endpoint(host=host, port=port), framing=framing)

    await transport.connect()
    try:
        rsa_keys = list(DEFAULT_SERVER_KEYRING.keys_by_fingerprint.values())
        try:
            res = await asyncio.wait_for(
                exchange_auth_key(transport, rsa_keys=rsa_keys),
                timeout=args.timeout,
            )
        except TimeoutError:
            print(f"Timed out after {args.timeout:.1f}s while exchanging auth_key")
            return 1
    finally:
        await transport.close()

    fp_u64 = res.rsa_fingerprint if res.rsa_fingerprint >= 0 else res.rsa_fingerprint + 2**64
    summary = {
        "endpoint": {"host": host, "port": port},
        "framing": args.framing,
        "rsa_fingerprint_signed": res.rsa_fingerprint,
        "rsa_fingerprint_unsigned_hex": hex(fp_u64),
        "auth_key_id_hex": res.auth_key_id.hex(),
        "server_salt_hex": res.server_salt.hex(),
        "server_time": res.server_time,
        "auth_key_b64": _b64(res.auth_key),
    }

    if args.out is not None:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote auth key info to {out_path}")
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke-test MTProto auth_key exchange (test DCs).")
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
    p.add_argument("--timeout", type=float, default=30.0, help="Overall timeout (seconds)")
    p.add_argument("--out", type=str, default=None, help="Write JSON output to this path")
    args = p.parse_args()

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
