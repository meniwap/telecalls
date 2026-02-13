#include "telecalls/engine.h"

#include "telecalls/codec_opus.h"
#include "telecalls/transport_udp.h"

#include <arpa/inet.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#define TC_MAX_SIGNALING_FRAMES 128
#define TC_MAX_SIGNALING_BYTES 16384
#define TC_MAX_ENDPOINTS 16
#define TC_MAX_KEY_BYTES 512
#define TC_MAX_IP_STR 64
#define TC_MAX_IPV6_STR 96
#define TC_MAX_PEER_TAG 32
#define TC_PCM_FRAME_SAMPLES 960
#define TC_PCM_QUEUE_FRAMES 64

#define TC_ENDPOINT_FLAG_RELAY (1U << 0)
#define TC_ENDPOINT_FLAG_P2P (1U << 1)

#define TC_UDP_KEEPALIVE_INTERVAL_MS 250U
#define TC_UDP_RECV_TIMEOUT_MS 40
#define TC_IDLE_SLEEP_MS 20
#define TC_MAGIC 0x54434D31U /* "TCM1" */

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
    int udp_valid;
    tc_udp_endpoint_t udp;
} tc_endpoint_runtime;

struct tc_engine {
    tc_engine_params_t params;
    int state;

    pthread_mutex_t mu;
    pthread_t worker;
    int worker_started;
    int worker_running;

    tc_udp_socket_t udp;

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

    int muted;
    int bitrate_hint_kbps;
    tc_opus_codec_t *codec;
    int16_t pcm_queue[TC_PCM_QUEUE_FRAMES][TC_PCM_FRAME_SAMPLES];
    int pcm_lengths[TC_PCM_QUEUE_FRAMES];
    size_t pcm_head;
    size_t pcm_tail;
    size_t pcm_size;

    uint32_t local_seq;
    uint32_t remote_seq;
    uint64_t remote_loss_count;

    uint64_t last_udp_send_ms;
    uint64_t last_signaling_emit_ms;
    uint64_t last_arrival_ms;
    float jitter_ms;

    uint64_t bytes_sent_window;
    uint64_t bytes_recv_window;
    uint64_t window_started_ms;

    tc_stats_t stats;
};

static uint64_t tc_now_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return ((uint64_t)tv.tv_sec * 1000ULL) + ((uint64_t)tv.tv_usec / 1000ULL);
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

static int tc_signaling_queue_push(tc_engine_t *engine, const uint8_t *data, size_t len) {
    tc_frame *frame = NULL;
    if (engine->signaling_size >= TC_MAX_SIGNALING_FRAMES) {
        return TC_ENGINE_ERR_QUEUE_FULL;
    }
    frame = &engine->signaling_out[engine->signaling_tail];
    frame->len = len;
    if (len > 0) {
        memcpy(frame->bytes, data, len);
    }
    engine->signaling_tail = (engine->signaling_tail + 1U) % TC_MAX_SIGNALING_FRAMES;
    engine->signaling_size += 1U;
    return TC_ENGINE_OK;
}

