#include "telecalls/engine.h"
#include "telecalls/engine_internal.h"

#include "telecalls/codec_opus.h"
#include "telecalls/crypto_mtproto2.h"
#include "telecalls/media_pipeline.h"
#include "telecalls/proto.h"
#include "telecalls/reflector_transport.h"

#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>

#define TC_MAX_SIGNALING_FRAMES 128
#define TC_MAX_SIGNALING_BYTES 16384
#define TC_MAX_ENDPOINTS 16
#define TC_MAX_KEY_BYTES 512
#define TC_MAX_IP_STR 64
#define TC_MAX_IPV6_STR 96
#define TC_MAX_PEER_TAG 32
#define TC_PCM_FRAME_SAMPLES 960
#define TC_PCM_QUEUE_FRAMES 64
#define TC_MAX_OUT_TRACK 256
#define TC_MAX_SIGNALING_DUP_TRACK 128U

#define TC_CODEC_OPUS_FOURCC 0x4F505553U
#define TC_ENDPOINT_FLAG_RELAY (1U << 0)
#define TC_ENDPOINT_FLAG_P2P (1U << 1)
#define TC_ENDPOINT_FLAG_TCP (1U << 2)
#define TC_ENDPOINT_FLAG_TURN (1U << 3)
#define TC_DEBUG_ENDPOINT_KIND_UNKNOWN 0U
#define TC_DEBUG_ENDPOINT_KIND_RELAY 1U
#define TC_DEBUG_ENDPOINT_KIND_WEBRTC 2U
#define TC_DEBUG_SIGNALING_DECRYPT_MODE_NONE 0U
#define TC_DEBUG_SIGNALING_DECRYPT_MODE_CTR 1U
#define TC_DEBUG_SIGNALING_DECRYPT_MODE_SHORT 2U
#define TC_DEBUG_SIGNALING_DECRYPT_DIR_NONE 0U
#define TC_DEBUG_SIGNALING_DECRYPT_DIR_LOCAL_ROLE 1U
#define TC_DEBUG_SIGNALING_DECRYPT_DIR_OPPOSITE_ROLE 2U
#define TC_DEBUG_SIGNALING_ERROR_STAGE_NONE 0U
#define TC_DEBUG_SIGNALING_ERROR_STAGE_CTR 1U
#define TC_DEBUG_SIGNALING_ERROR_STAGE_SHORT 2U
#define TC_DEBUG_SIGNALING_BEST_FAILURE_NONE 0U
#define TC_DEBUG_SIGNALING_BEST_FAILURE_CTR 1U
#define TC_DEBUG_SIGNALING_BEST_FAILURE_SHORT 2U
#define TC_DEBUG_SIGNALING_CTR_VARIANT_NONE 0U
#define TC_DEBUG_SIGNALING_CTR_VARIANT_KDF128 1U
#define TC_DEBUG_SIGNALING_CTR_VARIANT_KDF0 2U
#define TC_DEBUG_SIGNALING_CTR_HASH_NONE 0U
#define TC_DEBUG_SIGNALING_CTR_HASH_SEQ_PAYLOAD 1U
#define TC_DEBUG_SIGNALING_CTR_HASH_PAYLOAD_ONLY 2U

typedef struct tc_frame {
    size_t len;
    uint8_t bytes[TC_MAX_SIGNALING_BYTES];
} tc_frame;

typedef struct tc_endpoint_runtime {
    int64_t id;
    char ip[TC_MAX_IP_STR];
    char ipv6[TC_MAX_IPV6_STR];
    uint16_t port;
    uint8_t peer_tag[TC_MAX_PEER_TAG];
    size_t peer_tag_len;
    uint32_t flags;
    uint32_t priority;
} tc_endpoint_runtime;

typedef struct tc_out_track {
    uint32_t seq;
    uint64_t sent_ms;
    int used;
    int acked;
} tc_out_track_t;

typedef enum tc_packet_plane {
    TC_PACKET_PLANE_SIGNALING = 0,
    TC_PACKET_PLANE_UDP = 1
} tc_packet_plane_t;

struct tc_engine {
    tc_engine_params_t params;
    int state;

    pthread_mutex_t mu;

    tc_frame signaling_out[TC_MAX_SIGNALING_FRAMES];
    size_t signaling_head;
    size_t signaling_tail;
    size_t signaling_size;

    tc_endpoint_runtime endpoints[TC_MAX_ENDPOINTS];
    size_t endpoint_count;
    int active_endpoint_index;

    uint8_t key_material[TC_MAX_KEY_BYTES];
    size_t key_len;
    int64_t key_fingerprint;
    int keys_ready;
    int is_outgoing;

    int muted;
    int bitrate_hint_kbps;
    int network_type;
    uint32_t protocol_version;
    uint32_t min_protocol_version;
    int protocol_min_layer;
    int protocol_max_layer;
    int pending_network_changed;
    int pending_stream_flags;

    tc_opus_codec_t *codec;
    int16_t pcm_queue[TC_PCM_QUEUE_FRAMES][TC_PCM_FRAME_SAMPLES];
    int pcm_lengths[TC_PCM_QUEUE_FRAMES];
    size_t pcm_head;
    size_t pcm_tail;
    size_t pcm_size;

    tc_proto_rx_seq_state_t rx_seq;
    uint32_t local_seq;
    tc_out_track_t out_track[TC_MAX_OUT_TRACK];
    size_t out_track_next;

    int received_init;
    int received_init_ack;
    int init_sent;
    int init_ack_sent;
    int established;

    uint64_t last_init_tx_ms;
    uint64_t last_ping_tx_ms;
    uint64_t last_udp_ping_tx_ms;
    uint64_t last_rx_ms;
    uint64_t last_udp_rx_ms;
    uint64_t established_at_ms;
    uint64_t last_endpoint_fallback_ms;
    uint32_t endpoint_fallback_rounds;

    uint64_t out_acked_count;
    uint64_t out_lost_count;
    uint64_t in_lost_count;

    uint64_t bytes_sent_window;
    uint64_t bytes_recv_window;
    uint64_t window_started_ms;

    tc_stats_t stats;
    tc_debug_stats_t debug;
    tc_reflector_transport_t reflector;
    uint8_t last_signaling_cipher_head[TC_MAX_SIGNALING_DUP_TRACK];
    size_t last_signaling_cipher_head_len;
    size_t last_signaling_cipher_len;
};

static int tc_pump_udp_locked(tc_engine_t *engine);
static int tc_apply_endpoint_index_locked(tc_engine_t *engine, size_t idx, const char *reason);
static int tc_rotate_relay_endpoint_locked(tc_engine_t *engine, const char *reason);
static int tc_handle_packet_locked(
    tc_engine_t *engine,
    tc_packet_plane_t plane,
    const tc_proto_header_t *header,
    const uint8_t *payload,
    size_t payload_len
);
static void tc_update_bitrate_locked(tc_engine_t *engine, uint64_t now_ms);
static void tc_update_loss_stats_locked(tc_engine_t *engine);
static void tc_track_signaling_cipher_duplicate_locked(
    tc_engine_t *engine,
    const uint8_t *data,
    size_t len
);
static void tc_set_signaling_decrypt_error_locked(
    tc_engine_t *engine,
    uint32_t stage,
    int32_t code
);
static void tc_set_signaling_best_failure_locked(
    tc_engine_t *engine,
    uint32_t mode,
    int32_t code
);
static int tc_signaling_ctr_payload_sane(
    const uint8_t *plain,
    size_t plain_len,
    tc_crypto_debug_t *dbg
);

static uint64_t tc_now_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return ((uint64_t)tv.tv_sec * 1000ULL) + ((uint64_t)tv.tv_usec / 1000ULL);
}

static void tc_agent_debug_log(
    const char *run_id,
    const char *hypothesis_id,
    const char *location,
    const char *message,
    const char *data_json
) {
    static uint64_t seq = 0U;
    (void)mkdir("/Users/meniwap/satla/.cursor", 0777);
    FILE *handle = fopen("/Users/meniwap/satla/.cursor/debug.log", "a");
    uint64_t ts = 0U;
    if (handle == NULL) {
        return;
    }
    ts = tc_now_ms();
    seq += 1U;
    fprintf(
        handle,
        "{\"id\":\"c_%llu_%llu\",\"timestamp\":%llu,\"location\":\"%s\",\"message\":\"%s\","
        "\"runId\":\"%s\",\"hypothesisId\":\"%s\",\"data\":%s}\n",
        (unsigned long long)ts,
        (unsigned long long)seq,
        (unsigned long long)ts,
        location,
        message,
        run_id,
        hypothesis_id,
        data_json
    );
    fclose(handle);
}

static void tc_emit_state(tc_engine_t *engine, int state) {
    if (engine == NULL) {
        return;
    }
    engine->state = state;
    if (engine->params.on_state != NULL) {
        engine->params.on_state(engine->params.user_data, state);
    }
}

static void tc_emit_error(tc_engine_t *engine, int code, const char *message) {
    if (engine == NULL || engine->params.on_error == NULL) {
        return;
    }
    engine->params.on_error(engine->params.user_data, code, message);
}

static void tc_emit_stats(tc_engine_t *engine) {
    if (engine == NULL || engine->params.on_stats == NULL) {
        return;
    }
    engine->params.on_stats(engine->params.user_data, &engine->stats);
}

static uint32_t tc_read_u32_le(const uint8_t *in) {
    return ((uint32_t)in[0]) |
           ((uint32_t)in[1] << 8) |
           ((uint32_t)in[2] << 16) |
           ((uint32_t)in[3] << 24);
}

static void tc_write_u32_le(uint8_t *out, uint32_t value) {
    out[0] = (uint8_t)(value & 0xFFU);
    out[1] = (uint8_t)((value >> 8) & 0xFFU);
    out[2] = (uint8_t)((value >> 16) & 0xFFU);
    out[3] = (uint8_t)((value >> 24) & 0xFFU);
}

static void tc_write_u16_le(uint8_t *out, uint16_t value) {
    out[0] = (uint8_t)(value & 0xFFU);
    out[1] = (uint8_t)((value >> 8) & 0xFFU);
}

static void tc_track_signaling_cipher_duplicate_locked(
    tc_engine_t *engine,
    const uint8_t *data,
    size_t len
) {
    size_t copy_len = 0U;
    if (engine == NULL || data == NULL || len == 0U) {
        return;
    }
    copy_len = (len < TC_MAX_SIGNALING_DUP_TRACK) ? len : TC_MAX_SIGNALING_DUP_TRACK;
    if (engine->last_signaling_cipher_len == len &&
        engine->last_signaling_cipher_head_len == copy_len &&
        memcmp(engine->last_signaling_cipher_head, data, copy_len) == 0) {
        engine->debug.signaling_duplicate_ciphertexts_seen += 1U;
    }
    memcpy(engine->last_signaling_cipher_head, data, copy_len);
    engine->last_signaling_cipher_head_len = copy_len;
    engine->last_signaling_cipher_len = len;
}

static void tc_set_signaling_decrypt_error_locked(
    tc_engine_t *engine,
    uint32_t stage,
    int32_t code
) {
    if (engine == NULL) {
        return;
    }
    engine->debug.signaling_decrypt_last_error_stage = stage;
    engine->debug.signaling_decrypt_last_error_code = code;
}

