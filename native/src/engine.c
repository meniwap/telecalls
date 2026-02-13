#include "telecalls/engine.h"

#include <stdlib.h>
#include <string.h>

#define TC_MAX_SIGNALING_FRAMES 64
#define TC_MAX_SIGNALING_BYTES 16384
#define TC_MAX_ENDPOINTS 16
#define TC_MAX_KEY_BYTES 512

typedef struct tc_frame {
    size_t len;
    uint8_t bytes[TC_MAX_SIGNALING_BYTES];
} tc_frame;

struct tc_engine {
    tc_engine_params_t params;
    int state;

    tc_frame frames[TC_MAX_SIGNALING_FRAMES];
    size_t queue_head;
    size_t queue_tail;
    size_t queue_size;

    tc_endpoint_t endpoints[TC_MAX_ENDPOINTS];
    size_t endpoint_count;

    uint8_t key_material[TC_MAX_KEY_BYTES];
    size_t key_len;
    int64_t key_fingerprint;

    tc_stats_t stats;
};

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

static int tc_queue_push(tc_engine_t *engine, const uint8_t *data, size_t len) {
    if (engine->queue_size >= TC_MAX_SIGNALING_FRAMES) {
        return TC_ENGINE_ERR_QUEUE_FULL;
    }
    tc_frame *frame = &engine->frames[engine->queue_tail];
    frame->len = len;
    if (len > 0) {
        memcpy(frame->bytes, data, len);
    }
    engine->queue_tail = (engine->queue_tail + 1U) % TC_MAX_SIGNALING_FRAMES;
    engine->queue_size += 1U;
    return TC_ENGINE_OK;
}

static int tc_queue_pop(tc_engine_t *engine, uint8_t *out, size_t out_cap) {
    tc_frame *frame = NULL;
    size_t out_len = 0;

    if (engine->queue_size == 0U) {
        return 0;
    }

    frame = &engine->frames[engine->queue_head];
    out_len = frame->len;
    if (out_len > out_cap) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    if (out_len > 0U) {
        memcpy(out, frame->bytes, out_len);
        memset(frame->bytes, 0, frame->len);
    }
    frame->len = 0U;

    engine->queue_head = (engine->queue_head + 1U) % TC_MAX_SIGNALING_FRAMES;
    engine->queue_size -= 1U;
    return (int)out_len;
}

tc_engine_t *tc_engine_create(const tc_engine_params_t *params) {
    tc_engine_t *engine = NULL;
    if (params == NULL) {
        return NULL;
    }

    engine = (tc_engine_t *)calloc(1U, sizeof(tc_engine_t));
    if (engine == NULL) {
        return NULL;
    }
    memcpy(&engine->params, params, sizeof(tc_engine_params_t));
    engine->state = TC_ENGINE_STATE_IDLE;
    return engine;
}

int tc_engine_start(tc_engine_t *engine) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->state == TC_ENGINE_STATE_RUNNING) {
        return TC_ENGINE_OK;
    }

    tc_emit_state(engine, TC_ENGINE_STATE_STARTING);
    tc_emit_state(engine, TC_ENGINE_STATE_RUNNING);
    return TC_ENGINE_OK;
}

int tc_engine_push_signaling(tc_engine_t *engine, const uint8_t *data, size_t len) {
    int rc = 0;
    if (engine == NULL || data == NULL || len == 0U || len > TC_MAX_SIGNALING_BYTES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->state != TC_ENGINE_STATE_RUNNING) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    rc = tc_queue_push(engine, data, len);
    if (rc != TC_ENGINE_OK) {
        tc_emit_error(engine, rc, "signaling queue full");
        return rc;
    }

    engine->stats.packets_recv += 1U;
    engine->stats.bitrate_kbps = (float)((len * 8U) / 1000U);
    engine->stats.rtt_ms = 50.0f;
    engine->stats.loss = 0.0f;
    engine->stats.jitter_ms = 2.0f;

    if (engine->params.on_signaling != NULL) {
        engine->params.on_signaling(engine->params.user_data, data, len);
    }
    return TC_ENGINE_OK;
}

int tc_engine_pull_signaling(tc_engine_t *engine, uint8_t *out, size_t out_cap) {
    int rc = 0;
    if (engine == NULL || out == NULL || out_cap == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->state != TC_ENGINE_STATE_RUNNING) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    rc = tc_queue_pop(engine, out, out_cap);
    if (rc > 0) {
        engine->stats.packets_sent += 1U;
    }
    return rc;
}

int tc_engine_set_keys(
    tc_engine_t *engine,
    const uint8_t *key_material,
    size_t key_len,
    int64_t key_fingerprint
) {
    if (engine == NULL || key_material == NULL || key_len == 0U || key_len > TC_MAX_KEY_BYTES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    memcpy(engine->key_material, key_material, key_len);
    engine->key_len = key_len;
    engine->key_fingerprint = key_fingerprint;
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
    if (endpoints == NULL || count == 0U) {
        engine->endpoint_count = 0U;
        return TC_ENGINE_OK;
    }

    if (count > TC_MAX_ENDPOINTS) {
        count = TC_MAX_ENDPOINTS;
    }

    for (i = 0U; i < count; i++) {
        engine->endpoints[i].id = endpoints[i].id;
        engine->endpoints[i].ip = endpoints[i].ip;
        engine->endpoints[i].ipv6 = endpoints[i].ipv6;
        engine->endpoints[i].port = endpoints[i].port;
        engine->endpoints[i].peer_tag = endpoints[i].peer_tag;
        engine->endpoints[i].peer_tag_len = endpoints[i].peer_tag_len;
    }
    engine->endpoint_count = count;
    return TC_ENGINE_OK;
}

int tc_engine_poll_stats(tc_engine_t *engine, tc_stats_t *stats_out) {
    if (engine == NULL || stats_out == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    memcpy(stats_out, &engine->stats, sizeof(tc_stats_t));
    return TC_ENGINE_OK;
}

int tc_engine_stop(tc_engine_t *engine) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->state == TC_ENGINE_STATE_STOPPED) {
        return TC_ENGINE_OK;
    }
    tc_emit_state(engine, TC_ENGINE_STATE_STOPPED);
    return TC_ENGINE_OK;
}

void tc_engine_destroy(tc_engine_t *engine) {
    if (engine == NULL) {
        return;
    }

    memset(engine->key_material, 0, sizeof(engine->key_material));
    engine->key_len = 0U;
    engine->key_fingerprint = 0;
    free(engine);
}
