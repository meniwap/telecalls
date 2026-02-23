#include "telecalls/tgcalls_engine.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <new>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "tgcalls/Instance.h"
#include "tgcalls/InstanceImpl.h"

constexpr uint32_t kEndpointFlagRelay = 1U << 0;
constexpr uint32_t kEndpointFlagTurn = 1U << 3;
constexpr size_t kAuthKeySize = tgcalls::EncryptionKey::kSize;

struct EndpointConfig {
    int64_t id = 0;
    std::string ip;
    std::string ipv6;
    uint16_t port = 0;
    uint32_t flags = 0;
    uint32_t priority = 100;
};

struct RtcServerConfig {
    std::string host;
    uint16_t port = 0;
    std::string username;
    std::string password;
    bool is_turn = false;
    bool is_tcp = false;
};

struct tc_engine {
    std::mutex mu;
    tc_engine_params_t params{};

    int running = 0;
    int state = TC_ENGINE_STATE_IDLE;
    int muted = 0;
    int network_type = TC_NETWORK_TYPE_UNKNOWN;
    int bitrate_hint_kbps = 24;

    uint32_t protocol_version = 9;
    uint32_t min_protocol_version = 3;
    int32_t protocol_min_layer = 65;
    int32_t protocol_max_layer = 92;

    int keys_ready = 0;
    int endpoints_ready = 0;
    int rtc_servers_ready = 0;

    std::array<uint8_t, kAuthKeySize> auth_key{};
    bool is_outgoing = false;

    std::vector<EndpointConfig> endpoints;
    std::vector<RtcServerConfig> rtc_servers;

    std::unique_ptr<tgcalls::Instance> instance;
    std::deque<std::vector<uint8_t>> outgoing_signaling;

    int64_t endpoint_id = 0;
    int signal_bars = 0;
    float last_audio_level = 0.0f;

    uint64_t packets_sent = 0;
    uint64_t packets_recv = 0;
    uint64_t media_packets_sent = 0;
    uint64_t media_packets_recv = 0;
    uint64_t signaling_packets_sent = 0;
    uint64_t signaling_packets_recv = 0;
    uint64_t bytes_sent = 0;
    uint64_t bytes_recv = 0;

    float bitrate_kbps = 0.0F;
    std::chrono::steady_clock::time_point last_bitrate_ts{};
    uint64_t last_bitrate_bytes_total = 0;
};

static int tc_map_state(tgcalls::State state) {
    switch (state) {
        case tgcalls::State::WaitInit:
            return TC_ENGINE_STATE_WAIT_INIT;
        case tgcalls::State::WaitInitAck:
            return TC_ENGINE_STATE_WAIT_INIT_ACK;
        case tgcalls::State::Established:
            return TC_ENGINE_STATE_ESTABLISHED;
        case tgcalls::State::Failed:
            return TC_ENGINE_STATE_FAILED;
        case tgcalls::State::Reconnecting:
            return TC_ENGINE_STATE_RUNNING;
        default:
            return TC_ENGINE_STATE_RUNNING;
    }
}

static tgcalls::NetworkType tc_map_network_type(int network_type) {
    switch (network_type) {
        case TC_NETWORK_TYPE_WIFI:
            return tgcalls::NetworkType::WiFi;
        case TC_NETWORK_TYPE_ETHERNET:
            return tgcalls::NetworkType::Ethernet;
        case TC_NETWORK_TYPE_CELLULAR:
            return tgcalls::NetworkType::Lte;
        default:
            return tgcalls::NetworkType::Unknown;
    }
}

static void tc_emit_state_locked(tc_engine_t *engine, int state) {
    if (engine == nullptr) {
        return;
    }
    if (engine->state == state) {
        return;
    }
    engine->state = state;
    if (engine->params.on_state != nullptr) {
        engine->params.on_state(engine->params.user_data, state);
    }
}

static void tc_emit_error_locked(tc_engine_t *engine, int code, const char *message) {
    if (engine == nullptr) {
        return;
    }
    if (engine->params.on_error != nullptr) {
        engine->params.on_error(engine->params.user_data, code, message);
    }
}

