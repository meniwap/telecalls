#ifndef TELECALLS_ENGINE_H
#define TELECALLS_ENGINE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tc_engine tc_engine_t;

typedef void (*tc_engine_on_state_fn)(void *user_data, int state);
typedef void (*tc_engine_on_error_fn)(void *user_data, int code, const char *message);
typedef void (*tc_engine_on_signaling_fn)(void *user_data, const uint8_t *data, size_t len);

typedef struct tc_engine_params_t {
    uint64_t call_id;
    int incoming;
    int video;
    void *user_data;
    tc_engine_on_state_fn on_state;
    tc_engine_on_error_fn on_error;
    tc_engine_on_signaling_fn on_signaling;
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

typedef struct tc_stats_t {
    float rtt_ms;
    float loss;
    float bitrate_kbps;
    float jitter_ms;
    uint64_t packets_sent;
    uint64_t packets_recv;
} tc_stats_t;

enum tc_engine_state {
    TC_ENGINE_STATE_IDLE = 0,
    TC_ENGINE_STATE_STARTING = 1,
    TC_ENGINE_STATE_RUNNING = 2,
    TC_ENGINE_STATE_STOPPED = 3,
    TC_ENGINE_STATE_FAILED = 4
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
    int64_t key_fingerprint
);
int tc_engine_set_remote_endpoints(
    tc_engine_t *engine,
    const tc_endpoint_t *endpoints,
    size_t count
);
int tc_engine_poll_stats(tc_engine_t *engine, tc_stats_t *stats_out);
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
