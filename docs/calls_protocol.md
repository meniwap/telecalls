# Telecalls Call Protocol Baseline

This document locks the signaling/FSM/media baseline used by `telecalls`.

## Scope

- 1:1 user calls only.
- MTProto methods:
  - `phone.getCallConfig`
  - `phone.requestCall`
  - `phone.acceptCall`
  - `phone.confirmCall`
  - `phone.receivedCall`
  - `phone.sendSignalingData`
  - `phone.discardCall`
- Native transport/interoperability core is enabled through `native/` + `cffi`.
- Audio backend can be enabled with `calls_config.audio_enabled=True`.

## Interop Profile (locked)

- `interop_profile = "tgvoip_v9"` (default).
- `connectionMaxLayer` target: `92` (short-format compatible path).
- `phoneCallProtocol` is built from local `calls_config`:
  - `udp_p2p=False` (default)
  - `udp_reflector=True` (default)
  - `min_layer=65`
  - `max_layer=92`
  - `library_versions=["11.0.0","10.0.0","9.0.0"]` (single advertised version selected per attempt)
  - `strict_media_ready=True` (default)
  - `disable_soft_media_ready=True` (default)
- Protocol constants:
  - `PROTOCOL_VERSION = 9`
  - `MIN_PROTOCOL_VERSION = 3`
- Packet classes implemented in MVP parser:
  - `PKT_INIT`
  - `PKT_INIT_ACK`
  - `PKT_STREAM_STATE`
  - `PKT_STREAM_DATA`
  - `PKT_PING`
  - `PKT_PONG`
  - `PKT_NOP`
- Extras parsed without crash for unknown values, with first-class handling for:
  - `EXTRA_TYPE_STREAM_FLAGS`
  - `EXTRA_TYPE_NETWORK_CHANGED`

## FSM

States:

- `IDLE`
- `RINGING_IN`
- `OUTGOING_INIT`
- `CONNECTING`
- `IN_CALL`
- `DISCONNECTING`
- `ENDED`
- `FAILED`

Timeout policy:

- `RINGING_IN` timeout -> `FAILED` with `FAILED_TIMEOUT` and `phoneCallDiscardReasonMissed`.
- `CONNECTING` timeout -> `FAILED` with `FAILED_TIMEOUT` and best-effort `discardCall`.
- `media_ready` timeout (8s after `server_ready`) -> `FAILED` with `FAILED_TIMEOUT`.
- `DISCONNECTING` timeout -> forced cleanup and `FAILED_TIMEOUT`.

## Update Routing

- `updatePhoneCall` carries all call-state transitions.
- `updatePhoneCallSignalingData` is routed to the target `CallSession`.
- Unknown call IDs are kept in a dead-letter buffer; receiver must not crash.
- Duplicate updates and duplicate signaling blobs are dropped idempotently.
- Outbound signaling from native bridge is not digest-deduped, so required retransmits are preserved.

## Runtime Call Config

- `phone.getCallConfig` is fetched at startup and periodically refreshed.
- Parsed runtime fields:
  - protocol (`udp_p2p`, `udp_reflector`, `min_layer`, `max_layer`, `library_versions`)
  - connect timeout (`call_connect_timeout_ms` or aliases)
  - packet timeout (`packet_timeout_ms` or aliases)
- Local policy overrides:
  - relay-first by default
  - p2p disabled by default unless `calls_config.allow_p2p=True`

## Handshake Order

### Outgoing

1. Caller generates DH context: `g_a`, `g_a_hash = sha256(g_a)`.
2. Caller sends `phone.requestCall(..., g_a_hash)`.
3. Receiver responds with `phone.acceptCall(..., g_b)`.
4. Caller receives `phoneCallAccepted(g_b)`, derives shared key, computes `key_fingerprint`.
5. Caller sends `phone.confirmCall(..., g_a, key_fingerprint)`.
6. Both sides receive `phoneCall(g_a_or_b, key_fingerprint, connections)` and verify fingerprint.

### Incoming

1. Receiver gets `phoneCallRequested(g_a_hash)` and creates incoming DH context.
2. Receiver acknowledges with `phone.receivedCall`.
3. Receiver accepts with `phone.acceptCall(..., g_b)`.
4. Receiver verifies final `g_a_or_b` and `key_fingerprint` from `phoneCall`.

## Reason Mapping