static int tc_signaling_queue_pop(tc_engine_t *engine, uint8_t *out, size_t out_cap) {
    tc_frame *frame = NULL;
    size_t out_len = 0;

    if (engine->signaling_size == 0U) {
        return 0;
    }

    frame = &engine->signaling_out[engine->signaling_head];
    out_len = frame->len;
    if (out_len > out_cap) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    if (out_len > 0U) {
        memcpy(out, frame->bytes, out_len);
        memset(frame->bytes, 0, frame->len);
    }
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

static void tc_write_u32_be(uint8_t *out, uint32_t value) {
    out[0] = (uint8_t)((value >> 24) & 0xFFU);
    out[1] = (uint8_t)((value >> 16) & 0xFFU);
    out[2] = (uint8_t)((value >> 8) & 0xFFU);
    out[3] = (uint8_t)(value & 0xFFU);
}

static void tc_write_u64_be(uint8_t *out, uint64_t value) {
    out[0] = (uint8_t)((value >> 56) & 0xFFU);
    out[1] = (uint8_t)((value >> 48) & 0xFFU);
    out[2] = (uint8_t)((value >> 40) & 0xFFU);
    out[3] = (uint8_t)((value >> 32) & 0xFFU);
    out[4] = (uint8_t)((value >> 24) & 0xFFU);
    out[5] = (uint8_t)((value >> 16) & 0xFFU);
    out[6] = (uint8_t)((value >> 8) & 0xFFU);
    out[7] = (uint8_t)(value & 0xFFU);
}

static uint32_t tc_read_u32_be(const uint8_t *in) {
    return ((uint32_t)in[0] << 24) | ((uint32_t)in[1] << 16) | ((uint32_t)in[2] << 8) | (uint32_t)in[3];
}

static uint64_t tc_read_u64_be(const uint8_t *in) {
    return ((uint64_t)in[0] << 56) | ((uint64_t)in[1] << 48) | ((uint64_t)in[2] << 40) |
           ((uint64_t)in[3] << 32) | ((uint64_t)in[4] << 24) | ((uint64_t)in[5] << 16) |
           ((uint64_t)in[6] << 8) | (uint64_t)in[7];
}

static int tc_build_udp_packet(tc_engine_t *engine, uint8_t kind, uint8_t *out, size_t out_cap) {
    uint64_t now_ms = tc_now_ms();
    uint32_t seq = 0;

    if (out == NULL || out_cap < 17U) {
        return -1;
    }

    pthread_mutex_lock(&engine->mu);
    engine->local_seq += 1U;
    seq = engine->local_seq;
    engine->last_udp_send_ms = now_ms;
    pthread_mutex_unlock(&engine->mu);

    tc_write_u32_be(out, TC_MAGIC);
    out[4] = kind;
    tc_write_u32_be(out + 5, seq);
    tc_write_u64_be(out + 9, now_ms);
    return 17;
}

static void tc_update_bitrate_locked(tc_engine_t *engine, uint64_t now_ms) {
    uint64_t elapsed = 0;
    uint64_t total_bits = 0;

    if (engine->window_started_ms == 0U) {
        engine->window_started_ms = now_ms;
        return;
    }

    elapsed = now_ms - engine->window_started_ms;
    if (elapsed < 1000U) {
        return;
    }

    total_bits = (engine->bytes_sent_window + engine->bytes_recv_window) * 8U;
    engine->stats.bitrate_kbps = (float)total_bits / (float)elapsed;
    engine->bytes_sent_window = 0U;
    engine->bytes_recv_window = 0U;
    engine->window_started_ms = now_ms;
}

static int tc_pick_active_endpoint_locked(tc_engine_t *engine, tc_endpoint_runtime *out) {
    size_t i = 0U;
    int found = 0;

    if (engine->endpoint_count == 0U || out == NULL) {
        return 0;
    }

    for (i = 0U; i < engine->endpoint_count; i++) {
        const tc_endpoint_runtime *ep = &engine->endpoints[i];
        if (!ep->udp_valid) {
            continue;
        }
        if ((ep->flags & TC_ENDPOINT_FLAG_RELAY) == 0U) {
            continue;
        }
        *out = *ep;
        engine->active_endpoint_index = (int)i;
        found = 1;
        break;
    }

    if (!found) {
        for (i = 0U; i < engine->endpoint_count; i++) {
            const tc_endpoint_runtime *ep = &engine->endpoints[i];
            if (!ep->udp_valid) {
                continue;
            }
            *out = *ep;
            engine->active_endpoint_index = (int)i;
            found = 1;
            break;
        }
    }

    return found;
}

static void tc_handle_udp_packet(tc_engine_t *engine, const uint8_t *packet, int len,
                                 const tc_udp_endpoint_t *source, const tc_endpoint_runtime *active) {
    uint64_t now_ms = tc_now_ms();

    if (len <= 0 || packet == NULL || source == NULL || active == NULL) {
        return;
    }

    pthread_mutex_lock(&engine->mu);
    engine->stats.packets_recv += 1U;
    engine->bytes_recv_window += (uint64_t)len;

    if (engine->last_arrival_ms > 0U) {
        uint64_t delta = now_ms - engine->last_arrival_ms;
        float variation = (float)delta - engine->jitter_ms;
        if (variation < 0.0f) {
            variation = -variation;
        }
        engine->jitter_ms += variation / 16.0f;
        engine->stats.jitter_ms = engine->jitter_ms;
    }
    engine->last_arrival_ms = now_ms;

    if (len >= 17 && tc_read_u32_be(packet) == TC_MAGIC) {
        uint8_t kind = packet[4];
        uint32_t seq = tc_read_u32_be(packet + 5);
        uint64_t sent_ms = tc_read_u64_be(packet + 9);

        if (kind == 'K') {
            uint8_t ack[17];
            (void)seq;
            tc_write_u32_be(ack, TC_MAGIC);
            ack[4] = 'A';
            tc_write_u32_be(ack + 5, seq);
            tc_write_u64_be(ack + 9, sent_ms);
            (void)tc_udp_send(&engine->udp, &active->udp, ack, sizeof(ack));
        } else if (kind == 'A') {
            if (now_ms > sent_ms) {
                engine->stats.rtt_ms = (float)(now_ms - sent_ms);
            }
        }

        if (engine->remote_seq > 0U && seq > engine->remote_seq + 1U) {
            engine->remote_loss_count += (uint64_t)(seq - engine->remote_seq - 1U);
        }
        if (seq > engine->remote_seq) {
            engine->remote_seq = seq;
        }
        if (engine->stats.packets_recv + engine->remote_loss_count > 0U) {
            engine->stats.loss =
                (float)engine->remote_loss_count /
                (float)(engine->stats.packets_recv + engine->remote_loss_count);
        }
    }

    tc_update_bitrate_locked(engine, now_ms);
    pthread_mutex_unlock(&engine->mu);
}

static void *tc_worker_main(void *arg) {
    tc_engine_t *engine = (tc_engine_t *)arg;

    while (1) {
        int should_run = 0;
        int state = TC_ENGINE_STATE_IDLE;
        tc_endpoint_runtime active;
        int has_active_endpoint = 0;
        uint64_t now_ms = tc_now_ms();

        memset(&active, 0, sizeof(active));

        pthread_mutex_lock(&engine->mu);
        should_run = engine->worker_running;
        state = engine->state;
        has_active_endpoint = tc_pick_active_endpoint_locked(engine, &active);
        if (state == TC_ENGINE_STATE_RUNNING) {
            tc_update_bitrate_locked(engine, now_ms);
        }
        pthread_mutex_unlock(&engine->mu);

        if (!should_run) {
            break;
        }

        if (state != TC_ENGINE_STATE_RUNNING || !has_active_endpoint || engine->udp.fd < 0) {
            usleep(TC_IDLE_SLEEP_MS * 1000U);
            continue;
        }

        if ((now_ms - engine->last_udp_send_ms) >= TC_UDP_KEEPALIVE_INTERVAL_MS) {
            uint8_t packet[17];
            int packet_len = tc_build_udp_packet(engine, 'K', packet, sizeof(packet));
            if (packet_len > 0) {
                int sent = tc_udp_send(&engine->udp, &active.udp, packet, (size_t)packet_len);
                if (sent > 0) {
                    pthread_mutex_lock(&engine->mu);
                    engine->stats.packets_sent += 1U;
                    engine->bytes_sent_window += (uint64_t)sent;
                    tc_update_bitrate_locked(engine, tc_now_ms());
                    pthread_mutex_unlock(&engine->mu);
                }
            }
        }

        {
            uint8_t in[1500];
            tc_udp_endpoint_t source;
            int rc = tc_udp_recv(&engine->udp, &source, in, sizeof(in), TC_UDP_RECV_TIMEOUT_MS);
            if (rc > 0) {
                tc_handle_udp_packet(engine, in, rc, &source, &active);
            }
        }
    }

    return NULL;
}

static void tc_reset_runtime_state(tc_engine_t *engine) {
    engine->local_seq = 0U;
    engine->remote_seq = 0U;
    engine->remote_loss_count = 0U;
    engine->last_udp_send_ms = 0U;
    engine->last_signaling_emit_ms = 0U;
    engine->last_arrival_ms = 0U;
    engine->jitter_ms = 0.0f;
    engine->bytes_sent_window = 0U;
    engine->bytes_recv_window = 0U;
    engine->window_started_ms = tc_now_ms();
    engine->pcm_head = 0U;
    engine->pcm_tail = 0U;
    engine->pcm_size = 0U;
    memset(engine->pcm_lengths, 0, sizeof(engine->pcm_lengths));
    memset(&engine->stats, 0, sizeof(engine->stats));
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
    engine->active_endpoint_index = -1;
    engine->bitrate_hint_kbps = 24;
    engine->udp.fd = -1;
    tc_reset_runtime_state(engine);

    if (pthread_mutex_init(&engine->mu, NULL) != 0) {
        free(engine);
        return NULL;
    }

    if (tc_udp_open(&engine->udp) != 0) {
        tc_emit_error(engine, TC_ENGINE_ERR, "failed to open UDP socket");
    }

    return engine;
}

int tc_engine_start(tc_engine_t *engine) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state == TC_ENGINE_STATE_RUNNING) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }

    tc_emit_state(engine, TC_ENGINE_STATE_STARTING);
    engine->worker_running = 1;
    if (!engine->worker_started) {
        if (pthread_create(&engine->worker, NULL, tc_worker_main, engine) != 0) {
            engine->worker_running = 0;
            tc_emit_state(engine, TC_ENGINE_STATE_FAILED);
            pthread_mutex_unlock(&engine->mu);
            return TC_ENGINE_ERR;
        }
        engine->worker_started = 1;
    }

    if (engine->codec == NULL) {
        engine->codec = tc_opus_codec_create(48000, 1, engine->bitrate_hint_kbps * 1000);
    }

    tc_emit_state(engine, TC_ENGINE_STATE_RUNNING);
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_push_signaling(tc_engine_t *engine, const uint8_t *data, size_t len) {
    if (engine == NULL || data == NULL || len == 0U || len > TC_MAX_SIGNALING_BYTES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->state != TC_ENGINE_STATE_RUNNING) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    pthread_mutex_lock(&engine->mu);
    engine->stats.packets_recv += 1U;
    engine->bytes_recv_window += len;

    if (len >= 18U && memcmp(data, "tc.sig.keepalive:", 16) == 0) {
        const char *ts_part = (const char *)(data + 16);
        const char *sep = strchr(ts_part, ':');
        if (sep != NULL) {
            unsigned long long sent_ms = strtoull(sep + 1, NULL, 10);
            unsigned long long now_ms = tc_now_ms();
            if (now_ms > sent_ms) {
                engine->stats.rtt_ms = (float)(now_ms - sent_ms);
            }
        }
    }

    tc_update_bitrate_locked(engine, tc_now_ms());
    pthread_mutex_unlock(&engine->mu);

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

    pthread_mutex_lock(&engine->mu);
    rc = tc_signaling_queue_pop(engine, out, out_cap);
    if (rc > 0) {
        engine->stats.packets_sent += 1U;
        engine->bytes_sent_window += (size_t)rc;
        tc_update_bitrate_locked(engine, tc_now_ms());
    }
    pthread_mutex_unlock(&engine->mu);
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

    pthread_mutex_lock(&engine->mu);
    memcpy(engine->key_material, key_material, key_len);
    engine->key_len = key_len;
    engine->key_fingerprint = key_fingerprint;

    if (engine->codec != NULL) {
        tc_opus_codec_destroy(engine->codec);
        engine->codec = NULL;
    }
    engine->codec = tc_opus_codec_create(48000, 1, engine->bitrate_hint_kbps * 1000);
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

    if (endpoints == NULL || count == 0U) {
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

        dst->udp_valid = (tc_udp_resolve_ipv4(dst->ip, dst->port, &dst->udp) == 0) ? 1 : 0;
    }

    engine->endpoint_count = count;
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_poll_stats(tc_engine_t *engine, tc_stats_t *stats_out) {
    if (engine == NULL || stats_out == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    memcpy(stats_out, &engine->stats, sizeof(tc_stats_t));
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_set_mute(tc_engine_t *engine, int muted) {
    if (engine == NULL) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    engine->muted = (muted != 0) ? 1 : 0;
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
    uint8_t packet[2048];
    int16_t decoded[TC_PCM_FRAME_SAMPLES];
    int encoded_len = 0;
    int decoded_samples = frame_samples;

    if (engine == NULL || pcm == NULL || frame_samples <= 0 || frame_samples > TC_PCM_FRAME_SAMPLES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    pthread_mutex_lock(&engine->mu);
    if (engine->state != TC_ENGINE_STATE_RUNNING) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    if (engine->muted) {
        pthread_mutex_unlock(&engine->mu);
        return TC_ENGINE_OK;
    }

    if (engine->codec != NULL) {
        encoded_len = tc_opus_encode(engine->codec, pcm, frame_samples, packet, (int)sizeof(packet));
        if (encoded_len > 0) {
            decoded_samples =
                tc_opus_decode(engine->codec, packet, encoded_len, decoded, frame_samples);
            if (decoded_samples > 0) {
                tc_pcm_queue_push(engine, decoded, decoded_samples);
                pthread_mutex_unlock(&engine->mu);
                return TC_ENGINE_OK;
            }
        }
    }

    tc_pcm_queue_push(engine, pcm, frame_samples);
    pthread_mutex_unlock(&engine->mu);
    return TC_ENGINE_OK;
}

int tc_engine_pull_audio_frame(tc_engine_t *engine, int16_t *pcm_out, int frame_samples) {
    int rc = 0;
    if (engine == NULL || pcm_out == NULL || frame_samples <= 0 || frame_samples > TC_PCM_FRAME_SAMPLES) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    pthread_mutex_lock(&engine->mu);
    if (engine->state != TC_ENGINE_STATE_RUNNING) {
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
    engine->worker_running = 0;
    pthread_mutex_unlock(&engine->mu);

    if (engine->worker_started) {
        pthread_join(engine->worker, NULL);
        engine->worker_started = 0;
    }

    pthread_mutex_lock(&engine->mu);
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

    (void)tc_udp_close(&engine->udp);
    memset(engine->key_material, 0, sizeof(engine->key_material));
    engine->key_len = 0U;
    engine->key_fingerprint = 0;

    pthread_mutex_destroy(&engine->mu);
    free(engine);
}
