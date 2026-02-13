from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from telecraft.client import CallState, Client, ClientInit


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _run(args: argparse.Namespace) -> int:
    api_id = args.api_id if args.api_id is not None else _env_int("TELEGRAM_API_ID")
    api_hash = args.api_hash if args.api_hash is not None else os.environ.get("TELEGRAM_API_HASH")
    if api_id is None or api_hash is None:
        print("Need TELEGRAM_API_ID/TELEGRAM_API_HASH (or --api-id/--api-hash).")
        return 2

    client = Client(
        network=args.network,
        dc_id=args.dc,
        framing=args.framing,
        session_path=args.session,
        init=ClientInit(api_id=api_id, api_hash=api_hash),
        enable_calls=True,
        calls_config={
            "native_bridge_enabled": args.native,
            "native_test_mode": True,
        },
    )

    await client.connect(timeout=args.timeout)
    try:
        successes = 0
        failures = 0

        for attempt in range(1, args.runs + 1):
            call = await client.calls.call(args.peer, video=args.video, timeout=args.timeout)
            terminal_event = asyncio.Event()
            result: dict[str, Any] = {"states": [call.state.value]}

            def _on_state(state: CallState) -> None:
                result["states"].append(state.value)
                print({"attempt": attempt, "state": state.value, "call_id": call.call_id})
                if state in {CallState.IN_CALL, CallState.ENDED, CallState.FAILED}:
                    terminal_event.set()

            def _on_error(exc: Exception) -> None:
                result["error"] = repr(exc)
                print({"attempt": attempt, "error": repr(exc), "call_id": call.call_id})
                terminal_event.set()

            call.on_state_change(_on_state)
            call.on_error(_on_error)

            await asyncio.wait_for(terminal_event.wait(), timeout=args.timeout)
            if call.state == CallState.IN_CALL:
                successes += 1
                await asyncio.sleep(args.hold_seconds)
                await call.hangup()
                print(
                    {
                        "attempt": attempt,
                        "result": "in_call_then_hangup",
                        "call_id": call.call_id,
                    }
                )
                continue

            failures += 1
            print(
                {
                    "attempt": attempt,
                    "result": "failed_before_in_call",
                    "call_id": call.call_id,
                    "state": call.state.value,
                    "reason": call.end_reason,
                }
            )
            if args.fail_fast:
                break

        print({"runs": args.runs, "successes": successes, "failures": failures})
        return 0 if failures == 0 else 1
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for call signaling+handshake.")
    parser.add_argument("--peer", required=True, help="Target peer (@username or user:<id>)")
    parser.add_argument("--network", choices=["test", "prod"], default="prod")
    parser.add_argument("--dc", type=int, default=2)
    parser.add_argument("--framing", choices=["intermediate", "abridged"], default="intermediate")
    parser.add_argument("--session", type=str, default=".sessions/prod_dc2.session.json")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--hold-seconds", type=float, default=1.0)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--video", action="store_true")
    parser.add_argument("--native", action="store_true", help="Enable native bridge if available")
    parser.add_argument("--api-id", type=int, default=None)
    parser.add_argument("--api-hash", type=str, default=None)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