static void tc_emit_stats_locked(tc_engine_t *engine) {
    if (engine == nullptr || engine->params.on_stats == nullptr) {
        return;
    }
    tc_stats_t stats{};
    stats.rtt_ms = 0.0f;
    stats.loss = 0.0f;
    stats.bitrate_kbps = std::max(engine->bitrate_kbps, static_cast<float>(engine->bitrate_hint_kbps));
    stats.jitter_ms = 0.0f;
    stats.packets_sent = engine->packets_sent;
    stats.packets_recv = engine->packets_recv;
    stats.media_packets_sent = engine->media_packets_sent;
    stats.media_packets_recv = engine->media_packets_recv;
    stats.signaling_packets_sent = engine->signaling_packets_sent;
    stats.signaling_packets_recv = engine->signaling_packets_recv;
    stats.send_loss = 0.0f;
    stats.recv_loss = 0.0f;
    stats.endpoint_id = engine->endpoint_id;
    engine->params.on_stats(engine->params.user_data, &stats);
}

static void tc_update_traffic_locked(tc_engine_t *engine) {
    if (engine == nullptr || engine->instance == nullptr) {
        return;
    }

    const tgcalls::TrafficStats traffic = engine->instance->getTrafficStats();
    const uint64_t bytes_sent = traffic.bytesSentWifi + traffic.bytesSentMobile;
    const uint64_t bytes_recv = traffic.bytesReceivedWifi + traffic.bytesReceivedMobile;
    engine->bytes_sent = std::max(engine->bytes_sent, bytes_sent);
    engine->bytes_recv = std::max(engine->bytes_recv, bytes_recv);

    const uint64_t est_media_packets_sent = engine->bytes_sent / 1200U;
    const uint64_t est_media_packets_recv = engine->bytes_recv / 1200U;
    engine->media_packets_sent = std::max(engine->media_packets_sent, est_media_packets_sent);
    engine->media_packets_recv = std::max(engine->media_packets_recv, est_media_packets_recv);
    engine->packets_sent = std::max(
        engine->packets_sent,
        engine->media_packets_sent + engine->signaling_packets_sent);
    engine->packets_recv = std::max(
        engine->packets_recv,
        engine->media_packets_recv + engine->signaling_packets_recv);

    const auto now = std::chrono::steady_clock::now();
    const uint64_t total_bytes = engine->bytes_sent + engine->bytes_recv;
    if (engine->last_bitrate_ts.time_since_epoch().count() != 0 &&
        total_bytes >= engine->last_bitrate_bytes_total) {
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - engine->last_bitrate_ts);
        if (elapsed.count() > 0) {
            const uint64_t delta_bytes = total_bytes - engine->last_bitrate_bytes_total;
            const double bits = static_cast<double>(delta_bytes) * 8.0;
            const double kbps = bits / static_cast<double>(elapsed.count());
            engine->bitrate_kbps = static_cast<float>(std::max(0.0, kbps));
        }
    }
    engine->last_bitrate_ts = now;
    engine->last_bitrate_bytes_total = total_bytes;
}

static std::vector<tgcalls::RtcServer> tc_build_rtc_servers_locked(const tc_engine_t *engine) {
    std::vector<tgcalls::RtcServer> out;
    std::set<std::tuple<std::string, uint16_t, std::string, std::string, bool>> dedupe;

    for (const RtcServerConfig &item : engine->rtc_servers) {
        if (item.host.empty() || item.port == 0) {
            continue;
        }
        const auto key = std::make_tuple(
            item.host,
            item.port,
            item.username,
            item.password,
            item.is_turn);
        if (!dedupe.insert(key).second) {
            continue;
        }
        tgcalls::RtcServer server{};
        server.host = item.host;
        server.port = item.port;
        server.login = item.username;
        server.password = item.password;
        server.isTurn = item.is_turn;
        out.push_back(std::move(server));
    }

    for (const EndpointConfig &endpoint : engine->endpoints) {
        std::string host = endpoint.ip;
        if (host.empty()) {
            host = endpoint.ipv6;
        }
        if (host.empty() || endpoint.port == 0) {
            continue;
        }
        const bool is_turn = (endpoint.flags & (kEndpointFlagRelay | kEndpointFlagTurn)) != 0U;
        const auto key = std::make_tuple(
            host,
            endpoint.port,
            std::string(),
            std::string(),
            is_turn);
        if (!dedupe.insert(key).second) {
            continue;
        }
        tgcalls::RtcServer server{};
        server.host = std::move(host);
        server.port = endpoint.port;
        server.isTurn = is_turn;
        out.push_back(std::move(server));
    }

    return out;
}