static void tc_set_signaling_best_failure_locked(
    tc_engine_t *engine,
    uint32_t mode,
    int32_t code
) {
    if (engine == NULL) {
        return;
    }
    if (mode == TC_DEBUG_SIGNALING_BEST_FAILURE_CTR) {
        engine->debug.signaling_best_failure_mode = mode;
        engine->debug.signaling_best_failure_code = code;
        engine->debug.signaling_decrypt_last_error_stage = TC_DEBUG_SIGNALING_ERROR_STAGE_CTR;
        engine->debug.signaling_decrypt_last_error_code = code;
        return;
    }
    if (mode == TC_DEBUG_SIGNALING_BEST_FAILURE_SHORT &&
        engine->debug.signaling_best_failure_mode != TC_DEBUG_SIGNALING_BEST_FAILURE_CTR) {
        engine->debug.signaling_best_failure_mode = mode;
        engine->debug.signaling_best_failure_code = code;
        engine->debug.signaling_decrypt_last_error_stage = TC_DEBUG_SIGNALING_ERROR_STAGE_SHORT;
        engine->debug.signaling_decrypt_last_error_code = code;
        return;
    }
    if (mode == TC_DEBUG_SIGNALING_BEST_FAILURE_NONE &&
        engine->debug.signaling_best_failure_mode == TC_DEBUG_SIGNALING_BEST_FAILURE_NONE) {
        engine->debug.signaling_best_failure_code = code;
    }
}

static int tc_signaling_ctr_payload_sane(
    const uint8_t *plain,
    size_t plain_len,
    tc_crypto_debug_t *dbg
) {
    size_t i = 0U;
    int any_non_zero = 0;
    if (plain == NULL || plain_len < 8U || plain_len > TC_MAX_SIGNALING_BYTES) {
        if (dbg != NULL) {
            dbg->reason_code = TC_CRYPTO_ERR_HEADER_INVALID;
        }
        return 0;
    }
    for (i = 0U; i < plain_len; i++) {
        if (plain[i] != 0U) {
            any_non_zero = 1;
            break;
        }
    }
    if (!any_non_zero) {
        if (dbg != NULL) {
            dbg->reason_code = TC_CRYPTO_ERR_HEADER_INVALID;
        }
        return 0;
    }
    if (dbg != NULL && plain_len >= 4U) {
        dbg->flags_or_reserved = tc_read_u32_le(plain);
    }
    return 1;
}

