from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

from telecraft.client import CallState, Client, ClientInit


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_versions(raw: str) -> list[str]:
    items = [item.strip() for item in str(raw).split(",")]
    versions = [item for item in items if item]
    return versions if versions else ["9.0.0"]


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

    effective_audio_backend = args.audio_backend if args.audio else "null"
    library_versions = _parse_versions(args.library_versions)
    native_audio_managed = str(args.native_backend).strip().lower() == "tgcalls"
    audio_enabled = bool(args.audio and not native_audio_managed)
    if native_audio_managed and args.audio:
        print(
            {
                "event": "audio_policy",
                "message": "python audio disabled for native tgcalls backend",
                "native_backend": args.native_backend,
            }
        )
    if native_audio_managed:
        effective_audio_backend = "null"

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
            "allow_p2p": False,
            "audio_enabled": audio_enabled,
            "audio_backend": effective_audio_backend,
            "interop_profile": "tgvoip_v9",
            "network_type": args.network_type,
            "protocol_min_layer": 65,
            "protocol_max_layer": 92,
            "library_versions": library_versions,
            "native_backend": args.native_backend,
            "connect_timeout": 45.0,
            "media_ready_timeout": 8.0,
            "strict_media_ready": True,
            "disable_soft_media_ready": True,
            "native_decrypt_fallback_soft_ready": False,
            "degraded_in_call_mode": False,
            "native_backend_oracle_compare": False,
            "udp_media_required_for_in_call": True,
            "signaling_dump_path": args.signaling_dump,
        },
    )

    await client.connect(timeout=args.timeout)
    try:
        call = await client.calls.call(args.peer, video=False, timeout=args.timeout)
        terminal = asyncio.Event()
        history: list[str] = [call.state.value]

        def _on_state(state: CallState) -> None:
            history.append(state.value)
            print({"event": "state", "state": state.value, "call_id": call.call_id})
            if state in {CallState.IN_CALL, CallState.ENDED, CallState.FAILED}:
                terminal.set()

        def _on_error(exc: Exception) -> None:
            print({"event": "error", "error": repr(exc), "call_id": call.call_id})
            terminal.set()

        call.on_state_change(_on_state)
        call.on_error(_on_error)

        await asyncio.wait_for(terminal.wait(), timeout=args.timeout)
        if call.state != CallState.IN_CALL:
            print(
                {
                    "result": "not_in_call",
                    "state": call.state.value,
                    "reason": call.end_reason.value if call.end_reason else None,
                    "history": history,
                    "e2e_key_fingerprint_hex": call.e2e_key_fingerprint_hex,
                    "e2e_emojis": list(call.e2e_emojis) if call.e2e_emojis else None,
                    "disconnect_reason_raw": call.disconnect_reason_raw,
                    "native_state": call.native_state,
                    "had_degraded_media_ready": getattr(call, "had_degraded_media_ready", None),
                    "in_call_gate_satisfied": getattr(call, "in_call_gate_satisfied", None),
                    "in_call_gate_block_reason": getattr(call, "in_call_gate_block_reason", None),
                    "ring_tone_stopped_at": getattr(call, "ring_tone_stopped_at", None),
                    "native_handshake_block_reason": getattr(
                        call, "native_handshake_block_reason", None
                    ),
                    "selected_relay_endpoint_id": getattr(call, "selected_relay_endpoint_id", None),
                    "selected_relay_endpoint_kind": getattr(
                        call, "selected_relay_endpoint_kind", None
                    ),
                    "final_stats_snapshot": getattr(call, "final_stats_snapshot", None),
                }
            )
            return 1

        start = time.monotonic()
        while (time.monotonic() - start) < args.hold_seconds:
            await asyncio.sleep(1.0)
            print(
                {
                    "event": "stats",
                    "stats": call.stats(),
                    "call_id": call.call_id,
                    "e2e_emojis": list(call.e2e_emojis) if call.e2e_emojis else None,
                    "audio_capture_frames": getattr(call, "audio_capture_frames", None),
                    "audio_push_ok": getattr(call, "audio_push_ok", None),
                    "audio_push_fail": getattr(call, "audio_push_fail", None),
                    "had_degraded_media_ready": getattr(call, "had_degraded_media_ready", None),
                }
            )

        stats = call.stats()
        audio_capture_frames = getattr(call, "audio_capture_frames", None)
        audio_push_ok = getattr(call, "audio_push_ok", None)
        audio_push_fail = getattr(call, "audio_push_fail", None)
        await call.hangup()
        final_stats = getattr(call, "final_stats_snapshot", None) or stats
        packets_sent = final_stats.get("packets_sent")
        packets_recv = final_stats.get("packets_recv")
        udp_tx_bytes = final_stats.get("udp_tx_bytes")
        udp_rx_bytes = final_stats.get("udp_rx_bytes")
        udp_in_frames = final_stats.get("udp_in_frames")
        udp_recv_attempts = final_stats.get("udp_recv_attempts")
        udp_recv_timeouts = final_stats.get("udp_recv_timeouts")
        udp_recv_source_mismatch = final_stats.get("udp_recv_source_mismatch")
        udp_proto_decode_failures = final_stats.get("udp_proto_decode_failures")
        udp_decrypt_failures = final_stats.get("decrypt_failures_udp")
        signaling_proto_decode_failures = final_stats.get("signaling_proto_decode_failures")
        signaling_decrypt_ctr_failures = final_stats.get("signaling_decrypt_ctr_failures")
        signaling_decrypt_short_failures = final_stats.get("signaling_decrypt_short_failures")
        signaling_decrypt_ctr_header_invalid = final_stats.get(
            "signaling_decrypt_ctr_header_invalid"
        )
        signaling_decrypt_candidate_successes = final_stats.get(
            "signaling_decrypt_candidate_successes"
        )
        signaling_last_decrypt_mode = final_stats.get("signaling_last_decrypt_mode")
        signaling_last_decrypt_direction = final_stats.get("signaling_last_decrypt_direction")
        signaling_duplicate_ciphertexts_seen = final_stats.get(
            "signaling_duplicate_ciphertexts_seen"
        )
        signaling_decrypt_last_error_code = final_stats.get("signaling_decrypt_last_error_code")
        signaling_decrypt_last_error_stage = final_stats.get(
            "signaling_decrypt_last_error_stage"
        )
        signaling_ctr_last_error_code = final_stats.get("signaling_ctr_last_error_code")
        signaling_short_last_error_code = final_stats.get("signaling_short_last_error_code")
        signaling_ctr_last_variant = final_stats.get("signaling_ctr_last_variant")
        signaling_ctr_last_hash_mode = final_stats.get("signaling_ctr_last_hash_mode")
        signaling_best_failure_mode = final_stats.get("signaling_best_failure_mode")
        signaling_best_failure_code = final_stats.get("signaling_best_failure_code")
        signaling_proto_last_error_code = final_stats.get("signaling_proto_last_error_code")
        signaling_candidate_winner_index = final_stats.get("signaling_candidate_winner_index")
        raw_media_packets_sent = final_stats.get("raw_media_packets_sent")
        raw_media_packets_recv = final_stats.get("raw_media_packets_recv")
        selected_endpoint_id = final_stats.get("selected_endpoint_id")
        selected_endpoint_kind = final_stats.get("selected_endpoint_kind")
        has_udp_tx = udp_tx_bytes not in (None, 0, 0.0)
        has_udp_rx = (udp_in_frames not in (None, 0, 0.0)) or (
            udp_rx_bytes not in (None, 0, 0.0)
        )
        has_media_tx = raw_media_packets_sent not in (None, 0, 0.0)
        result = (
            "ok_media_interop"
            if (has_udp_tx and has_udp_rx and has_media_tx)
            else "ok_signaling_only"
        )
        print(
            {
                "result": result,
                "stats": stats,
                "final_stats": final_stats,
                "history": history,
                "audio_capture_frames": audio_capture_frames,
                "audio_push_ok": audio_push_ok,
                "audio_push_fail": audio_push_fail,
                "had_degraded_media_ready": getattr(call, "had_degraded_media_ready", None),
                "in_call_gate_satisfied": getattr(call, "in_call_gate_satisfied", None),
                "in_call_gate_block_reason": getattr(call, "in_call_gate_block_reason", None),
                "ring_tone_stopped_at": getattr(call, "ring_tone_stopped_at", None),
                "native_handshake_block_reason": getattr(
                    call, "native_handshake_block_reason", None
                ),
                "native_backend": final_stats.get("native_backend"),
                "udp_tx_bytes": udp_tx_bytes,
                "udp_rx_bytes": udp_rx_bytes,
                "udp_in_frames": udp_in_frames,
                "udp_recv_attempts": udp_recv_attempts,
                "udp_recv_timeouts": udp_recv_timeouts,
                "udp_recv_source_mismatch": udp_recv_source_mismatch,
                "udp_proto_decode_failures": udp_proto_decode_failures,
                "udp_decrypt_failures": udp_decrypt_failures,
                "signaling_proto_decode_failures": signaling_proto_decode_failures,
                "signaling_decrypt_ctr_failures": signaling_decrypt_ctr_failures,
                "signaling_decrypt_short_failures": signaling_decrypt_short_failures,
                "signaling_decrypt_ctr_header_invalid": signaling_decrypt_ctr_header_invalid,
                "signaling_decrypt_candidate_successes": (
                    signaling_decrypt_candidate_successes
                ),
                "signaling_last_decrypt_mode": signaling_last_decrypt_mode,
                "signaling_last_decrypt_direction": signaling_last_decrypt_direction,
                "signaling_duplicate_ciphertexts_seen": signaling_duplicate_ciphertexts_seen,
                "signaling_decrypt_last_error_code": signaling_decrypt_last_error_code,
                "signaling_decrypt_last_error_stage": signaling_decrypt_last_error_stage,
                "signaling_ctr_last_error_code": signaling_ctr_last_error_code,
                "signaling_short_last_error_code": signaling_short_last_error_code,
                "signaling_ctr_last_variant": signaling_ctr_last_variant,
                "signaling_ctr_last_hash_mode": signaling_ctr_last_hash_mode,
                "signaling_best_failure_mode": signaling_best_failure_mode,
                "signaling_best_failure_code": signaling_best_failure_code,
                "signaling_proto_last_error_code": signaling_proto_last_error_code,
                "signaling_candidate_winner_index": signaling_candidate_winner_index,
                "raw_media_packets_sent": raw_media_packets_sent,
                "raw_media_packets_recv": raw_media_packets_recv,
                "selected_endpoint_id": selected_endpoint_id,
                "selected_endpoint_kind": selected_endpoint_kind,
                "selected_relay_endpoint_id": getattr(call, "selected_relay_endpoint_id", None),
                "selected_relay_endpoint_kind": getattr(
                    call, "selected_relay_endpoint_kind", None
                ),
                "e2e_emojis": list(call.e2e_emojis) if call.e2e_emojis else None,
            }
        )

        if packets_sent in (None, 0.0) and packets_recv in (None, 0.0):
            return 1
        return 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Voice smoke test with native signaling/media path."
    )
    parser.add_argument("--peer", required=True, help="Target peer (@username or user:<id>)")
    parser.add_argument("--network", choices=["test", "prod"], default="prod")
    parser.add_argument("--dc", type=int, default=4)
    parser.add_argument("--framing", choices=["intermediate", "abridged"], default="intermediate")
    parser.add_argument("--session", type=str, default=".sessions/prod_dc4.session.json")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--hold-seconds", type=float, default=60.0)
    parser.add_argument("--audio", action="store_true", help="Enable local audio backend")
    parser.add_argument("--audio-backend", type=str, default="portaudio")
    parser.add_argument(
        "--network-type",
        choices=["unknown", "wifi", "ethernet", "cellular"],
        default="wifi",
    )
    parser.add_argument("--api-id", type=int, default=None)
    parser.add_argument("--api-hash", type=str, default=None)
    parser.add_argument(
        "--native-backend",
        choices=["legacy", "tgcalls", "auto"],
        default=os.environ.get("TELECALLS_NATIVE_BACKEND", "legacy"),
    )
    parser.add_argument(
        "--library-versions",
        type=str,
        default=os.environ.get("TELECALLS_LIBRARY_VERSIONS", "9.0.0"),
        help="Comma-separated phoneCallProtocol library_versions (example: 11.0.0,9.0.0)",
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="DEBUG")
    parser.add_argument("--signaling-dump", type=str, default=".sessions/signaling.dump.log")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