- `phoneCallDiscardReasonBusy` -> `BUSY`
- `phoneCallDiscardReasonMissed` -> `MISSED`
- `phoneCallDiscardReasonHangup` -> `REMOTE_HANGUP`
- `phoneCallDiscardReasonDisconnect` -> `REMOTE_HANGUP`
- Local API hangup -> `LOCAL_HANGUP`
- Protocol/parse mismatch -> `FAILED_PROTOCOL`
- On early `FAILED_PROTOCOL` (<=3s in outgoing call), local fallback arms once to the next
  configured `library_versions` index for the next attempt.

## Native Signaling Bridge

The native ABI is defined in `/Users/meniwap/satla/telecalls/native/include/telecalls/engine.h`.

Python never depends on native runtime for signaling correctness:

- native disabled or unavailable -> no-op/mock bridge.
- native available -> engine receives signaling blobs, emits protocol states, and reports stats.

Engine responsibilities:

- MTProto2 short-format packet crypto (`KDF2 + AES-IGE + SHA256 msg_key`).
- Init handshake over signaling blobs (`INIT/INIT_ACK`) with protocol version checks.
- Seq/ack/recent mask bookkeeping.
- Endpoint selection policy (relay-first).
- Stats (`rtt`, `loss`, `bitrate`, `jitter`, `packets_sent`, `packets_recv`, `send_loss`, `recv_loss`, `endpoint_id`).
- Opus encode/decode path (when `libopus` is available).
- Local audio frame queue (`push_audio_frame`/`pull_audio_frame`).

Interop note (legacy backend):

- Signaling/control blobs stay on MTProto (`phone.sendSignalingData`).
- `STREAM_DATA` is routed to the reflector UDP path.
- Inbound reflector UDP packets are decrypted (CTR then short/IGE fallback), parsed with
  `tc_proto_decode_short`, and dispatched locally; malformed packets increment UDP debug
  counters and are ignored (never crash policy).
- Debug counters expose both planes (`signaling_out_frames`, `udp_out_frames`, `udp_in_frames`,
  `udp_tx_bytes`, `udp_rx_bytes`) so signaling-only sessions are visible immediately.
- UDP diagnostics also expose recv-path failure points (`udp_recv_attempts`,
  `udp_recv_timeouts`, `udp_recv_source_mismatch`, `udp_proto_decode_failures`,
  `decrypt_failures_udp`) plus selected endpoint metadata to isolate inbound failures.
- Signaling diagnostics expose decrypt/parser split for pre-UDP failures
  (`signaling_decrypt_ctr_failures`, `signaling_decrypt_short_failures`,
  `signaling_proto_decode_failures`, `signaling_last_decrypt_mode`,
  `signaling_last_decrypt_direction`).

## Readiness Gating

`CallSession` enters `IN_CALL` only when both are true:

- `server_ready`: MTProto side reached `phoneCall` stage.
- `media_ready`: native engine reached `TC_ENGINE_STATE_ESTABLISHED`.

For official interop smoke runs, `udp_media_required_for_in_call=True` is recommended so
`IN_CALL` does not trigger on signaling-only / degraded paths.
The manager also records gate state on the session (`in_call_gate_satisfied`,
`in_call_gate_block_reason`) for postmortem logging.
It also records a native-handshake block reason (`native_handshake_block_reason`) to
distinguish `wait_init_ack` / signaling decrypt / signaling proto failures from `udp_gate`.
Signaling decrypt diagnostics also expose the last decrypt error code/stage and winner
candidate index, so repeated 44-byte blobs can be classified as crypto mismatch vs parser
failure before the UDP stage starts.

Before both are true, session remains in `CONNECTING`.

## Tone Policy

- Local ringback is stopped immediately on first `phoneCallAccepted` and again on `phoneCall`.
- Incoming `accept()` also stops local tone immediately before the signaling round-trip completes.
- No local loopback tone is generated while waiting for remote media.
- Playback path is remote stream only (or silence when no frame is available).

## E2E Visual Key

- Once key material is verified, the session stores:
  - `e2e_key_fingerprint_hex`
  - `e2e_emojis` (4 deterministic emojis derived from `auth_key`)
- This is for local interop diagnostics and parity checks against official clients.

## Security Constraints

- Auth key material is process-memory only.
- Zeroization is best-effort on call cleanup.
- Never log raw key material or encrypted payload bytes.