static int tc_signaling_queue_push_locked(tc_engine_t *engine, const uint8_t *data, size_t len) {
    tc_frame *frame = NULL;
    if (engine == NULL || data == NULL || len == 0U || len > TC_MAX_SIGNALING_BYTES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    if (engine->params.on_signaling != NULL) {
        engine->params.on_signaling(engine->params.user_data, data, len);
        return TC_ENGINE_OK;
    }

    if (engine->signaling_size >= TC_MAX_SIGNALING_FRAMES) {
        return TC_ENGINE_ERR_QUEUE_FULL;
    }
    frame = &engine->signaling_out[engine->signaling_tail];
    frame->len = len;
    memcpy(frame->bytes, data, len);
    engine->signaling_tail = (engine->signaling_tail + 1U) % TC_MAX_SIGNALING_FRAMES;
    engine->signaling_size += 1U;
    return TC_ENGINE_OK;
}

static const uint8_t *tc_pick_peer_tag_locked(
    const tc_engine_t *engine,
    size_t *peer_tag_len_out
) {
    size_t i = 0U;
    if (peer_tag_len_out == NULL || engine == NULL) {
        return NULL;
    }
    *peer_tag_len_out = 0U;

    if (engine->active_endpoint_index >= 0 &&
        (size_t)engine->active_endpoint_index < engine->endpoint_count) {
        const tc_endpoint_runtime *ep = &engine->endpoints[engine->active_endpoint_index];
        if (ep->peer_tag_len == 16U) {
            *peer_tag_len_out = 16U;
            return ep->peer_tag;
        }
    }

    for (i = 0U; i < engine->endpoint_count; i++) {
        const tc_endpoint_runtime *ep = &engine->endpoints[i];
        if (ep->peer_tag_len == 16U) {
            *peer_tag_len_out = 16U;
            return ep->peer_tag;
        }
    }
    return NULL;
}

static int tc_strip_peer_tag_locked(
    const tc_engine_t *engine,
    const uint8_t **data_inout,
    size_t *len_inout
) {
    size_t i = 0U;
    if (engine == NULL || data_inout == NULL || len_inout == NULL || *data_inout == NULL) {
        return 0;
    }
    if (*len_inout <= 16U) {
        return 0;
    }

    for (i = 0U; i < engine->endpoint_count; i++) {
        const tc_endpoint_runtime *ep = &engine->endpoints[i];
        if (ep->peer_tag_len != 16U) {
            continue;
        }
        if (memcmp(*data_inout, ep->peer_tag, 16U) == 0) {
            *data_inout += 16U;
            *len_inout -= 16U;
            return 1;
        }
    }
    return 0;
}

static int tc_reflector_ready_locked(const tc_engine_t *engine) {
    if (engine == NULL) {
        return 0;
    }
    return engine->reflector.socket_open && engine->reflector.remote_ready;
}

static uint32_t tc_debug_endpoint_kind_from_flags(uint32_t flags) {
    if ((flags & TC_ENDPOINT_FLAG_TURN) != 0U || (flags & TC_ENDPOINT_FLAG_P2P) != 0U) {
        return TC_DEBUG_ENDPOINT_KIND_WEBRTC;
    }
    if ((flags & TC_ENDPOINT_FLAG_RELAY) != 0U) {
        return TC_DEBUG_ENDPOINT_KIND_RELAY;
    }
    return TC_DEBUG_ENDPOINT_KIND_UNKNOWN;
}

static int tc_endpoint_is_legacy_relay_candidate(const tc_endpoint_runtime *ep) {
    if (ep == NULL) {
        return 0;
    }
    if (ep->ip[0] == '\0' || ep->port == 0U) {
        return 0;
    }
    if ((ep->flags & TC_ENDPOINT_FLAG_RELAY) == 0U) {
        return 0;
    }
    if ((ep->flags & TC_ENDPOINT_FLAG_TURN) != 0U) {
        return 0;
    }
    if ((ep->flags & TC_ENDPOINT_FLAG_TCP) != 0U) {
        return 0;
    }
    return 1;
}

static int tc_apply_endpoint_index_locked(tc_engine_t *engine, size_t idx, const char *reason) {
    const tc_endpoint_runtime *ep = NULL;
    char data_json[512];

    if (engine == NULL || idx >= engine->endpoint_count) {
        return -1;
    }
    ep = &engine->endpoints[idx];
    if (ep->ip[0] == '\0' || ep->port == 0U) {
        return -1;
    }
    if (tc_reflector_transport_set_remote_ipv4(&engine->reflector, ep->ip, ep->port) != 0) {
        return -1;
    }

    engine->active_endpoint_index = (int)idx;
    engine->stats.endpoint_id = ep->id;
    engine->debug.selected_endpoint_id = ep->id;
    engine->debug.selected_endpoint_kind = tc_debug_endpoint_kind_from_flags(ep->flags);
    engine->last_endpoint_fallback_ms = tc_now_ms();

    snprintf(
        data_json,
        sizeof(data_json),
        "{\"call_id\":%d,\"reason\":\"%s\",\"index\":%llu,\"endpoint_id\":%lld,"
        "\"port\":%u,\"flags\":%u,\"kind\":%u}",
        (int)engine->params.call_id,
        (reason != NULL) ? reason : "select",
        (unsigned long long)idx,
        (long long)ep->id,
        (unsigned int)ep->port,
        (unsigned int)ep->flags,
        (unsigned int)engine->debug.selected_endpoint_kind
    );
    tc_agent_debug_log("run1", "H19", "engine.c:endpoint_apply", "udp.endpoint_selected", data_json);
    return 0;
}

static int tc_rotate_relay_endpoint_locked(tc_engine_t *engine, const char *reason) {
    size_t start = 0U;
    size_t i = 0U;
    size_t attempts = 0U;
    int found_any = 0;

    if (engine == NULL || engine->endpoint_count == 0U) {
        return -1;
    }
    if (engine->active_endpoint_index >= 0) {
        start = ((size_t)engine->active_endpoint_index + 1U) % engine->endpoint_count;
    }

    for (attempts = 0U; attempts < engine->endpoint_count; attempts++) {
        i = (start + attempts) % engine->endpoint_count;
        if (!tc_endpoint_is_legacy_relay_candidate(&engine->endpoints[i])) {
            continue;
        }
        found_any = 1;
        if ((int)i == engine->active_endpoint_index) {
            continue;
        }
        engine->endpoint_fallback_rounds += 1U;
        return tc_apply_endpoint_index_locked(engine, i, reason);
    }

    if (!found_any) {
        engine->debug.selected_endpoint_kind = TC_DEBUG_ENDPOINT_KIND_UNKNOWN;
    }
    return -1;
}

static int tc_send_udp_frame_locked(
    tc_engine_t *engine,
    const uint8_t *encrypted,
    size_t encrypted_len
) {
    uint8_t framed[TC_MAX_SIGNALING_BYTES];
    const uint8_t *peer_tag = NULL;
    size_t peer_tag_len = 0U;
    size_t framed_len = 0U;
    int rc = 0;

    if (engine == NULL || encrypted == NULL || encrypted_len == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    peer_tag = tc_pick_peer_tag_locked(engine, &peer_tag_len);
    if (peer_tag_len > 0U && peer_tag != NULL) {
        if ((peer_tag_len + encrypted_len) > sizeof(framed)) {
            return TC_ENGINE_ERR_QUEUE_FULL;
        }
        memcpy(framed, peer_tag, peer_tag_len);
        memcpy(framed + peer_tag_len, encrypted, encrypted_len);
        framed_len = peer_tag_len + encrypted_len;
    } else {
        if (encrypted_len > sizeof(framed)) {
            return TC_ENGINE_ERR_QUEUE_FULL;
        }
        memcpy(framed, encrypted, encrypted_len);
        framed_len = encrypted_len;
    }

    rc = tc_reflector_transport_send(&engine->reflector, framed, framed_len);
    if (rc < 0) {
        return TC_ENGINE_ERR;
    }
    if (rc == 0) {
        return TC_ENGINE_ERR_QUEUE_FULL;
    }

    engine->debug.udp_out_frames += 1U;
    engine->debug.udp_tx_bytes += (uint64_t)rc;
    engine->stats.packets_sent += 1U;
    engine->bytes_sent_window += (uint64_t)rc;
    return TC_ENGINE_OK;
}

static int tc_pump_udp_locked(tc_engine_t *engine) {
    uint8_t packet[TC_MAX_SIGNALING_BYTES];
    uint8_t plain[TC_MAX_SIGNALING_BYTES];
    const uint8_t *cipher = NULL;
    size_t cipher_len = 0U;
    size_t plain_len = 0U;
    tc_proto_header_t header;
    const uint8_t *extras = NULL;
    const uint8_t *payload = NULL;
    size_t extras_len = 0U;
    size_t payload_len = 0U;
    tc_proto_rx_seq_update_t seq_update;
    int rc = 0;
    int loops = 0;
    int decrypt_ok = 0;
    int recv_status = TC_REFLECTOR_RECV_NONE;
    int directions[2];
    uint32_t ctr_seq = 0U;
    size_t i = 0U;
    int processed_any = 0;

    if (engine == NULL) {
        return 0;
    }
    if (!engine->keys_ready) {
        return 0;
    }

    for (loops = 0; loops < 4; loops++) {
        engine->debug.udp_recv_attempts += 1U;
        recv_status = TC_REFLECTOR_RECV_NONE;
        rc = tc_reflector_transport_recv(
            &engine->reflector,
            packet,
            sizeof(packet),
            0,
            &recv_status
        );
        if (rc <= 0) {
            if (recv_status == TC_REFLECTOR_RECV_TIMEOUT) {
                engine->debug.udp_recv_timeouts += 1U;
            } else if (recv_status == TC_REFLECTOR_RECV_SOURCE_MISMATCH) {
                engine->debug.udp_recv_source_mismatch += 1U;
                processed_any = 1;
                continue;
            }
            break;
        }
        engine->debug.udp_in_frames += 1U;
        engine->debug.udp_rx_bytes += (uint64_t)rc;
        engine->stats.packets_recv += 1U;
        engine->bytes_recv_window += (uint64_t)rc;
        engine->last_rx_ms = tc_now_ms();
        engine->last_udp_rx_ms = engine->last_rx_ms;
        cipher = packet;
        cipher_len = (size_t)rc;
        if (!tc_strip_peer_tag_locked(engine, &cipher, &cipher_len)) {
            size_t expected_peer_tag_len = 0U;
            (void)tc_pick_peer_tag_locked(engine, &expected_peer_tag_len);
            if (expected_peer_tag_len == 16U && cipher_len > 16U) {
                engine->debug.udp_rx_peer_tag_mismatch += 1U;
            }
        }
        if (cipher_len == 0U) {
            engine->debug.udp_rx_short_packet_drops += 1U;
            continue;
        }

        decrypt_ok = 0;
        directions[0] = engine->is_outgoing;
        directions[1] = engine->is_outgoing ? 0 : 1;
        for (i = 0U; i < 2U; i++) {
            if (tc_mtproto2_decrypt_ctr(
                    engine->key_material,
                    engine->key_len,
                    cipher,
                    cipher_len,
                    directions[i],
                    plain,
                    sizeof(plain),
                    &plain_len,
                    &ctr_seq
                ) == 0) {
                decrypt_ok = 1;
                break;
            }
        }
        if (!decrypt_ok) {
            for (i = 0U; i < 2U; i++) {
                if (tc_mtproto2_decrypt_short(
                        engine->key_material,
                        engine->key_len,
                        cipher,
                        cipher_len,
                        directions[i],
                        plain,
                        sizeof(plain),
                        &plain_len
                    ) == 0) {
                    decrypt_ok = 1;
                    break;
                }
            }
        }
        if (!decrypt_ok) {
            engine->debug.decrypt_failures_udp += 1U;
            tc_update_bitrate_locked(engine, engine->last_rx_ms);
            tc_update_loss_stats_locked(engine);
            processed_any = 1;
            continue;
        }

        rc = tc_proto_decode_short(
            plain,
            plain_len,
            &header,
            &extras,
            &extras_len,
            &payload,
            &payload_len
        );
        if (rc != 0) {
            engine->debug.udp_proto_decode_failures += 1U;
            tc_update_bitrate_locked(engine, engine->last_rx_ms);
            tc_update_loss_stats_locked(engine);
            processed_any = 1;
            continue;
        }

        if (extras != NULL && extras_len > 0U) {
            tc_proto_extra_t parsed[8];
            size_t parsed_count = 0U;
            if (tc_proto_parse_extras(extras, extras_len, parsed, 8U, &parsed_count) != 0) {
                engine->debug.udp_proto_decode_failures += 1U;
                tc_update_bitrate_locked(engine, engine->last_rx_ms);
                tc_update_loss_stats_locked(engine);
                processed_any = 1;
                continue;
            }
        }

        tc_proto_update_rx_seq(&engine->rx_seq, header.seq, &seq_update);
        engine->in_lost_count += (uint64_t)seq_update.lost_count_increment;

        rc = tc_handle_packet_locked(
            engine,
            TC_PACKET_PLANE_UDP,
            &header,
            payload,
            payload_len
        );
        if (rc != TC_ENGINE_OK) {
            /* Never crash or fail the whole session on a single malformed UDP packet. */
            tc_update_bitrate_locked(engine, engine->last_rx_ms);
            tc_update_loss_stats_locked(engine);
            processed_any = 1;
            continue;
        }

        tc_update_bitrate_locked(engine, engine->last_rx_ms);
        tc_update_loss_stats_locked(engine);
        processed_any = 1;
    }

    if (processed_any) {
        tc_emit_stats(engine);
    }
    return 0;
}

static int tc_signaling_queue_pop_locked(tc_engine_t *engine, uint8_t *out, size_t out_cap) {
    tc_frame *frame = NULL;
    size_t out_len = 0;

    if (engine == NULL || out == NULL || out_cap == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    if (engine->signaling_size == 0U) {
        return 0;
    }

    frame = &engine->signaling_out[engine->signaling_head];
    out_len = frame->len;
    if (out_len > out_cap) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    memcpy(out, frame->bytes, out_len);
    memset(frame->bytes, 0, frame->len);
    frame->len = 0U;

    engine->signaling_head = (engine->signaling_head + 1U) % TC_MAX_SIGNALING_FRAMES;
    engine->signaling_size -= 1U;
    return (int)out_len;
}

static void tc_pcm_queue_push(tc_engine_t *engine, const int16_t *pcm, int frame_samples) {
    size_t slot = 0U;
    int samples = frame_samples;
    if (samples > TC_PCM_FRAME_SAMPLES) {
        samples = TC_PCM_FRAME_SAMPLES;
    }
    if (samples <= 0) {
        return;
    }

    if (engine->pcm_size >= TC_PCM_QUEUE_FRAMES) {
        engine->pcm_head = (engine->pcm_head + 1U) % TC_PCM_QUEUE_FRAMES;
        engine->pcm_size -= 1U;
    }

    slot = engine->pcm_tail;
    memset(engine->pcm_queue[slot], 0, sizeof(engine->pcm_queue[slot]));
    memcpy(engine->pcm_queue[slot], pcm, (size_t)samples * sizeof(int16_t));
    engine->pcm_lengths[slot] = samples;

    engine->pcm_tail = (engine->pcm_tail + 1U) % TC_PCM_QUEUE_FRAMES;
    engine->pcm_size += 1U;
}

static int tc_pcm_queue_pop(tc_engine_t *engine, int16_t *out, int frame_samples) {
    size_t slot = 0U;
    int samples = 0;
    int copy_samples = 0;

    if (engine->pcm_size == 0U || frame_samples <= 0) {
        return 0;
    }

    slot = engine->pcm_head;
    samples = engine->pcm_lengths[slot];
    if (samples <= 0) {
        engine->pcm_head = (engine->pcm_head + 1U) % TC_PCM_QUEUE_FRAMES;
        engine->pcm_size -= 1U;
        return 0;
    }

    copy_samples = frame_samples;
    if (copy_samples > samples) {
        copy_samples = samples;
    }

    memset(out, 0, (size_t)frame_samples * sizeof(int16_t));
    memcpy(out, engine->pcm_queue[slot], (size_t)copy_samples * sizeof(int16_t));
    memset(engine->pcm_queue[slot], 0, sizeof(engine->pcm_queue[slot]));
    engine->pcm_lengths[slot] = 0;

    engine->pcm_head = (engine->pcm_head + 1U) % TC_PCM_QUEUE_FRAMES;
    engine->pcm_size -= 1U;
    return copy_samples;
}

static void tc_update_bitrate_locked(tc_engine_t *engine, uint64_t now_ms) {
    uint64_t elapsed = 0;
    uint64_t bits = 0;

    if (engine->window_started_ms == 0U) {
        engine->window_started_ms = now_ms;
        return;
    }

    elapsed = now_ms - engine->window_started_ms;
    if (elapsed < 1000U) {
        return;
    }

    bits = (engine->bytes_sent_window + engine->bytes_recv_window) * 8U;
    engine->stats.bitrate_kbps = (float)bits / (float)elapsed;
    engine->bytes_sent_window = 0U;
    engine->bytes_recv_window = 0U;
    engine->window_started_ms = now_ms;
}

static void tc_update_loss_stats_locked(tc_engine_t *engine) {
    uint64_t out_total = engine->out_acked_count + engine->out_lost_count;
    uint64_t in_total = engine->stats.signaling_packets_recv + engine->in_lost_count;

    engine->stats.send_loss = (out_total > 0U) ? ((float)engine->out_lost_count / (float)out_total) : 0.0f;
    engine->stats.recv_loss = (in_total > 0U) ? ((float)engine->in_lost_count / (float)in_total) : 0.0f;
    engine->stats.loss = engine->stats.recv_loss;
}

static void tc_track_outgoing_seq_locked(tc_engine_t *engine, uint32_t seq, uint64_t now_ms) {
    tc_out_track_t *slot = &engine->out_track[engine->out_track_next % TC_MAX_OUT_TRACK];
    slot->seq = seq;
    slot->sent_ms = now_ms;
    slot->used = 1;
    slot->acked = 0;
    engine->out_track_next = (engine->out_track_next + 1U) % TC_MAX_OUT_TRACK;
}

static void tc_apply_remote_acks_locked(tc_engine_t *engine, uint32_t ack_seq, uint32_t ack_mask) {
    size_t i = 0U;
    uint64_t now_ms = tc_now_ms();

    for (i = 0U; i < TC_MAX_OUT_TRACK; i++) {
        tc_out_track_t *item = &engine->out_track[i];
        if (!item->used || item->acked) {
            continue;
        }
        if (tc_proto_header_acks(ack_seq, ack_mask, item->seq)) {
            item->acked = 1;
            engine->out_acked_count += 1U;
            continue;
        }
        if (now_ms > item->sent_ms && (now_ms - item->sent_ms) > 8000U) {
            item->acked = 1;
            engine->out_lost_count += 1U;
        }
    }

    tc_update_loss_stats_locked(engine);
}

static int tc_build_init_payload(
    const tc_engine_t *engine,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
) {
    if (engine == NULL || out == NULL || out_len == NULL || out_cap < 16U) {
        return -1;
    }

    tc_write_u32_le(out, engine->protocol_version);
    tc_write_u32_le(out + 4U, engine->min_protocol_version);
    tc_write_u32_le(out + 8U, 0U);
    out[12] = 1U;
    tc_write_u32_le(out + 13U, TC_CODEC_OPUS_FOURCC);
    out[17] = 0U;
    out[18] = 0U;

    *out_len = 19U;
    return 0;
}

static int tc_build_init_ack_payload(
    const tc_engine_t *engine,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
) {
    if (engine == NULL || out == NULL || out_len == NULL || out_cap < 16U) {
        return -1;
    }

    tc_write_u32_le(out, engine->protocol_version);
    tc_write_u32_le(out + 4U, engine->min_protocol_version);
    out[8] = 1U;
    out[9] = 1U;
    out[10] = 1U;
    tc_write_u32_le(out + 11U, TC_CODEC_OPUS_FOURCC);
    tc_write_u16_le(out + 15U, 20U);
    out[17] = 1U;

    *out_len = 18U;
    return 0;
}

static int tc_send_proto_packet_locked(
    tc_engine_t *engine,
    tc_packet_plane_t plane,
    uint8_t pkt_type,
    const uint8_t *payload,
    size_t payload_len
) {
    tc_proto_header_t header;
    uint8_t plain[TC_MAX_SIGNALING_BYTES];
    uint8_t encrypted[TC_MAX_SIGNALING_BYTES];
    uint8_t framed[TC_MAX_SIGNALING_BYTES];
    uint8_t extras[16];
    size_t extras_len = 0U;
    size_t plain_len = 0U;
    size_t encrypted_len = 0U;
    size_t framed_len = 0U;
    uint64_t now_ms = tc_now_ms();
    uint32_t seq = 0U;
    uint32_t payload_head_le = 0U;
    int rc = 0;

    if (!engine->keys_ready) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    memset(&header, 0, sizeof(header));
    header.type = pkt_type;
    header.ack_id = engine->rx_seq.last_remote_seq;
    header.recent_mask = engine->rx_seq.recent_mask;
    engine->local_seq += 1U;
    seq = engine->local_seq;
    header.seq = seq;

    if (engine->pending_stream_flags || engine->pending_network_changed) {
        uint8_t count = 0U;
        size_t cursor = 1U;
        memset(extras, 0, sizeof(extras));

        if (engine->pending_stream_flags) {
            extras[cursor++] = 2U;
            extras[cursor++] = TC_EXTRA_TYPE_STREAM_FLAGS;
            extras[cursor++] = (uint8_t)(engine->muted ? 8 : 1);
            count += 1U;
            engine->pending_stream_flags = 0;
        }

        if (engine->pending_network_changed) {
            extras[cursor++] = 2U;
            extras[cursor++] = TC_EXTRA_TYPE_NETWORK_CHANGED;
            extras[cursor++] = (uint8_t)engine->network_type;
            count += 1U;
            engine->pending_network_changed = 0;
        }

        extras[0] = count;
        extras_len = cursor;
        header.flags |= TC_XPFLAG_HAS_EXTRA;
    }

    rc = tc_proto_encode_short(
        &header,
        extras_len > 0U ? extras : NULL,
        extras_len,
        payload,
        payload_len,
        plain,
        sizeof(plain),
        &plain_len
    );
    if (rc != 0) {
        return TC_ENGINE_ERR;
    }

    /*
     * Use AES-CTR encryption with tgcalls signaling format.
     * The seq is embedded inside the encrypted payload as a 4-byte
     * network-order value (tgcalls convention), separate from the
     * libtgvoip seq in the proto header.
     */
    rc = tc_mtproto2_encrypt_ctr(
        engine->key_material,
        engine->key_len,
        plain,
        plain_len,
        seq,     /* tgcalls seq in NBO inside encrypted payload */
        engine->is_outgoing,
        encrypted,
        sizeof(encrypted),
        &encrypted_len
    );
    if (rc != 0) {
        return TC_ENGINE_ERR;
    }

    if (plane == TC_PACKET_PLANE_UDP) {
        rc = tc_send_udp_frame_locked(engine, encrypted, encrypted_len);
        if (rc != TC_ENGINE_OK) {
            return rc;
        }
        tc_track_outgoing_seq_locked(engine, seq, now_ms);
        tc_update_bitrate_locked(engine, now_ms);
        tc_update_loss_stats_locked(engine);
        tc_emit_stats(engine);
        return TC_ENGINE_OK;
    }

    if (engine->stats.signaling_packets_sent < 8U) {
        // #region agent log
        {
            char data_json[448];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"call_id\":%d,\"state\":%d,\"pkt_type\":%u,\"seq\":%u,"
                "\"plain_len\":%llu,\"encrypted_len\":%llu,"
                "\"is_outgoing\":%d,\"crypto\":\"CTR\"}",
                (int)engine->params.call_id,
                (int)engine->state,
                (unsigned int)header.type,
                (unsigned int)seq,
                (unsigned long long)plain_len,
                (unsigned long long)encrypted_len,
                (int)engine->is_outgoing
            );
            tc_agent_debug_log(
                "run1",
                "H19",
                "engine.c:send_ctr",
                "outgoing signaling CTR encrypted",
                data_json
            );
        }
        // #endregion
    }

    /*
     * Signaling packets are exchanged via MTProto phone.sendSignalingData.
     * Peer tags are transport-level hints for reflector UDP and should not be
     * prepended to signaling blobs.
     */
    if (encrypted_len > sizeof(framed)) {
        return TC_ENGINE_ERR_QUEUE_FULL;
    }
    memcpy(framed, encrypted, encrypted_len);
    framed_len = encrypted_len;

    rc = tc_signaling_queue_push_locked(engine, framed, framed_len);
    if (rc != TC_ENGINE_OK) {
        return rc;
    }

    engine->debug.signaling_out_frames += 1U;
    tc_track_outgoing_seq_locked(engine, seq, now_ms);
    engine->stats.packets_sent += 1U;
    engine->stats.signaling_packets_sent += 1U;
    engine->bytes_sent_window += framed_len;
    tc_update_bitrate_locked(engine, now_ms);
    tc_update_loss_stats_locked(engine);
    tc_emit_stats(engine);
    return TC_ENGINE_OK;
}

static void tc_maybe_mark_established_locked(tc_engine_t *engine) {
    if (engine->established) {
        return;
    }
    if (engine->received_init && engine->received_init_ack) {
        engine->established = 1;
        engine->established_at_ms = tc_now_ms();
        engine->last_endpoint_fallback_ms = engine->established_at_ms;
        // #region agent log
        {
            char data_json[320];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"call_id\":%d,\"state\":%d,\"received_init\":%d,"
                "\"received_init_ack\":%d,\"signaling_packets_recv\":%llu,"
                "\"signaling_packets_sent\":%llu}",
                (int)engine->params.call_id,
                (int)engine->state,
                (int)engine->received_init,
                (int)engine->received_init_ack,
                (unsigned long long)engine->stats.signaling_packets_recv,
                (unsigned long long)engine->stats.signaling_packets_sent
            );
            tc_agent_debug_log(
                "run1",
                "H13",
                "engine.c:575",
                "native established gate passed",
                data_json
            );
        }
        // #endregion
        tc_emit_state(engine, TC_ENGINE_STATE_ESTABLISHED);
    }
}

static int tc_send_init_locked(tc_engine_t *engine) {
    uint8_t payload[64];
    size_t payload_len = 0U;
    if (tc_build_init_payload(engine, payload, sizeof(payload), &payload_len) != 0) {
        return TC_ENGINE_ERR;
    }
    engine->init_sent = 1;
    engine->last_init_tx_ms = tc_now_ms();
    tc_emit_state(engine, TC_ENGINE_STATE_WAIT_INIT_ACK);
    return tc_send_proto_packet_locked(
        engine,
        TC_PACKET_PLANE_SIGNALING,
        TC_PKT_INIT,
        payload,
        payload_len
    );
}

static int tc_send_init_ack_locked(tc_engine_t *engine) {
    uint8_t payload[64];
    size_t payload_len = 0U;
    if (tc_build_init_ack_payload(engine, payload, sizeof(payload), &payload_len) != 0) {
        return TC_ENGINE_ERR;
    }
    engine->init_ack_sent = 1;
    return tc_send_proto_packet_locked(
        engine,
        TC_PACKET_PLANE_SIGNALING,
        TC_PKT_INIT_ACK,
        payload,
        payload_len
    );
}

static int tc_send_ping_locked(tc_engine_t *engine, tc_packet_plane_t plane) {
    uint8_t payload[8];
    uint64_t now_ms = tc_now_ms();
    tc_write_u32_le(payload, (uint32_t)(now_ms & 0xFFFFFFFFU));
    tc_write_u32_le(payload + 4U, (uint32_t)((now_ms >> 32) & 0xFFFFFFFFU));
    if (plane == TC_PACKET_PLANE_UDP) {
        engine->last_udp_ping_tx_ms = now_ms;
    } else {
        engine->last_ping_tx_ms = now_ms;
    }
    return tc_send_proto_packet_locked(engine, plane, TC_PKT_PING, payload, sizeof(payload));
}

static int tc_send_pong_locked(
    tc_engine_t *engine,
    tc_packet_plane_t plane,
    const uint8_t *payload,
    size_t payload_len
) {
    return tc_send_proto_packet_locked(engine, plane, TC_PKT_PONG, payload, payload_len);
}

static void tc_tick_locked(tc_engine_t *engine) {
    uint64_t now_ms = tc_now_ms();

    if (!engine->keys_ready || engine->state == TC_ENGINE_STATE_STOPPED ||
        engine->state == TC_ENGINE_STATE_FAILED) {
        return;
    }

    if (!engine->init_sent ||
        (!engine->received_init_ack && (now_ms - engine->last_init_tx_ms) >= 700U)) {
        (void)tc_send_init_locked(engine);
    }

    if (engine->established && (now_ms - engine->last_ping_tx_ms) >= 1000U) {
        (void)tc_send_ping_locked(engine, TC_PACKET_PLANE_SIGNALING);
    }

    if (engine->established && tc_reflector_ready_locked(engine) &&
        engine->endpoint_count > 1U &&
        engine->debug.udp_rx_bytes == 0U &&
        (now_ms > engine->established_at_ms) &&
        (now_ms - engine->established_at_ms) >= 2500U &&
        (now_ms - engine->last_endpoint_fallback_ms) >= 2000U &&
        engine->endpoint_fallback_rounds < engine->endpoint_count) {
        (void)tc_rotate_relay_endpoint_locked(engine, "no_udp_rx");
    }

    if (engine->established && tc_reflector_ready_locked(engine) &&
        (now_ms - engine->last_udp_ping_tx_ms) >= 1000U) {
        (void)tc_send_ping_locked(engine, TC_PACKET_PLANE_UDP);
    }

    (void)tc_pump_udp_locked(engine);
}

static int tc_handle_packet_locked(
    tc_engine_t *engine,
    tc_packet_plane_t plane,
    const tc_proto_header_t *header,
    const uint8_t *payload,
    size_t payload_len
) {
    tc_proto_extra_t parsed[8];
    size_t parsed_count = 0U;

    if (header == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    (void)payload;
    (void)payload_len;

    tc_apply_remote_acks_locked(engine, header->ack_id, header->recent_mask);

    if ((header->flags & TC_XPFLAG_HAS_EXTRA) != 0U) {
        /* extras are parsed at decode stage and ignored when unknown */
        (void)parsed;
        (void)parsed_count;
    }

    switch (header->type) {
        case TC_PKT_INIT: {
            uint32_t peer_ver = 0U;
            uint32_t peer_min = 0U;
            if (payload_len < 8U) {
                return TC_ENGINE_ERR;
            }
            peer_ver = tc_read_u32_le(payload);
            peer_min = tc_read_u32_le(payload + 4U);
            if (peer_min > engine->protocol_version || peer_ver < engine->min_protocol_version) {
                return TC_ENGINE_ERR;
            }

            engine->received_init = 1;
            if (!engine->init_ack_sent) {
                (void)tc_send_init_ack_locked(engine);
            }
            if (!engine->init_sent) {
                (void)tc_send_init_locked(engine);
            }
            break;
        }
        case TC_PKT_INIT_ACK: {
            uint32_t peer_ver = 0U;
            uint32_t peer_min = 0U;
            if (payload_len >= 8U) {
                peer_ver = tc_read_u32_le(payload);
                peer_min = tc_read_u32_le(payload + 4U);
                if (peer_min > engine->protocol_version || peer_ver < engine->min_protocol_version) {
                    return TC_ENGINE_ERR;
                }
            }
            engine->received_init_ack = 1;
            break;
        }
        case TC_PKT_PING:
            (void)tc_send_pong_locked(engine, plane, payload, payload_len);
            break;
        case TC_PKT_PONG:
            if (payload_len >= 8U) {
                uint64_t then_ms = (uint64_t)tc_read_u32_le(payload) |
                                   ((uint64_t)tc_read_u32_le(payload + 4U) << 32);
                uint64_t now_ms = tc_now_ms();
                if (now_ms > then_ms) {
                    engine->stats.rtt_ms = (float)(now_ms - then_ms);
                }
            }
            break;
        case TC_PKT_STREAM_STATE:
            /* Stream flags are consumed implicitly; no hard failure on unknown flags. */
            break;
        case TC_PKT_STREAM_DATA:
            if (engine->codec != NULL && payload != NULL && payload_len > 0U) {
                int16_t decoded[TC_PCM_FRAME_SAMPLES];
                int decoded_samples = tc_media_decode_frame(
                    engine->codec,
                    payload,
                    (int)payload_len,
                    decoded,
                    TC_PCM_FRAME_SAMPLES
                );
                if (decoded_samples > 0) {
                    tc_pcm_queue_push(engine, decoded, decoded_samples);
                    engine->stats.media_packets_recv += 1U;
                }
            }
            break;
        case TC_PKT_NOP:
            break;
        default:
            /* Unknown type should not crash receiver. */
            break;
    }

    tc_maybe_mark_established_locked(engine);
    return TC_ENGINE_OK;
}

static void tc_reset_runtime_state(tc_engine_t *engine) {
    memset(&engine->rx_seq, 0, sizeof(engine->rx_seq));
    memset(engine->out_track, 0, sizeof(engine->out_track));
    engine->out_track_next = 0U;
    engine->local_seq = 0U;
    engine->received_init = 0;
    engine->received_init_ack = 0;
    engine->init_sent = 0;
    engine->init_ack_sent = 0;
    engine->established = 0;
    engine->last_init_tx_ms = 0U;
    engine->last_ping_tx_ms = 0U;
    engine->last_udp_ping_tx_ms = 0U;
    engine->last_rx_ms = 0U;
    engine->last_udp_rx_ms = 0U;
    engine->established_at_ms = 0U;
    engine->last_endpoint_fallback_ms = 0U;
    engine->endpoint_fallback_rounds = 0U;
    engine->out_acked_count = 0U;
    engine->out_lost_count = 0U;
    engine->in_lost_count = 0U;
    engine->bytes_sent_window = 0U;
    engine->bytes_recv_window = 0U;
    engine->window_started_ms = tc_now_ms();
    engine->pcm_head = 0U;
    engine->pcm_tail = 0U;
    engine->pcm_size = 0U;
    memset(engine->pcm_lengths, 0, sizeof(engine->pcm_lengths));
    memset(&engine->stats, 0, sizeof(engine->stats));
    tc_debug_stats_zero(&engine->debug);
    memset(engine->last_signaling_cipher_head, 0, sizeof(engine->last_signaling_cipher_head));
    engine->last_signaling_cipher_head_len = 0U;
    engine->last_signaling_cipher_len = 0U;
    if (engine->active_endpoint_index >= 0 &&
        (size_t)engine->active_endpoint_index < engine->endpoint_count) {
        const tc_endpoint_runtime *ep = &engine->endpoints[engine->active_endpoint_index];
        engine->stats.endpoint_id = ep->id;
        engine->debug.selected_endpoint_id = ep->id;
        engine->debug.selected_endpoint_kind = tc_debug_endpoint_kind_from_flags(ep->flags);
    } else {
        engine->stats.endpoint_id = 0;
        engine->debug.selected_endpoint_id = 0;
        engine->debug.selected_endpoint_kind = TC_DEBUG_ENDPOINT_KIND_UNKNOWN;
    }
}

tc_engine_t *tc_engine_create(const tc_engine_params_t *params) {
    tc_engine_t *engine = NULL;
    if (params == NULL) {
        return NULL;
    }
    if (params->on_state == NULL || params->on_error == NULL || params->on_signaling == NULL ||
        params->on_stats == NULL) {
        return NULL;
    }

    engine = (tc_engine_t *)calloc(1U, sizeof(tc_engine_t));
    if (engine == NULL) {
        return NULL;
    }

    memcpy(&engine->params, params, sizeof(tc_engine_params_t));
    engine->state = TC_ENGINE_STATE_IDLE;
    engine->active_endpoint_index = -1;
    engine->bitrate_hint_kbps = 24;
    engine->network_type = TC_NETWORK_TYPE_WIFI;
    engine->protocol_version = TC_VOIP_PROTOCOL_VERSION;
    engine->min_protocol_version = TC_VOIP_MIN_PROTOCOL_VERSION;
    engine->protocol_min_layer = 65;
    engine->protocol_max_layer = 92;
    engine->pending_network_changed = 1;
    engine->pending_stream_flags = 1;
    tc_reflector_transport_init(&engine->reflector);

    if (pthread_mutex_init(&engine->mu, NULL) != 0) {
        free(engine);
        return NULL;
    }

    tc_reset_runtime_state(engine);
    return engine;
}

int tc_engine_start(tc_engine_t *engine) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_RUNNING || engine->state == TC_ENGINE_STATE_WAIT_INIT ||
        engine->state == TC_ENGINE_STATE_WAIT_INIT_ACK ||
        engine->state == TC_ENGINE_STATE_ESTABLISHED) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }

    tc_emit_state(engine, TC_ENGINE_STATE_STARTING);
    tc_emit_state(engine, TC_ENGINE_STATE_RUNNING);

    if (engine->codec == NULL) {
        engine->codec = tc_opus_codec_create(48000, 1, engine->bitrate_hint_kbps * 1000);
    }

    if (engine->keys_ready) {
        tc_emit_state(engine, TC_ENGINE_STATE_WAIT_INIT);
        tc_tick_locked(engine);
    }

    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_push_signaling(tc_engine_t *engine, const uint8_t *data, size_t len) {
    const uint8_t *cipher = data;
    size_t cipher_len = len;
    const uint8_t *cipher_alt = data;
    size_t cipher_alt_len = len;
    uint8_t plain[TC_MAX_SIGNALING_BYTES];
    size_t plain_len = 0U;
    tc_proto_header_t header;
    const uint8_t *extras = NULL;
    const uint8_t *payload = NULL;
    size_t extras_len = 0U;
    size_t payload_len = 0U;
    tc_proto_rx_seq_update_t seq_update;
    int rc = TC_ENGINE_OK;
    int decrypt_ok = 0;
    int ctr_decrypt_ok = 0;
    int stripped_tag = 0;
    int plain_rc = -99;
    int plain_alt_rc = -99;
    int known_type = 0;
    tc_proto_header_t plain_header;
    tc_proto_header_t plain_header_alt;
    const uint8_t *plain_extras = NULL;
    const uint8_t *plain_payload = NULL;
    const uint8_t *plain_extras_alt = NULL;
    const uint8_t *plain_payload_alt = NULL;
    size_t plain_extras_len = 0U;
    size_t plain_payload_len = 0U;
    size_t plain_extras_len_alt = 0U;
    size_t plain_payload_len_alt = 0U;
    uint32_t plain_payload_head_le = 0U;
    uint32_t plain_alt_payload_head_le = 0U;
    int directions[2];
    int direction_codes[2];
    int ctr_decrypt_rc_primary[2] = {-999, -999};
    int ctr_decrypt_rc_alt[2] = {-999, -999};
    int ctr_candidate_proto_primary[2] = {-999, -999};
    int ctr_candidate_proto_alt[2] = {-999, -999};
    int short_candidate_proto_primary[2] = {-999, -999};
    int short_candidate_proto_alt[2] = {-999, -999};
    int decrypt_rc_primary[2] = {-999, -999};
    int decrypt_rc_alt[2] = {-999, -999};
    int ctr_reason_primary[2] = {0, 0};
    int ctr_reason_alt[2] = {0, 0};
    int short_reason_primary[2] = {0, 0};
    int short_reason_alt[2] = {0, 0};
    int ctr_last_error_code = 0;
    int short_last_error_code = 0;
    uint32_t ctr_last_variant_code = TC_DEBUG_SIGNALING_CTR_VARIANT_NONE;
    uint32_t ctr_last_hash_mode_code = TC_DEBUG_SIGNALING_CTR_HASH_NONE;
    int ctr_candidates_attempted = 0;
    int ctr_candidates_succeeded = 0;
    int short_proto_ready = 0;
    int selected_direction_code = TC_DEBUG_SIGNALING_DECRYPT_DIR_NONE;
    uint32_t selected_decrypt_mode = TC_DEBUG_SIGNALING_DECRYPT_MODE_NONE;
    uint32_t selected_ctr_variant_code = TC_DEBUG_SIGNALING_CTR_VARIANT_NONE;
    uint32_t selected_ctr_hash_mode_code = TC_DEBUG_SIGNALING_CTR_HASH_NONE;
    int selected_candidate_index = -1;
    tc_crypto_debug_t crypto_dbg;
    size_t i = 0U;

    if (engine == NULL || data == NULL || len == 0U || len > TC_MAX_SIGNALING_BYTES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_STOPPED || engine->state == TC_ENGINE_STATE_FAILED) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    if (!engine->keys_ready) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    stripped_tag = tc_strip_peer_tag_locked(engine, &cipher, &cipher_len);
    if (cipher_len == 0U) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }
    tc_track_signaling_cipher_duplicate_locked(engine, data, len);
    engine->debug.signaling_last_decrypt_mode = TC_DEBUG_SIGNALING_DECRYPT_MODE_NONE;
    engine->debug.signaling_last_decrypt_direction = TC_DEBUG_SIGNALING_DECRYPT_DIR_NONE;
    engine->debug.signaling_decrypt_last_error_code = 0;
    engine->debug.signaling_decrypt_last_error_stage = TC_DEBUG_SIGNALING_ERROR_STAGE_NONE;
    engine->debug.signaling_proto_last_error_code = 0;
    engine->debug.signaling_candidate_winner_index = -1;
    engine->debug.signaling_ctr_last_error_code = 0;
    engine->debug.signaling_short_last_error_code = 0;
    engine->debug.signaling_ctr_last_variant = TC_DEBUG_SIGNALING_CTR_VARIANT_NONE;
    engine->debug.signaling_ctr_last_hash_mode = TC_DEBUG_SIGNALING_CTR_HASH_NONE;
    engine->debug.signaling_best_failure_mode = TC_DEBUG_SIGNALING_BEST_FAILURE_NONE;
    engine->debug.signaling_best_failure_code = 0;

    directions[0] = engine->is_outgoing;
    directions[1] = engine->is_outgoing ? 0 : 1;
    direction_codes[0] = TC_DEBUG_SIGNALING_DECRYPT_DIR_LOCAL_ROLE;
    direction_codes[1] = TC_DEBUG_SIGNALING_DECRYPT_DIR_OPPOSITE_ROLE;

    /*
     * Candidate matrix (mode x direction), preferring a candidate that is
     * decrypt-valid and format-valid for its mode:
     * - CTR (tgcalls signaling): decrypt success is considered valid.
     * - SHORT/IGE (legacy): decrypt success must also pass proto decode (+extras).
     *
     * If peer-tag stripping did not match but packet is long enough, also try a
     * compatibility path that drops a 16-byte prefix (common reflector tag shape).
     */
    if (!stripped_tag && cipher_alt_len > 16U) {
        cipher_alt += 16U;
        cipher_alt_len -= 16U;
    } else {
        cipher_alt = NULL;
        cipher_alt_len = 0U;
    }

    /* Phase 1: AES-CTR signaling with compat variants (KDF family + hash mode). */
    if (!decrypt_ok) {
        int kdf_families[2] = {TC_CTR_KDF_SIGNALING_128, TC_CTR_KDF_SIGNALING_0};
        uint32_t kdf_variant_codes[2] = {
            TC_DEBUG_SIGNALING_CTR_VARIANT_KDF128,
            TC_DEBUG_SIGNALING_CTR_VARIANT_KDF0
        };
        int hash_modes[2] = {TC_CTR_HASH_SEQ_PLUS_PAYLOAD, TC_CTR_HASH_PAYLOAD_ONLY};
        uint32_t hash_mode_codes[2] = {
            TC_DEBUG_SIGNALING_CTR_HASH_SEQ_PAYLOAD,
            TC_DEBUG_SIGNALING_CTR_HASH_PAYLOAD_ONLY
        };
        size_t hm = 0U;
        size_t kf = 0U;
        size_t strip_idx = 0U;
        for (hm = 0U; hm < 2U && !decrypt_ok; hm++) {
            for (kf = 0U; kf < 2U && !decrypt_ok; kf++) {
                for (strip_idx = 0U; strip_idx < 2U && !decrypt_ok; strip_idx++) {
                    const uint8_t *ctr_cipher = (strip_idx == 0U) ? cipher : cipher_alt;
                    size_t ctr_cipher_len = (strip_idx == 0U) ? cipher_len : cipher_alt_len;
                    if (ctr_cipher == NULL || ctr_cipher_len == 0U) {
                        continue;
                    }
                    for (i = 0U; i < 2U && !decrypt_ok; i++) {
                        uint32_t ctr_seq = 0U;
                        int candidate_index = (int)(hm * 8U + kf * 4U + strip_idx * 2U + i);
                        ctr_candidates_attempted += 1;
                        rc = tc_mtproto2_decrypt_ctr_variant_ex(
                            engine->key_material,
                            engine->key_len,
                            ctr_cipher,
                            ctr_cipher_len,
                            directions[i],
                            kdf_families[kf],
                            hash_modes[hm],
                            plain,
                            sizeof(plain),
                            &plain_len,
                            &ctr_seq,
                            &crypto_dbg
                        );
                        if (strip_idx == 0U) {
                            ctr_decrypt_rc_primary[i] = rc;
                            ctr_reason_primary[i] = crypto_dbg.reason_code;
                        } else {
                            ctr_decrypt_rc_alt[i] = rc;
                            ctr_reason_alt[i] = crypto_dbg.reason_code;
                        }
                        ctr_last_error_code = crypto_dbg.reason_code;
                        ctr_last_variant_code = kdf_variant_codes[kf];
                        ctr_last_hash_mode_code = hash_mode_codes[hm];
                        engine->debug.signaling_ctr_last_error_code = (int32_t)ctr_last_error_code;
                        engine->debug.signaling_ctr_last_variant = ctr_last_variant_code;
                        engine->debug.signaling_ctr_last_hash_mode = ctr_last_hash_mode_code;
                        if (rc != 0) {
                            engine->debug.signaling_decrypt_ctr_failures += 1U;
                            tc_set_signaling_decrypt_error_locked(
                                engine,
                                TC_DEBUG_SIGNALING_ERROR_STAGE_CTR,
                                (int32_t)crypto_dbg.reason_code
                            );
                            continue;
                        }
                        if (!tc_signaling_ctr_payload_sane(plain, plain_len, &crypto_dbg)) {
                            engine->debug.signaling_decrypt_ctr_header_invalid += 1U;
                            ctr_last_error_code = crypto_dbg.reason_code;
                            engine->debug.signaling_ctr_last_error_code =
                                (int32_t)ctr_last_error_code;
                            tc_set_signaling_decrypt_error_locked(
                                engine,
                                TC_DEBUG_SIGNALING_ERROR_STAGE_CTR,
                                (int32_t)crypto_dbg.reason_code
                            );
                            continue;
                        }
                        ctr_candidates_succeeded += 1;
                        engine->debug.signaling_decrypt_candidate_successes += 1U;
                        if (strip_idx == 0U) {
                            ctr_candidate_proto_primary[i] = 0;
                        } else {
                            ctr_candidate_proto_alt[i] = 0;
                        }
                        decrypt_ok = 1;
                        ctr_decrypt_ok = 1;
                        selected_decrypt_mode = TC_DEBUG_SIGNALING_DECRYPT_MODE_CTR;
                        selected_direction_code = direction_codes[i];
                        selected_candidate_index = candidate_index;
                        selected_ctr_variant_code = kdf_variant_codes[kf];
                        selected_ctr_hash_mode_code = hash_mode_codes[hm];
                        if (engine->stats.signaling_packets_recv < 8U) {
                            char hex_buf[97];
                            size_t hx = 0U;
                            size_t hmax = plain_len < 48U ? plain_len : 48U;
                            for (hx = 0U; hx < hmax; hx++) {
                                snprintf(hex_buf + hx * 2U, 3, "%02x", (unsigned int)plain[hx]);
                            }
                            hex_buf[hmax * 2U] = '\0';
                            {
                                char data_json[640];
                                snprintf(
                                    data_json,
                                    sizeof(data_json),
                                    "{\"call_id\":%d,\"ctr_seq\":%u,\"dir\":%d,\"plain_len\":%llu,"
                                    "\"head_hex\":\"%s\",\"key_len\":%llu,\"kdf_variant\":%u,"
                                    "\"hash_mode\":%u,\"strip16\":%d,\"winner_index\":%d}",
                                    (int)engine->params.call_id,
                                    (unsigned int)ctr_seq,
                                    (int)directions[i],
                                    (unsigned long long)plain_len,
                                    hex_buf,
                                    (unsigned long long)engine->key_len,
                                    (unsigned int)selected_ctr_variant_code,
                                    (unsigned int)selected_ctr_hash_mode_code,
                                    (int)(strip_idx != 0U),
                                    selected_candidate_index
                                );
                                tc_agent_debug_log(
                                    "run1",
                                    "H19",
                                    "engine.c:ctr_decrypt_ok",
                                    "CTR decrypt succeeded",
                                    data_json
                                );
                            }
                        }
                    }
                }
            }
        }
    }
    if (!decrypt_ok && ctr_candidates_attempted > 0 && ctr_candidates_succeeded == 0) {
        tc_set_signaling_best_failure_locked(
            engine,
            TC_DEBUG_SIGNALING_BEST_FAILURE_CTR,
            (int32_t)ctr_last_error_code
        );
    }

    /* Phase 2: AES-IGE/short signaling (legacy), but only accept proto-valid decode. */
    if (!decrypt_ok) {
        for (i = 0U; i < 2U; i++) {
            rc = tc_mtproto2_decrypt_short_ex(
                engine->key_material,
                engine->key_len,
                cipher,
                cipher_len,
                directions[i],
                plain,
                sizeof(plain),
                &plain_len,
                &crypto_dbg
            );
            decrypt_rc_primary[i] = rc;
            short_reason_primary[i] = crypto_dbg.reason_code;
            if (rc != 0) {
                engine->debug.signaling_decrypt_short_failures += 1U;
                short_last_error_code = crypto_dbg.reason_code;
                engine->debug.signaling_short_last_error_code = (int32_t)short_last_error_code;
                tc_set_signaling_decrypt_error_locked(
                    engine,
                    TC_DEBUG_SIGNALING_ERROR_STAGE_SHORT,
                    (int32_t)crypto_dbg.reason_code
                );
                continue;
            }
            engine->debug.signaling_decrypt_candidate_successes += 1U;
            rc = tc_proto_decode_short(
                plain,
                plain_len,
                &header,
                &extras,
                &extras_len,
                &payload,
                &payload_len
            );
            short_candidate_proto_primary[i] = rc;
            if (rc != 0) {
                engine->debug.signaling_proto_decode_failures += 1U;
                engine->debug.signaling_proto_last_error_code = (int32_t)rc;
                short_last_error_code = (int)rc;
                engine->debug.signaling_short_last_error_code = (int32_t)short_last_error_code;
                continue;
            }
            if (extras != NULL && extras_len > 0U) {
                tc_proto_extra_t parsed[8];
                size_t parsed_count = 0U;
                rc = tc_proto_parse_extras(extras, extras_len, parsed, 8U, &parsed_count);
                short_candidate_proto_primary[i] = rc;
                if (rc != 0) {
                    engine->debug.signaling_proto_decode_failures += 1U;
                    engine->debug.signaling_proto_last_error_code = (int32_t)rc;
                    short_last_error_code = (int)rc;
                    engine->debug.signaling_short_last_error_code =
                        (int32_t)short_last_error_code;
                    continue;
                }
            }
            decrypt_ok = 1;
            short_proto_ready = 1;
            selected_decrypt_mode = TC_DEBUG_SIGNALING_DECRYPT_MODE_SHORT;
            selected_direction_code = direction_codes[i];
            selected_candidate_index = 16 + (int)i;
            break;
        }
    }

    if (!decrypt_ok && cipher_alt != NULL) {
        for (i = 0U; i < 2U; i++) {
            rc = tc_mtproto2_decrypt_short_ex(
                engine->key_material,
                engine->key_len,
                cipher_alt,
                cipher_alt_len,
                directions[i],
                plain,
                sizeof(plain),
                &plain_len,
                &crypto_dbg
            );
            decrypt_rc_alt[i] = rc;
            short_reason_alt[i] = crypto_dbg.reason_code;
            if (rc != 0) {
                engine->debug.signaling_decrypt_short_failures += 1U;
                short_last_error_code = crypto_dbg.reason_code;
                engine->debug.signaling_short_last_error_code = (int32_t)short_last_error_code;
                tc_set_signaling_decrypt_error_locked(
                    engine,
                    TC_DEBUG_SIGNALING_ERROR_STAGE_SHORT,
                    (int32_t)crypto_dbg.reason_code
                );
                continue;
            }
            engine->debug.signaling_decrypt_candidate_successes += 1U;
            rc = tc_proto_decode_short(
                plain,
                plain_len,
                &header,
                &extras,
                &extras_len,
                &payload,
                &payload_len
            );
            short_candidate_proto_alt[i] = rc;
            if (rc != 0) {
                engine->debug.signaling_proto_decode_failures += 1U;
                engine->debug.signaling_proto_last_error_code = (int32_t)rc;
                short_last_error_code = (int)rc;
                engine->debug.signaling_short_last_error_code = (int32_t)short_last_error_code;
                continue;
            }
            if (extras != NULL && extras_len > 0U) {
                tc_proto_extra_t parsed[8];
                size_t parsed_count = 0U;
                rc = tc_proto_parse_extras(extras, extras_len, parsed, 8U, &parsed_count);
                short_candidate_proto_alt[i] = rc;
                if (rc != 0) {
                    engine->debug.signaling_proto_decode_failures += 1U;
                    engine->debug.signaling_proto_last_error_code = (int32_t)rc;
                    short_last_error_code = (int)rc;
                    engine->debug.signaling_short_last_error_code =
                        (int32_t)short_last_error_code;
                    continue;
                }
            }
            decrypt_ok = 1;
            short_proto_ready = 1;
            selected_decrypt_mode = TC_DEBUG_SIGNALING_DECRYPT_MODE_SHORT;
            selected_direction_code = direction_codes[i];
            selected_candidate_index = 18 + (int)i;
            break;
        }
    }

    if (!decrypt_ok && engine->debug.signaling_best_failure_mode == TC_DEBUG_SIGNALING_BEST_FAILURE_NONE &&
        short_last_error_code != 0) {
        tc_set_signaling_best_failure_locked(
            engine,
            TC_DEBUG_SIGNALING_BEST_FAILURE_SHORT,
            (int32_t)short_last_error_code
        );
    }

    if (!decrypt_ok) {
        engine->debug.decrypt_failures_signaling += 1U;
        engine->debug.signaling_last_decrypt_mode = TC_DEBUG_SIGNALING_DECRYPT_MODE_NONE;
        engine->debug.signaling_last_decrypt_direction = TC_DEBUG_SIGNALING_DECRYPT_DIR_NONE;
        /* #region agent log */
        {
            char data_json[2048];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"call_id\":%d,\"state\":%d,\"is_outgoing\":%d,"
                "\"len\":%llu,\"cipher_len\":%llu,\"stripped_tag\":%d,"
                "\"key_len\":%llu,"
                "\"rc_ctr_0\":%d,\"rc_ctr_1\":%d,\"rc_ctr_alt_0\":%d,\"rc_ctr_alt_1\":%d,"
                "\"reason_ctr_0\":%d,\"reason_ctr_1\":%d,"
                "\"reason_ctr_alt_0\":%d,\"reason_ctr_alt_1\":%d,"
                "\"rc_ige_0\":%d,\"rc_ige_1\":%d,\"rc_ige_alt_0\":%d,\"rc_ige_alt_1\":%d,"
                "\"reason_ige_0\":%d,\"reason_ige_1\":%d,"
                "\"reason_ige_alt_0\":%d,\"reason_ige_alt_1\":%d,"
                "\"proto_ige_0\":%d,\"proto_ige_1\":%d,"
                "\"proto_ige_alt_0\":%d,\"proto_ige_alt_1\":%d,"
                "\"last_dec_err_stage\":%u,\"last_dec_err_code\":%d,"
                "\"best_fail_mode\":%u,\"best_fail_code\":%d,"
                "\"ctr_last_err\":%d,\"short_last_err\":%d,"
                "\"ctr_last_variant\":%u,\"ctr_last_hash_mode\":%u,"
                "\"endpoint_count\":%llu}",
                (int)engine->params.call_id,
                (int)engine->state,
                (int)engine->is_outgoing,
                (unsigned long long)len,
                (unsigned long long)cipher_len,
                (int)stripped_tag,
                (unsigned long long)engine->key_len,
                (int)ctr_decrypt_rc_primary[0],
                (int)ctr_decrypt_rc_primary[1],
                (int)ctr_decrypt_rc_alt[0],
                (int)ctr_decrypt_rc_alt[1],
                (int)ctr_reason_primary[0],
                (int)ctr_reason_primary[1],
                (int)ctr_reason_alt[0],
                (int)ctr_reason_alt[1],
                (int)decrypt_rc_primary[0],
                (int)decrypt_rc_primary[1],
                (int)decrypt_rc_alt[0],
                (int)decrypt_rc_alt[1],
                (int)short_reason_primary[0],
                (int)short_reason_primary[1],
                (int)short_reason_alt[0],
                (int)short_reason_alt[1],
                (int)short_candidate_proto_primary[0],
                (int)short_candidate_proto_primary[1],
                (int)short_candidate_proto_alt[0],
                (int)short_candidate_proto_alt[1],
                (unsigned int)engine->debug.signaling_decrypt_last_error_stage,
                (int)engine->debug.signaling_decrypt_last_error_code,
                (unsigned int)engine->debug.signaling_best_failure_mode,
                (int)engine->debug.signaling_best_failure_code,
                (int)engine->debug.signaling_ctr_last_error_code,
                (int)engine->debug.signaling_short_last_error_code,
                (unsigned int)engine->debug.signaling_ctr_last_variant,
                (unsigned int)engine->debug.signaling_ctr_last_hash_mode,
                (unsigned long long)engine->endpoint_count
            );
            tc_agent_debug_log(
                "run1",
                "H19",
                "engine.c:all_decrypt_failed",
                "all decrypt attempts failed (CTR+IGE)",
                data_json
            );
        }
        /* #endregion */
        tc_emit_error(engine, TC_ENGINE_ERR, "failed to decrypt signaling packet");
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }

    engine->debug.signaling_last_decrypt_mode = selected_decrypt_mode;
    engine->debug.signaling_last_decrypt_direction = (uint32_t)selected_direction_code;
    engine->debug.signaling_candidate_winner_index = selected_candidate_index;
    engine->debug.signaling_decrypt_last_error_code = 0;
    engine->debug.signaling_decrypt_last_error_stage = TC_DEBUG_SIGNALING_ERROR_STAGE_NONE;
    engine->debug.signaling_proto_last_error_code = 0;
    engine->debug.signaling_best_failure_mode = TC_DEBUG_SIGNALING_BEST_FAILURE_NONE;
    engine->debug.signaling_best_failure_code = 0;
    if (selected_decrypt_mode == TC_DEBUG_SIGNALING_DECRYPT_MODE_CTR) {
        engine->debug.signaling_ctr_last_variant = selected_ctr_variant_code;
        engine->debug.signaling_ctr_last_hash_mode = selected_ctr_hash_mode_code;
    }

    /*
     * CTR-decrypted payloads are in tgcalls message format, not libtgvoip.
     * For now: treat any successfully CTR-decrypted packet as a valid
     * signaling exchange (update counters, mark INIT received so the
     * engine can progress to ESTABLISHED state), then skip the libtgvoip
     * protocol parser.
     *
     * TODO: Parse tgcalls message content to properly handle WebRTC
     * candidates, SDP exchange, etc. for full interop.
     */
    if (ctr_decrypt_ok) {
        engine->stats.packets_recv += 1U;
        engine->stats.signaling_packets_recv += 1U;
        engine->bytes_recv_window += len;
        engine->last_rx_ms = tc_now_ms();
        tc_update_bitrate_locked(engine, engine->last_rx_ms);
        tc_update_loss_stats_locked(engine);

        /* Mark init/init_ack so engine can reach ESTABLISHED state.
         * The remote tgcalls instance sending us ANY successfully-decrypted
         * signaling implies it accepted our call setup. */
        if (!engine->received_init) {
            engine->received_init = 1;
            if (!engine->init_ack_sent) {
                (void)tc_send_init_ack_locked(engine);
            }
        }
        if (!engine->received_init_ack) {
            engine->received_init_ack = 1;
        }
        tc_maybe_mark_established_locked(engine);

        tc_emit_stats(engine);
        tc_tick_locked(engine);
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }

    /* Legacy IGE path: parse as libtgvoip proto (prevalidated during candidate selection). */
    if (!short_proto_ready) {
        rc = tc_proto_decode_short(
            plain,
            plain_len,
            &header,
            &extras,
            &extras_len,
            &payload,
            &payload_len
        );
        if (rc != 0) {
            engine->debug.signaling_proto_decode_failures += 1U;
            engine->debug.signaling_proto_last_error_code = (int32_t)rc;
            tc_emit_error(engine, TC_ENGINE_ERR, "failed to parse signaling packet");
            pthread_mutex_unlock(&engine->mu);
            return TC_ENGINE_OK;
        }
    }
    known_type = (header.type == TC_PKT_INIT) ||
                 (header.type == TC_PKT_INIT_ACK) ||
                 (header.type == TC_PKT_PING) ||
                 (header.type == TC_PKT_PONG) ||
                 (header.type == TC_PKT_STREAM_STATE) ||
                 (header.type == TC_PKT_STREAM_DATA) ||
                 (header.type == TC_PKT_NOP);
    if (engine->stats.signaling_packets_recv < 5U || !known_type) {
        // #region agent log
        {
            char data_json[448];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"call_id\":%d,\"state\":%d,\"header_type\":%u,\"header_flags\":%u,"
                "\"ack_id\":%u,\"seq\":%u,\"recent_mask\":%u,\"payload_len\":%llu,"
                "\"extras_len\":%llu,\"known_type\":%d,\"decrypt_ok\":%d,"
                "\"plain_len\":%llu,\"signaling_recv_index\":%llu}",
                (int)engine->params.call_id,
                (int)engine->state,
                (unsigned int)header.type,
                (unsigned int)header.flags,
                (unsigned int)header.ack_id,
                (unsigned int)header.seq,
                (unsigned int)header.recent_mask,
                (unsigned long long)payload_len,
                (unsigned long long)extras_len,
                (int)known_type,
                (int)decrypt_ok,
                (unsigned long long)plain_len,
                (unsigned long long)(engine->stats.signaling_packets_recv + 1U)
            );
            tc_agent_debug_log(
                "run1",
                "H13",
                "engine.c:1011",
                "incoming signaling parsed",
                data_json
            );
        }
        // #endregion
    }

    if (!short_proto_ready && extras != NULL && extras_len > 0U) {
        tc_proto_extra_t parsed[8];
        size_t parsed_count = 0U;
        if (tc_proto_parse_extras(extras, extras_len, parsed, 8U, &parsed_count) != 0) {
            engine->debug.signaling_proto_decode_failures += 1U;
            engine->debug.signaling_proto_last_error_code = -1;
            tc_emit_error(engine, TC_ENGINE_ERR, "failed to parse packet extras");
            pthread_mutex_unlock(&engine->mu);
            return TC_ENGINE_OK;
        }
    }

    tc_proto_update_rx_seq(&engine->rx_seq, header.seq, &seq_update);
    engine->in_lost_count += (uint64_t)seq_update.lost_count_increment;

    engine->stats.packets_recv += 1U;
    engine->stats.signaling_packets_recv += 1U;
    engine->bytes_recv_window += len;
    engine->last_rx_ms = tc_now_ms();
    tc_update_bitrate_locked(engine, engine->last_rx_ms);
    tc_update_loss_stats_locked(engine);

    rc = tc_handle_packet_locked(
        engine,
        TC_PACKET_PLANE_SIGNALING,
        &header,
        payload,
        payload_len
    );
    if (rc != TC_ENGINE_OK) {
        tc_emit_error(engine, rc, "protocol mismatch while handling signaling packet");
        tc_emit_state(engine, TC_ENGINE_STATE_FAILED);
        pthread_mutex_unlock(&engine->mu);
        return rc;
    }

    tc_emit_stats(engine);
    tc_tick_locked(engine);
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_pull_signaling(tc_engine_t *engine, uint8_t *out, size_t out_cap) {
    int rc = 0;
    if (engine == NULL || out == NULL || out_cap == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_STOPPED || engine->state == TC_ENGINE_STATE_FAILED) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    tc_tick_locked(engine);
    rc = tc_signaling_queue_pop_locked(engine, out, out_cap);
    if (rc > 0) {
        engine->stats.packets_sent += 1U;
        engine->stats.signaling_packets_sent += 1U;
        engine->bytes_sent_window += (size_t)rc;
        tc_update_bitrate_locked(engine, tc_now_ms());
        tc_update_loss_stats_locked(engine);
        tc_emit_stats(engine);
    }
    pthread_mutex_unlock(&engine->mu);
    return rc;
}