static void tc_try_start_instance_locked(tc_engine_t *engine) {
    if (engine == nullptr || engine->running == 0 || engine->instance != nullptr) {
        return;
    }
    if (engine->keys_ready == 0) {
        return;
    }
    if (engine->endpoints_ready == 0 && engine->rtc_servers_ready == 0) {
        return;
    }

    static std::once_flag register_once;
    std::call_once(register_once, []() {
        (void)tgcalls::Register<tgcalls::InstanceImpl>();
    });

    auto key_value = std::make_shared<std::array<uint8_t, kAuthKeySize>>(engine->auth_key);
    tgcalls::Descriptor descriptor{
        tgcalls::Config{},
        tgcalls::PersistentState{},
        {},
        nullptr,
        {},
        tc_map_network_type(engine->network_type),
        tgcalls::EncryptionKey(std::move(key_value), engine->is_outgoing),
    };
    descriptor.config.initializationTimeout = 5.0;
    descriptor.config.receiveTimeout = 5.0;
    descriptor.config.dataSaving = tgcalls::DataSaving::Never;
    descriptor.config.enableP2P = true;
    descriptor.config.allowTCP = true;
    descriptor.config.enableStunMarking = true;
    descriptor.config.enableAEC = true;
    descriptor.config.enableNS = true;
    descriptor.config.enableAGC = true;
    descriptor.config.enableVolumeControl = true;
    descriptor.config.maxApiLayer = std::max(0, engine->protocol_max_layer);
    descriptor.config.protocolVersion = tgcalls::ProtocolVersion::V1;
    descriptor.rtcServers = tc_build_rtc_servers_locked(engine);

    descriptor.stateUpdated = [engine](tgcalls::State state) {
        std::lock_guard<std::mutex> lock(engine->mu);
        tc_emit_state_locked(engine, tc_map_state(state));
    };
    descriptor.signalBarsUpdated = [engine](int signal_bars) {
        std::lock_guard<std::mutex> lock(engine->mu);
        engine->signal_bars = signal_bars;
    };
    descriptor.audioLevelUpdated = [engine](float level) {
        std::lock_guard<std::mutex> lock(engine->mu);
        engine->last_audio_level = level;
        if (level > 0.001f) {
            engine->media_packets_recv += 1U;
            engine->packets_recv += 1U;
        }
    };
    descriptor.remoteBatteryLevelIsLowUpdated = [](bool) {};
    descriptor.remoteMediaStateUpdated = [](tgcalls::AudioState, tgcalls::VideoState) {};
    descriptor.remotePrefferedAspectRatioUpdated = [](float) {};
    descriptor.signalingDataEmitted = [engine](const std::vector<uint8_t> &data) {
        if (data.empty()) {
            return;
        }
        std::lock_guard<std::mutex> lock(engine->mu);
        engine->outgoing_signaling.push_back(data);
        if (engine->params.on_signaling != nullptr) {
            engine->params.on_signaling(
                engine->params.user_data,
                data.data(),
                data.size());
        }
    };

    const char *version = "3.0.0";
    if (engine->protocol_version < 9U) {
        version = "2.7.7";
    }
    engine->instance = tgcalls::Meta::Create(version, std::move(descriptor));
    if (engine->instance == nullptr) {
        tc_emit_error_locked(engine, TC_ENGINE_ERR, "tgcalls Meta::Create returned null");
        tc_emit_state_locked(engine, TC_ENGINE_STATE_FAILED);
        return;
    }

    engine->instance->setNetworkType(tc_map_network_type(engine->network_type));
    engine->instance->setMuteMicrophone(engine->muted != 0);
    tc_emit_state_locked(engine, TC_ENGINE_STATE_WAIT_INIT);
}

extern "C" {

tc_engine_t *tc_engine_create(const tc_engine_params_t *params) {
    if (params == nullptr) {
        return nullptr;
    }
    tc_engine_t *engine = new (std::nothrow) tc_engine_t();
    if (engine == nullptr) {
        return nullptr;
    }
    engine->params = *params;
    engine->state = TC_ENGINE_STATE_IDLE;
    return engine;
}

int tc_engine_start(tc_engine_t *engine) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->running = 1;
    tc_emit_state_locked(engine, TC_ENGINE_STATE_WAIT_INIT);
    tc_try_start_instance_locked(engine);
    tc_emit_stats_locked(engine);
    return TC_ENGINE_OK;
}

