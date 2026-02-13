from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from secrets import randbits

from telecraft.client.mtproto import ClientInit, MtprotoClient
from telecraft.tl.generated.functions import MessagesSendMessage
from telecraft.tl.generated.types import InputPeerSelf


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

    text = args.text.strip() if args.text else ""
    if not text:
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = f"telecalls connected test ({stamp})"

    client = MtprotoClient(
        network=args.network,
        dc_id=args.dc,
        framing=args.framing,
        session_path=args.session,
        init=ClientInit(api_id=api_id, api_hash=api_hash),
    )
    await client.connect(timeout=args.timeout)
    try:
        me = await client.get_me(timeout=args.timeout)
        updates = await client.invoke_api(
            MessagesSendMessage(
                flags=0,
                no_webpage=None,
                silent=None,
                background=None,
                clear_draft=None,
                noforwards=None,
                update_stickersets_order=None,
                invert_media=None,
                allow_paid_floodskip=None,
                peer=InputPeerSelf(),
                reply_to=None,
                message=text,
                random_id=randbits(63),
                reply_markup=None,
                entities=None,
                schedule_date=None,
                schedule_repeat_period=None,
                send_as=None,
                quick_reply_shortcut=None,
                effect=None,
                allow_paid_stars=None,
                suggested_post=None,
            ),
            timeout=args.timeout,
        )
        print(
            {
                "sent": True,
                "message": text,
                "me_id": getattr(me, "id", None),
                "result_type": type(updates).__name__,
            }
        )
        return 0
    finally:
        await client.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Send a connectivity probe message to Saved Messages.")
    p.add_argument("--network", choices=["test", "prod"], default="prod")
    p.add_argument("--dc", type=int, default=4)
    p.add_argument("--framing", choices=["intermediate", "abridged"], default="intermediate")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--session", type=str, default=".sessions/prod_dc4.session.json")
    p.add_argument("--api-id", type=int, default=None)
    p.add_argument("--api-hash", type=str, default=None)
    p.add_argument("--text", type=str, default="")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
