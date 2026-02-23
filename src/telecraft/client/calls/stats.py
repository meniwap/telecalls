from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class CallStats:
    rtt_ms: float | None = None
    loss: float | None = None
    bitrate_kbps: float | None = None
    jitter_ms: float | None = None
    packets_sent: int | None = None
    packets_recv: int | None = None
    media_packets_sent: int | None = None
    media_packets_recv: int | None = None
    signaling_packets_sent: int | None = None
    signaling_packets_recv: int | None = None
    send_loss: float | None = None
    recv_loss: float | None = None
    endpoint_id: int | None = None
    udp_tx_bytes: int | None = None
    udp_rx_bytes: int | None = None
    raw_media_packets_sent: int | None = None
    raw_media_packets_recv: int | None = None
    raw_packets_sent: int | None = None
    raw_packets_recv: int | None = None
    signaling_out_frames: int | None = None
    udp_out_frames: int | None = None
    udp_in_frames: int | None = None
    udp_recv_attempts: int | None = None
    udp_recv_timeouts: int | None = None
    udp_recv_source_mismatch: int | None = None
    udp_proto_decode_failures: int | None = None
    udp_rx_peer_tag_mismatch: int | None = None
    udp_rx_short_packet_drops: int | None = None
    decrypt_failures_signaling: int | None = None
    decrypt_failures_udp: int | None = None
    signaling_proto_decode_failures: int | None = None
    signaling_decrypt_ctr_failures: int | None = None
    signaling_decrypt_short_failures: int | None = None
    signaling_decrypt_ctr_header_invalid: int | None = None
    signaling_decrypt_candidate_successes: int | None = None
    signaling_last_decrypt_mode: str | None = None
    signaling_last_decrypt_direction: str | None = None
    signaling_duplicate_ciphertexts_seen: int | None = None
    signaling_ctr_last_error_code: int | None = None
    signaling_short_last_error_code: int | None = None
    signaling_ctr_last_variant: str | None = None
    signaling_ctr_last_hash_mode: str | None = None
    signaling_best_failure_mode: str | None = None
    signaling_best_failure_code: int | None = None
    signaling_decrypt_last_error_code: int | None = None
    signaling_decrypt_last_error_stage: str | None = None
    signaling_proto_last_error_code: int | None = None
    signaling_candidate_winner_index: int | None = None
    selected_endpoint_id: int | None = None
    selected_endpoint_kind: str | None = None
    native_backend: str | None = None
    local_audio_push_ok: int | None = None
    local_audio_push_fail: int | None = None
    updated_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, float | int | str | None]:
        return {
            "rtt_ms": self.rtt_ms,
            "loss": self.loss,
            "bitrate_kbps": self.bitrate_kbps,
            "jitter_ms": self.jitter_ms,
            "packets_sent": float(self.packets_sent) if self.packets_sent is not None else None,
            "packets_recv": float(self.packets_recv) if self.packets_recv is not None else None,
            "media_packets_sent": (
                float(self.media_packets_sent)
                if self.media_packets_sent is not None
                else None
            ),
            "media_packets_recv": (
                float(self.media_packets_recv)
                if self.media_packets_recv is not None
                else None
            ),
            "signaling_packets_sent": (
                float(self.signaling_packets_sent)
                if self.signaling_packets_sent is not None
                else None
            ),
            "signaling_packets_recv": (
                float(self.signaling_packets_recv)
                if self.signaling_packets_recv is not None
                else None
            ),
            "send_loss": self.send_loss,
            "recv_loss": self.recv_loss,
            "endpoint_id": float(self.endpoint_id) if self.endpoint_id is not None else None,
            "udp_tx_bytes": self.udp_tx_bytes,
            "udp_rx_bytes": self.udp_rx_bytes,
            "raw_media_packets_sent": self.raw_media_packets_sent,
            "raw_media_packets_recv": self.raw_media_packets_recv,
            "raw_packets_sent": self.raw_packets_sent,
            "raw_packets_recv": self.raw_packets_recv,
            "signaling_out_frames": self.signaling_out_frames,
            "udp_out_frames": self.udp_out_frames,
            "udp_in_frames": self.udp_in_frames,
            "udp_recv_attempts": self.udp_recv_attempts,
            "udp_recv_timeouts": self.udp_recv_timeouts,
            "udp_recv_source_mismatch": self.udp_recv_source_mismatch,
            "udp_proto_decode_failures": self.udp_proto_decode_failures,
            "udp_rx_peer_tag_mismatch": self.udp_rx_peer_tag_mismatch,
            "udp_rx_short_packet_drops": self.udp_rx_short_packet_drops,
            "decrypt_failures_signaling": self.decrypt_failures_signaling,
            "decrypt_failures_udp": self.decrypt_failures_udp,
            "signaling_proto_decode_failures": self.signaling_proto_decode_failures,
            "signaling_decrypt_ctr_failures": self.signaling_decrypt_ctr_failures,
            "signaling_decrypt_short_failures": self.signaling_decrypt_short_failures,
            "signaling_decrypt_ctr_header_invalid": self.signaling_decrypt_ctr_header_invalid,
            "signaling_decrypt_candidate_successes": self.signaling_decrypt_candidate_successes,
            "signaling_last_decrypt_mode": self.signaling_last_decrypt_mode,
            "signaling_last_decrypt_direction": self.signaling_last_decrypt_direction,
            "signaling_duplicate_ciphertexts_seen": self.signaling_duplicate_ciphertexts_seen,
            "signaling_ctr_last_error_code": self.signaling_ctr_last_error_code,
            "signaling_short_last_error_code": self.signaling_short_last_error_code,
            "signaling_ctr_last_variant": self.signaling_ctr_last_variant,
            "signaling_ctr_last_hash_mode": self.signaling_ctr_last_hash_mode,
            "signaling_best_failure_mode": self.signaling_best_failure_mode,
            "signaling_best_failure_code": self.signaling_best_failure_code,
            "signaling_decrypt_last_error_code": self.signaling_decrypt_last_error_code,
            "signaling_decrypt_last_error_stage": self.signaling_decrypt_last_error_stage,
            "signaling_proto_last_error_code": self.signaling_proto_last_error_code,
            "signaling_candidate_winner_index": self.signaling_candidate_winner_index,
            "selected_endpoint_id": self.selected_endpoint_id,
            "selected_endpoint_kind": self.selected_endpoint_kind,
            "native_backend": self.native_backend,
            "local_audio_push_ok": self.local_audio_push_ok,
            "local_audio_push_fail": self.local_audio_push_fail,
        }
