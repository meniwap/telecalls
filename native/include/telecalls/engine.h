#ifndef TELECALLS_ENGINE_H
#define TELECALLS_ENGINE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tc_engine tc_engine_t;
typedef struct tc_stats_t tc_stats_t;
typedef struct tc_debug_stats_t tc_debug_stats_t;

typedef void (*tc_engine_on_state_fn)(void *user_data, int state);
typedef void (*tc_engine_on_error_fn)(void *user_data, int code, const char *message);
typedef void (*tc_engine_on_signaling_fn)(void *user_data, const uint8_t *data, size_t len);
typedef void (*tc_engine_on_stats_fn)(void *user_data, const tc_stats_t *stats);

typedef struct tc_engine_params_t {
    uint64_t call_id;
    int incoming;
    int video;
    void *user_data;
    tc_engine_on_state_fn on_state;
    tc_engine_on_error_fn on_error;
    tc_engine_on_signaling_fn on_signaling;
    tc_engine_on_stats_fn on_stats;
} tc_engine_params_t;

typedef struct tc_endpoint_t {
    int64_t id;
    const char *ip;
    const char *ipv6;
    uint16_t port;
    const uint8_t *peer_tag;
    size_t peer_tag_len;
    uint32_t flags;
    uint32_t priority;
} tc_endpoint_t;

struct tc_stats_t {
    float rtt_ms;
    float loss;
    float bitrate_kbps;
    float jitter_ms;
    uint64_t packets_sent;
    uint64_t packets_recv;
    uint64_t media_packets_sent;
    uint64_t media_packets_recv;
    uint64_t signaling_packets_sent;
    uint64_t signaling_packets_recv;
    float send_loss;
    float recv_loss;
    int64_t endpoint_id;
};

struct tc_debug_stats_t {
    uint64_t signaling_out_frames;
    uint64_t udp_out_frames;
    uint64_t udp_in_frames;
    uint64_t udp_recv_attempts;
    uint64_t udp_recv_timeouts;
    uint64_t udp_recv_source_mismatch;
    uint64_t udp_proto_decode_failures;
    uint64_t udp_rx_peer_tag_mismatch;
    uint64_t udp_rx_short_packet_drops;
    uint64_t decrypt_failures_signaling;
    uint64_t decrypt_failures_udp;
    uint64_t signaling_proto_decode_failures;
    uint64_t signaling_decrypt_ctr_failures;
    uint64_t signaling_decrypt_short_failures;
    uint64_t signaling_decrypt_ctr_header_invalid;
    uint64_t signaling_decrypt_candidate_successes;
    uint64_t signaling_duplicate_ciphertexts_seen;
    int32_t signaling_ctr_last_error_code;
    int32_t signaling_short_last_error_code;
    uint32_t signaling_ctr_last_variant;
    uint32_t signaling_ctr_last_hash_mode;
    uint32_t signaling_best_failure_mode;
    int32_t signaling_best_failure_code;
    uint32_t signaling_last_decrypt_mode;
    uint32_t signaling_last_decrypt_direction;
    int32_t signaling_decrypt_last_error_code;
    uint32_t signaling_decrypt_last_error_stage;
    int32_t signaling_proto_last_error_code;
    int32_t signaling_candidate_winner_index;
    uint64_t udp_tx_bytes;
    uint64_t udp_rx_bytes;
    uint64_t raw_packets_sent;
    uint64_t raw_packets_recv;
    uint64_t raw_media_packets_sent;
    uint64_t raw_media_packets_recv;
    int64_t selected_endpoint_id;
    uint32_t selected_endpoint_kind;
};

typedef struct tc_protocol_config_t {
    uint32_t protocol_version;
    uint32_t min_protocol_version;
    int32_t min_layer;
    int32_t max_layer;
} tc_protocol_config_t;

enum tc_engine_state {
    TC_ENGINE_STATE_IDLE = 0,
    TC_ENGINE_STATE_STARTING = 1,
    TC_ENGINE_STATE_RUNNING = 2,
    TC_ENGINE_STATE_STOPPED = 3,
    TC_ENGINE_STATE_FAILED = 4,
    TC_ENGINE_STATE_WAIT_INIT = 5,
    TC_ENGINE_STATE_WAIT_INIT_ACK = 6,
    TC_ENGINE_STATE_ESTABLISHED = 7
};

enum tc_network_type {
    TC_NETWORK_TYPE_UNKNOWN = 0,
    TC_NETWORK_TYPE_WIFI = 1,
    TC_NETWORK_TYPE_ETHERNET = 2,
    TC_NETWORK_TYPE_CELLULAR = 3
};

enum tc_engine_result {
    TC_ENGINE_OK = 0,
    TC_ENGINE_ERR = -1,
    TC_ENGINE_ERR_INVALID_ARG = -2,
    TC_ENGINE_ERR_QUEUE_FULL = -3,
    TC_ENGINE_ERR_NOT_RUNNING = -4
};

tc_engine_t *tc_engine_create(const tc_engine_params_t *params);
int tc_engine_start(tc_engine_t *engine);
int tc_engine_push_signaling(tc_engine_t *engine, const uint8_t *data, size_t len);
int tc_engine_pull_signaling(tc_engine_t *engine, uint8_t *out, size_t out_cap);
int tc_engine_set_keys(
    tc_engine_t *engine,
    const uint8_t *key_material,
    size_t key_len,
    int64_t key_fingerprint,
    int is_outgoing
);
int tc_engine_set_remote_endpoints(
    tc_engine_t *engine,
    const tc_endpoint_t *endpoints,
    size_t count
);
int tc_engine_set_protocol_config(tc_engine_t *engine, const tc_protocol_config_t *config);
int tc_engine_set_network_type(tc_engine_t *engine, int network_type);
int tc_engine_poll_stats(tc_engine_t *engine, tc_stats_t *stats_out);
int tc_engine_poll_debug(tc_engine_t *engine, tc_debug_stats_t *debug_out);
int tc_engine_set_mute(tc_engine_t *engine, int muted);
int tc_engine_set_bitrate_hint(tc_engine_t *engine, int bitrate_kbps);
int tc_engine_push_audio_frame(
    tc_engine_t *engine,
    const int16_t *pcm,
    int frame_samples
);
int tc_engine_pull_audio_frame(
    tc_engine_t *engine,
    int16_t *pcm_out,
    int frame_samples
);
int tc_engine_stop(tc_engine_t *engine);
void tc_engine_destroy(tc_engine_t *engine);

#ifdef __cplusplus
}
#endif

#endif