int tc_engine_push_signaling(tc_engine_t *engine, const uint8_t *data, size_t len) {
    if (engine == nullptr || data == nullptr || len == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::vector<uint8_t> payload(data, data + len);
    std::lock_guard<std::mutex> lock(engine->mu);
    if (engine->running == 0) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    if (engine->instance == nullptr) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }

    try {
        engine->instance->receiveSignalingData(payload);
    } catch (...) {
        tc_emit_error_locked(engine, TC_ENGINE_ERR, "tgcalls receiveSignalingData threw");
        return TC_ENGINE_ERR;
    }

    engine->packets_recv += 1U;
    engine->signaling_packets_recv += 1U;
    tc_update_traffic_locked(engine);
    tc_emit_stats_locked(engine);
    return TC_ENGINE_OK;
}

int tc_engine_pull_signaling(tc_engine_t *engine, uint8_t *out, size_t out_cap) {
    if (engine == nullptr || out == nullptr || out_cap == 0U) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    if (engine->running == 0) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    if (engine->outgoing_signaling.empty()) {
        return 0;
    }

    const std::vector<uint8_t> payload = std::move(engine->outgoing_signaling.front());
    engine->outgoing_signaling.pop_front();
    if (payload.size() > out_cap) {
        tc_emit_error_locked(engine, TC_ENGINE_ERR, "tgcalls signaling buffer too small");
        return TC_ENGINE_ERR;
    }
    std::memcpy(out, payload.data(), payload.size());

    engine->packets_sent += 1U;
    engine->signaling_packets_sent += 1U;
    tc_update_traffic_locked(engine);
    tc_emit_stats_locked(engine);
    return static_cast<int>(payload.size());
}

int tc_engine_set_keys(
    tc_engine_t *engine,
    const uint8_t *key_material,
    size_t key_len,
    int64_t key_fingerprint,
    int is_outgoing) {
    if (engine == nullptr || key_material == nullptr || key_len < kAuthKeySize) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    (void)key_fingerprint;
    std::lock_guard<std::mutex> lock(engine->mu);
    std::memcpy(engine->auth_key.data(), key_material, kAuthKeySize);
    engine->is_outgoing = (is_outgoing != 0);
    engine->keys_ready = 1;
    tc_try_start_instance_locked(engine);
    return TC_ENGINE_OK;
}

int tc_engine_set_remote_endpoints(
    tc_engine_t *engine,
    const tc_endpoint_t *endpoints,
    size_t count) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->endpoints.clear();
    engine->endpoint_id = 0;
    if (endpoints != nullptr && count > 0U) {
        engine->endpoints.reserve(count);
        for (size_t i = 0; i < count; ++i) {
            const tc_endpoint_t &item = endpoints[i];
            EndpointConfig parsed{};
            parsed.id = item.id;
            parsed.ip = (item.ip != nullptr) ? std::string(item.ip) : std::string();
            parsed.ipv6 = (item.ipv6 != nullptr) ? std::string(item.ipv6) : std::string();
            parsed.port = item.port;
            parsed.flags = item.flags;
            parsed.priority = item.priority;
            engine->endpoints.push_back(std::move(parsed));
        }
        engine->endpoint_id = engine->endpoints.front().id;
    }
    engine->endpoints_ready = engine->endpoints.empty() ? 0 : 1;
    tc_try_start_instance_locked(engine);
    return TC_ENGINE_OK;
}

int tc_engine_set_rtc_servers(
    tc_engine_t *engine,
    const tc_rtc_server_t *servers,
    size_t count) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->rtc_servers.clear();
    if (servers != nullptr && count > 0U) {
        engine->rtc_servers.reserve(count);
        for (size_t i = 0; i < count; ++i) {
            const tc_rtc_server_t &item = servers[i];
            if (item.host == nullptr || item.port == 0) {
                continue;
            }
            RtcServerConfig parsed{};
            parsed.host = std::string(item.host);
            parsed.port = item.port;
            parsed.username = (item.username != nullptr) ? std::string(item.username) : std::string();
            parsed.password = (item.password != nullptr) ? std::string(item.password) : std::string();
            parsed.is_turn = item.is_turn != 0;
            parsed.is_tcp = item.is_tcp != 0;
            engine->rtc_servers.push_back(std::move(parsed));
        }
    }
    engine->rtc_servers_ready = engine->rtc_servers.empty() ? 0 : 1;
    tc_try_start_instance_locked(engine);
    return TC_ENGINE_OK;
}

