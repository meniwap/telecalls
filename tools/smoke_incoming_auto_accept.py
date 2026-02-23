from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from telecraft.client import CallState, Client, ClientInit


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _json_safe(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except Exception:
            return bytes(value).hex()
    return value


def _emoji_list(value: object) -> list[object] | None:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    return None


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
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
            "native_bridge_enabled": True,
            "native_test_mode": False,
            "native_backend": "legacy",
            "allow_p2p": False,
            "protocol_min_layer": 65,
            "protocol_max_layer": 92,
            "library_versions": ("11.0.0", "10.0.0", "9.0.0"),
            "strict_media_ready": True,
            "disable_soft_media_ready": True,
            "native_decrypt_fallback_soft_ready": False,
            "degraded_in_call_mode": False,
            "udp_media_required_for_in_call": True,
            "connect_timeout": 45.0,
            "media_ready_timeout": 8.0,
            "audio_enabled": False,
            "network_type": "wifi",
            "signaling_dump_path": args.signaling_dump,
        },
    )

    await client.connect(timeout=args.timeout)
    done = asyncio.Event()
    final_call: list[object] = []
    poller_task: asyncio.Task[None] | None = None

    async def _stats_loop(call: object) -> None:
        while not done.is_set():
            state = getattr(call, "state", None)
            if state in {CallState.ENDED, CallState.FAILED}:
                return
            await asyncio.sleep(1.0)
            print(
                json.dumps(
                    {
                        "event": "stats",
                        "call_id": getattr(call, "call_id", None),
                        "state": getattr(state, "value", state),
                        "native_state": getattr(call, "native_state", None),
                        "in_call_gate_satisfied": getattr(call, "in_call_gate_satisfied", None),
                        "in_call_gate_block_reason": getattr(
                            call, "in_call_gate_block_reason", None
                        ),
                        "native_handshake_block_reason": getattr(
                            call, "native_handshake_block_reason", None
                        ),
                        "ring_tone_stopped_at": getattr(call, "ring_tone_stopped_at", None),
                        "stats": getattr(call, "stats")(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    async def _on_incoming(call: object) -> None:
        nonlocal poller_task
        final_call[:] = [call]
        print(
            json.dumps(
                {
                    "event": "incoming",
                    "call_id": getattr(call, "call_id", None),
                    "state": getattr(getattr(call, "state", None), "value", None),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        def _on_state(state: CallState) -> None:
            print(
                json.dumps(
                    {
                        "event": "state",
                        "call_id": getattr(call, "call_id", None),
                        "state": state.value,
                        "native_state": getattr(call, "native_state", None),
                        "in_call_gate_satisfied": getattr(call, "in_call_gate_satisfied", None),
                        "in_call_gate_block_reason": getattr(
                            call, "in_call_gate_block_reason", None
                        ),
                        "native_handshake_block_reason": getattr(
                            call, "native_handshake_block_reason", None
                        ),
                        "e2e_emojis": _emoji_list(getattr(call, "e2e_emojis", None)),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if state in {CallState.ENDED, CallState.FAILED}:
                done.set()

        def _on_error(exc: Exception) -> None:
            print(
                json.dumps(
                    {
                        "event": "error",
                        "call_id": getattr(call, "call_id", None),
                        "error": repr(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            done.set()

        getattr(call, "on_state_change")(_on_state)
        getattr(call, "on_error")(_on_error)
        poller_task = asyncio.create_task(_stats_loop(call))
        print(
            json.dumps(
                {"event": "incoming_accept_start", "call_id": getattr(call, "call_id", None)}
            ),
            flush=True,
        )
        try:
            await getattr(call, "accept")()
            print(
                json.dumps(
                    {
                        "event": "incoming_accept_done",
                        "call_id": getattr(call, "call_id", None),
                        "state": getattr(getattr(call, "state", None), "value", None),
                    }
                ),
                flush=True,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "incoming_accept_error",
                        "call_id": getattr(call, "call_id", None),
                        "error": repr(exc),
                    }
                ),
                flush=True,
            )
            done.set()

    client.calls.on_incoming(_on_incoming)
    await client.calls.start()
    me = await client.get_me(timeout=min(args.timeout, 20.0))
    print(
        json.dumps(
            {
                "event": "ready",
                "session": args.session,
                "me": {
                    "id": _json_safe(getattr(me, "id", None)),
                    "username": _json_safe(getattr(me, "username", None)),
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    try:
        await asyncio.wait_for(done.wait(), timeout=args.listen_timeout)
    except asyncio.TimeoutError:
        print(json.dumps({"event": "timeout_waiting_call"}), flush=True)
        return 1
    finally:
        if poller_task is not None:
            poller_task.cancel()
            try:
                await poller_task
            except BaseException:
                pass

        if final_call:
            call = final_call[0]
            final_stats_snapshot = getattr(call, "final_stats_snapshot", None)
            native_handshake_block_reason = getattr(call, "native_handshake_block_reason", None)
            classification = (
                "blocked_on_signaling_decrypt"
                if native_handshake_block_reason == "signaling_decrypt"
                else (
                    "blocked_on_signaling_proto"
                    if native_handshake_block_reason == "signaling_proto"
                    else (
                        "blocked_on_udp"
                        if native_handshake_block_reason == "udp_gate"
                        else (
                            "ok_media_interop"
                            if getattr(getattr(call, "state", None), "value", None) == "IN_CALL"
                            else "unknown"
                        )
                    )
                )
            )
            print(
                json.dumps(
                    {
                        "event": "final",
                        "classification": classification,
                        "call_id": getattr(call, "call_id", None),
                        "state": getattr(getattr(call, "state", None), "value", None),
                        "reason": (
                            getattr(getattr(call, "end_reason", None), "value", None)
                            if getattr(call, "end_reason", None) is not None
                            else None
                        ),
                        "disconnect_reason_raw": getattr(call, "disconnect_reason_raw", None),
                        "native_state": getattr(call, "native_state", None),
                        "in_call_gate_satisfied": getattr(call, "in_call_gate_satisfied", None),
                        "in_call_gate_block_reason": getattr(
                            call, "in_call_gate_block_reason", None
                        ),
                        "native_handshake_block_reason": native_handshake_block_reason,
                        "ring_tone_stopped_at": getattr(call, "ring_tone_stopped_at", None),
                        "last_signaling_blob_len": getattr(call, "last_signaling_blob_len", None),
                        "repeated_signaling_blob_count": getattr(
                            call, "repeated_signaling_blob_count", None
                        ),
                        "final_stats_snapshot": final_stats_snapshot,
                        "signaling_decrypt_ctr_failures": (
                            final_stats_snapshot.get("signaling_decrypt_ctr_failures")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_decrypt_short_failures": (
                            final_stats_snapshot.get("signaling_decrypt_short_failures")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_decrypt_ctr_header_invalid": (
                            final_stats_snapshot.get("signaling_decrypt_ctr_header_invalid")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_decrypt_candidate_successes": (
                            final_stats_snapshot.get("signaling_decrypt_candidate_successes")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_last_decrypt_mode": (
                            final_stats_snapshot.get("signaling_last_decrypt_mode")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_last_decrypt_direction": (
                            final_stats_snapshot.get("signaling_last_decrypt_direction")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_decrypt_last_error_code": (
                            final_stats_snapshot.get("signaling_decrypt_last_error_code")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_decrypt_last_error_stage": (
                            final_stats_snapshot.get("signaling_decrypt_last_error_stage")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_ctr_last_error_code": (
                            final_stats_snapshot.get("signaling_ctr_last_error_code")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_short_last_error_code": (
                            final_stats_snapshot.get("signaling_short_last_error_code")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_ctr_last_variant": (
                            final_stats_snapshot.get("signaling_ctr_last_variant")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_ctr_last_hash_mode": (
                            final_stats_snapshot.get("signaling_ctr_last_hash_mode")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_best_failure_mode": (
                            final_stats_snapshot.get("signaling_best_failure_mode")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_best_failure_code": (
                            final_stats_snapshot.get("signaling_best_failure_code")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_proto_last_error_code": (
                            final_stats_snapshot.get("signaling_proto_last_error_code")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "signaling_candidate_winner_index": (
                            final_stats_snapshot.get("signaling_candidate_winner_index")
                            if isinstance(final_stats_snapshot, dict)
                            else None
                        ),
                        "e2e_emojis": _emoji_list(getattr(call, "e2e_emojis", None)),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        await client.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Incoming call auto-accept smoke (DC4 default).")
    p.add_argument("--network", choices=["test", "prod"], default="prod")
    p.add_argument("--dc", type=int, default=4)
    p.add_argument("--framing", choices=["intermediate", "abridged"], default="intermediate")
    p.add_argument("--session", type=str, default=".sessions/prod_dc4.session.json")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--listen-timeout", type=float, default=180.0)
    p.add_argument("--api-id", type=int, default=None)
    p.add_argument("--api-hash", type=str, default=None)
    p.add_argument("--signaling-dump", type=str, default=".sessions/signaling.dump.log")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    try:
        return asyncio.run(_run(p.parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