int tc_engine_set_keys(
    tc_engine_t *engine,
    const uint8_t *key_material,
    size_t key_len,
    int64_t key_fingerprint,
    int is_outgoing
) {
    if (engine == NULL || key_material == NULL || key_len < 128U || key_len > TC_MAX_KEY_BYTES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    memcpy(engine->key_material, key_material, key_len);
    engine->key_len = key_len;
    engine->key_fingerprint = key_fingerprint;
    engine->keys_ready = 1;
    engine->is_outgoing = (is_outgoing != 0) ? 1 : 0;

    // #region agent log
    {
        char data_json[384];
        snprintf(
            data_json,
            sizeof(data_json),
            "{\"call_id\":%d,\"state\":%d,\"key_len\":%llu,\"is_outgoing\":%d,"
            "\"pre_media_packets_sent\":%llu,\"pre_media_packets_recv\":%llu,"
            "\"pre_signaling_packets_recv\":%llu}",
            (int)engine->params.call_id,
            (int)engine->state,
            (unsigned long long)key_len,
            (int)engine->is_outgoing,
            (unsigned long long)engine->stats.media_packets_sent,
            (unsigned long long)engine->stats.media_packets_recv,
            (unsigned long long)engine->stats.signaling_packets_recv
        );
        tc_agent_debug_log("run1", "H3", "engine.c:983", "set_keys resets runtime state", data_json);
    }
    // #endregion
    tc_reset_runtime_state(engine);
    tc_emit_state(engine, TC_ENGINE_STATE_WAIT_INIT);

    if (engine->codec != NULL) {
        tc_opus_codec_destroy(engine->codec);
        engine->codec = NULL;
    }
    engine->codec = tc_opus_codec_create(48000, 1, engine->bitrate_hint_kbps * 1000);

    tc_tick_locked(engine);
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_set_remote_endpoints(
    tc_engine_t *engine,
    const tc_endpoint_t *endpoints,
    size_t count
) {
    size_t i = 0U;

    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    engine->endpoint_count = 0U;
    engine->active_endpoint_index = -1;
    tc_reflector_transport_reset(&engine->reflector);

    if (endpoints == NULL || count == 0U) {
        engine->stats.endpoint_id = 0;
        engine->debug.selected_endpoint_id = 0;
        engine->debug.selected_endpoint_kind = TC_DEBUG_ENDPOINT_KIND_UNKNOWN;
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }

    if (count > TC_MAX_ENDPOINTS) {
        count = TC_MAX_ENDPOINTS;
    }

    for (i = 0U; i < count; i++) {
        tc_endpoint_runtime *dst = &engine->endpoints[i];
        const tc_endpoint_t *src = &endpoints[i];

        memset(dst, 0, sizeof(*dst));
        dst->id = src->id;
        dst->port = src->port;
        dst->flags = src->flags;
        dst->priority = src->priority;

        if (src->ip != NULL) {
            strncpy(dst->ip, src->ip, TC_MAX_IP_STR - 1U);
            dst->ip[TC_MAX_IP_STR - 1U] = '\0';
        }
        if (src->ipv6 != NULL) {
            strncpy(dst->ipv6, src->ipv6, TC_MAX_IPV6_STR - 1U);
            dst->ipv6[TC_MAX_IPV6_STR - 1U] = '\0';
        }
        if (src->peer_tag != NULL && src->peer_tag_len > 0U) {
            size_t tag_len = src->peer_tag_len;
            if (tag_len > TC_MAX_PEER_TAG) {
                tag_len = TC_MAX_PEER_TAG;
            }
            memcpy(dst->peer_tag, src->peer_tag, tag_len);
            dst->peer_tag_len = tag_len;
        }
    }

    engine->endpoint_count = count;
    engine->active_endpoint_index = -1;
    engine->stats.endpoint_id = 0;
    engine->debug.selected_endpoint_id = 0;
    engine->debug.selected_endpoint_kind = TC_DEBUG_ENDPOINT_KIND_UNKNOWN;
    for (i = 0U; i < engine->endpoint_count; i++) {
        if (!tc_endpoint_is_legacy_relay_candidate(&engine->endpoints[i])) {
            continue;
        }
        if (tc_apply_endpoint_index_locked(engine, i, "initial_relay") == 0) {
            break;
        }
    }
    if (engine->active_endpoint_index < 0) {
        for (i = 0U; i < engine->endpoint_count; i++) {
            if (tc_apply_endpoint_index_locked(engine, i, "initial_fallback") == 0) {
                break;
            }
        }
    }
    // #region agent log
    {
        size_t tagged_count = 0U;
        uint32_t first_tag_u32 = 0U;
        for (i = 0U; i < engine->endpoint_count; i++) {
            if (engine->endpoints[i].peer_tag_len == 16U) {
                tagged_count += 1U;
            }
        }
        if (engine->endpoint_count > 0U && engine->endpoints[0].peer_tag_len >= 4U) {
            first_tag_u32 = ((uint32_t)engine->endpoints[0].peer_tag[0]) |
                            ((uint32_t)engine->endpoints[0].peer_tag[1] << 8) |
                            ((uint32_t)engine->endpoints[0].peer_tag[2] << 16) |
                            ((uint32_t)engine->endpoints[0].peer_tag[3] << 24);
        }
        {
            char data_json[320];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"call_id\":%d,\"endpoint_count\":%llu,\"tagged_count\":%llu,"
                "\"active_index\":%d,\"first_endpoint_id\":%lld,\"first_peer_tag_len\":%llu,"
                "\"first_peer_tag_u32\":%u}",
                (int)engine->params.call_id,
                (unsigned long long)engine->endpoint_count,
                (unsigned long long)tagged_count,
                (int)engine->active_endpoint_index,
                (long long)(
                    engine->active_endpoint_index >= 0
                    ? engine->endpoints[engine->active_endpoint_index].id
                    : 0LL
                ),
                (unsigned long long)(
                    engine->active_endpoint_index >= 0
                    ? engine->endpoints[engine->active_endpoint_index].peer_tag_len
                    : 0U
                ),
                (unsigned int)first_tag_u32
            );
            tc_agent_debug_log(
                "run1",
                "H7",
                "engine.c:1141",
                "remote endpoints applied",
                data_json
            );
        }
    }
    // #endregion
    tc_emit_stats(engine);

    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_set_protocol_config(tc_engine_t *engine, const tc_protocol_config_t *config) {
    int should_restart = 0;
    int changed = 0;
    uint32_t next_protocol_version = 0U;
    uint32_t next_min_protocol_version = 0U;
    int next_min_layer = 0;
    int next_max_layer = 0;
    if (engine == NULL || config == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (config->protocol_version == 0U || config->min_protocol_version == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (config->max_layer < config->min_layer) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    next_protocol_version = (uint32_t)config->protocol_version;
    next_min_protocol_version = (uint32_t)config->min_protocol_version;
    next_min_layer = (int)config->min_layer;
    next_max_layer = (int)config->max_layer;
    changed = (engine->protocol_version != next_protocol_version) ||
              (engine->min_protocol_version != next_min_protocol_version) ||
              (engine->protocol_min_layer != next_min_layer) ||
              (engine->protocol_max_layer != next_max_layer);
    engine->protocol_version = next_protocol_version;
    engine->min_protocol_version = next_min_protocol_version;
    engine->protocol_min_layer = next_min_layer;
    engine->protocol_max_layer = next_max_layer;

    should_restart = changed && engine->keys_ready &&
                     (engine->state == TC_ENGINE_STATE_RUNNING ||
                      engine->state == TC_ENGINE_STATE_WAIT_INIT ||
                      engine->state == TC_ENGINE_STATE_WAIT_INIT_ACK ||
                      engine->state == TC_ENGINE_STATE_ESTABLISHED);
    // #region agent log
    {
        char data_json[448];
        snprintf(
            data_json,
            sizeof(data_json),
            "{\"call_id\":%d,\"state\":%d,\"keys_ready\":%d,\"should_restart\":%d,"
                "\"changed\":%d,\"protocol_version\":%u,\"min_protocol_version\":%u,"
                "\"min_layer\":%d,\"max_layer\":%d,"
            "\"pre_media_packets_sent\":%llu,\"pre_media_packets_recv\":%llu,"
            "\"pre_signaling_packets_recv\":%llu}",
            (int)engine->params.call_id,
            (int)engine->state,
            (int)engine->keys_ready,
            (int)should_restart,
                (int)changed,
            (unsigned int)engine->protocol_version,
            (unsigned int)engine->min_protocol_version,
            (int)engine->protocol_min_layer,
            (int)engine->protocol_max_layer,
            (unsigned long long)engine->stats.media_packets_sent,
            (unsigned long long)engine->stats.media_packets_recv,
            (unsigned long long)engine->stats.signaling_packets_recv
        );
        tc_agent_debug_log(
            "run1",
            "H3",
            "engine.c:1077",
            "set_protocol_config evaluated",
            data_json
        );
    }
    // #endregion
    if (should_restart) {
        tc_reset_runtime_state(engine);
        tc_emit_state(engine, TC_ENGINE_STATE_WAIT_INIT);
        tc_tick_locked(engine);
    }
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_set_network_type(tc_engine_t *engine, int network_type) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    engine->network_type = network_type;
    engine->pending_network_changed = 1;
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_poll_stats(tc_engine_t *engine, tc_stats_t *stats_out) {
    if (engine == NULL || stats_out == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    tc_tick_locked(engine);
    memcpy(stats_out, &engine->stats, sizeof(tc_stats_t));
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_poll_debug(tc_engine_t *engine, tc_debug_stats_t *debug_out) {
    if (engine == NULL || debug_out == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    tc_tick_locked(engine);
    memcpy(debug_out, &engine->debug, sizeof(tc_debug_stats_t));
    debug_out->raw_packets_sent = engine->stats.packets_sent;
    debug_out->raw_packets_recv = engine->stats.packets_recv;
    debug_out->raw_media_packets_sent = engine->stats.media_packets_sent;
    debug_out->raw_media_packets_recv = engine->stats.media_packets_recv;
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_set_mute(tc_engine_t *engine, int muted) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    engine->muted = (muted != 0) ? 1 : 0;
    engine->pending_stream_flags = 1;
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_set_bitrate_hint(tc_engine_t *engine, int bitrate_kbps) {
    int rc = TC_ENGINE_OK;
    if (engine == NULL || bitrate_kbps <= 0) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    engine->bitrate_hint_kbps = bitrate_kbps;
    if (engine->codec != NULL) {
        rc = tc_opus_codec_set_bitrate(engine->codec, bitrate_kbps * 1000);
    }
    pthread_mutex_unlock(&engine->mu);
    return rc;
}

int tc_engine_push_audio_frame(tc_engine_t *engine, const int16_t *pcm, int frame_samples) {
    uint8_t encoded[512];
    int encoded_len = 0;
    int send_rc = TC_ENGINE_OK;

    if (engine == NULL || pcm == NULL || frame_samples <= 0 || frame_samples > TC_PCM_FRAME_SAMPLES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_STOPPED || engine->state == TC_ENGINE_STATE_FAILED) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    if (engine->muted) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }
    if (engine->codec == NULL) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    encoded_len = tc_media_encode_frame(
        engine->codec,
        pcm,
        frame_samples,
        encoded,
        (int)sizeof(encoded)
    );
    if (encoded_len <= 0) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR;
    }

    send_rc = tc_send_proto_packet_locked(
        engine,
        TC_PACKET_PLANE_UDP,
        TC_PKT_STREAM_DATA,
        encoded,
        (size_t)encoded_len
    );
    if (send_rc == TC_ENGINE_OK) {
        engine->stats.media_packets_sent += 1U;
        if (engine->stats.media_packets_sent <= 3U ||
            (engine->stats.media_packets_sent % 200U) == 0U) {
            // #region agent log
            {
                char data_json[320];
                snprintf(
                    data_json,
                    sizeof(data_json),
                    "{\"call_id\":%d,\"state\":%d,\"encoded_len\":%d,"
                    "\"media_packets_sent\":%llu,\"packets_sent\":%llu}",
                    (int)engine->params.call_id,
                    (int)engine->state,
                    (int)encoded_len,
                    (unsigned long long)engine->stats.media_packets_sent,
                    (unsigned long long)engine->stats.packets_sent
                );
                tc_agent_debug_log(
                    "run1",
                    "H4",
                    "engine.c:1175",
                    "audio frame accepted by native engine",
                    data_json
                );
            }
            // #endregion
        }
    } else {
        // #region agent log
        {
            char data_json[256];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"call_id\":%d,\"state\":%d,\"send_rc\":%d,\"encoded_len\":%d}",
                (int)engine->params.call_id,
                (int)engine->state,
                (int)send_rc,
                (int)encoded_len
            );
            tc_agent_debug_log(
                "run1",
                "H5",
                "engine.c:1173",
                "audio frame rejected by native engine",
                data_json
            );
        }
        // #endregion
    }
    pthread_mutex_unlock(&engine->mu);
    return send_rc;
}

int tc_engine_pull_audio_frame(tc_engine_t *engine, int16_t *pcm_out, int frame_samples) {
    int rc = 0;
    if (engine == NULL || pcm_out == NULL || frame_samples <= 0 || frame_samples > TC_PCM_FRAME_SAMPLES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_STOPPED || engine->state == TC_ENGINE_STATE_FAILED) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    rc = tc_pcm_queue_pop(engine, pcm_out, frame_samples);
    pthread_mutex_unlock(&engine->mu);
    return rc;
}

int tc_engine_stop(tc_engine_t *engine) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_STOPPED) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }
    tc_reflector_transport_reset(&engine->reflector);
    tc_emit_state(engine, TC_ENGINE_STATE_STOPPED);
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

void tc_engine_destroy(tc_engine_t *engine) {
    if (engine == NULL) {
        return;
    }

    (void)tc_engine_stop(engine);
    if (engine->codec != NULL) {
        tc_opus_codec_destroy(engine->codec);
        engine->codec = NULL;
    }
    tc_reflector_transport_reset(&engine->reflector);

    memset(engine->key_material, 0, sizeof(engine->key_material));
    engine->key_len = 0U;
    engine->key_fingerprint = 0;

    pthread_mutex_destroy(&engine->mu);
    free(engine);
}