int tc_engine_set_protocol_config(tc_engine_t *engine, const tc_protocol_config_t *config) {
    if (engine == nullptr || config == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->protocol_version = config->protocol_version;
    engine->min_protocol_version = config->min_protocol_version;
    engine->protocol_min_layer = config->min_layer;
    engine->protocol_max_layer = config->max_layer;
    return TC_ENGINE_OK;
}

int tc_engine_set_network_type(tc_engine_t *engine, int network_type) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->network_type = network_type;
    if (engine->instance != nullptr) {
        engine->instance->setNetworkType(tc_map_network_type(network_type));
    }
    return TC_ENGINE_OK;
}

int tc_engine_poll_stats(tc_engine_t *engine, tc_stats_t *stats_out) {
    if (engine == nullptr || stats_out == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    tc_update_traffic_locked(engine);
    stats_out->rtt_ms = 0.0f;
    stats_out->loss = 0.0f;
    stats_out->bitrate_kbps = std::max(engine->bitrate_kbps, static_cast<float>(engine->bitrate_hint_kbps));
    stats_out->jitter_ms = 0.0f;
    stats_out->packets_sent = engine->packets_sent;
    stats_out->packets_recv = engine->packets_recv;
    stats_out->media_packets_sent = engine->media_packets_sent;
    stats_out->media_packets_recv = engine->media_packets_recv;
    stats_out->signaling_packets_sent = engine->signaling_packets_sent;
    stats_out->signaling_packets_recv = engine->signaling_packets_recv;
    stats_out->send_loss = 0.0f;
    stats_out->recv_loss = 0.0f;
    stats_out->endpoint_id = engine->endpoint_id;
    return TC_ENGINE_OK;
}

int tc_engine_set_mute(tc_engine_t *engine, int muted) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->muted = muted ? 1 : 0;
    if (engine->instance != nullptr) {
        engine->instance->setMuteMicrophone(engine->muted != 0);
    }
    return TC_ENGINE_OK;
}

int tc_engine_set_bitrate_hint(tc_engine_t *engine, int bitrate_kbps) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    std::lock_guard<std::mutex> lock(engine->mu);
    engine->bitrate_hint_kbps = std::max(8, bitrate_kbps);
    return TC_ENGINE_OK;
}

int tc_engine_push_audio_frame(
    tc_engine_t *engine,
    const int16_t *pcm,
    int frame_samples) {
    if (engine == nullptr || pcm == nullptr || frame_samples <= 0) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->running == 0) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    // Real tgcalls backend owns the audio device pipeline natively.
    return TC_ENGINE_OK;
}

int tc_engine_pull_audio_frame(
    tc_engine_t *engine,
    int16_t *pcm_out,
    int frame_samples) {
    if (engine == nullptr || pcm_out == nullptr || frame_samples <= 0) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }
    if (engine->running == 0) {
        return TC_ENGINE_ERR_NOT_RUNNING;
    }
    (void)pcm_out;
    (void)frame_samples;
    return 0;
}

int tc_engine_stop(tc_engine_t *engine) {
    if (engine == nullptr) {
        return TC_ENGINE_ERR_INVALID_ARG;
    }

    std::unique_ptr<tgcalls::Instance> instance;
    {
        std::lock_guard<std::mutex> lock(engine->mu);
        engine->running = 0;
        instance = std::move(engine->instance);
    }

    if (instance != nullptr) {
        std::mutex wait_mu;
        std::condition_variable wait_cv;
        bool done = false;
        instance->stop([&](tgcalls::FinalState final_state) {
            std::lock_guard<std::mutex> wait_lock(wait_mu);
            (void)final_state;
            done = true;
            wait_cv.notify_one();
        });
        std::unique_lock<std::mutex> wait_lock(wait_mu);
        (void)wait_cv.wait_for(wait_lock, std::chrono::seconds(3), [&]() { return done; });
    }

    {
        std::lock_guard<std::mutex> lock(engine->mu);
        tc_update_traffic_locked(engine);
        tc_emit_state_locked(engine, TC_ENGINE_STATE_STOPPED);
        tc_emit_stats_locked(engine);
    }
    return TC_ENGINE_OK;
}

void tc_engine_destroy(tc_engine_t *engine) {
    if (engine == nullptr) {
        return;
    }
    (void)tc_engine_stop(engine);
    delete engine;
}

}  // extern "C"
