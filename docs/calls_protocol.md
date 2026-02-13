# Telecalls Call Protocol Baseline

This document locks the signaling/FSM baseline used by `telecalls` before full media support.

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
- No audio backend in this phase.

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
- `DISCONNECTING` timeout -> forced cleanup and `FAILED_TIMEOUT`.

## Update Routing

- `updatePhoneCall` carries all call-state transitions.
- `updatePhoneCallSignalingData` is routed to the target `CallSession`.
- Unknown call IDs are kept in a dead-letter buffer; receiver must not crash.

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

## Native Signaling Bridge

The native ABI is defined in `/Users/meniwap/satla/telecalls/native/include/telecalls/engine.h`.

Python never depends on native runtime for signaling correctness:

- native disabled or unavailable -> no-op/mock bridge.
- native available -> engine receives signaling blobs and reports stats.

## Security Constraints

- Auth key material is process-memory only.
- Zeroization is best-effort on call cleanup.
- Never log raw key material or encrypted payload bytes.
