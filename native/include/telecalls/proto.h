#ifndef TELECALLS_PROTO_H
#define TELECALLS_PROTO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TC_VOIP_PROTOCOL_VERSION 9
#define TC_VOIP_MIN_PROTOCOL_VERSION 3

#define TC_PKT_INIT 1
#define TC_PKT_INIT_ACK 2
#define TC_PKT_STREAM_STATE 3
#define TC_PKT_STREAM_DATA 4
#define TC_PKT_PING 6
#define TC_PKT_PONG 7
#define TC_PKT_NOP 14

#define TC_XPFLAG_HAS_EXTRA 1
#define TC_XPFLAG_HAS_RECV_TS 2

#define TC_EXTRA_TYPE_STREAM_FLAGS 1
#define TC_EXTRA_TYPE_NETWORK_CHANGED 4

typedef struct tc_proto_header {
    uint8_t type;
    uint32_t ack_id;
    uint32_t seq;
    uint32_t recent_mask;
    uint8_t flags;
} tc_proto_header_t;

typedef struct tc_proto_extra {
    uint8_t type;
    const uint8_t *data;
    size_t len;
} tc_proto_extra_t;

typedef struct tc_proto_rx_seq_state {
    uint32_t last_remote_seq;
    uint32_t recent_mask;
} tc_proto_rx_seq_state_t;

typedef struct tc_proto_rx_seq_update {
    uint32_t previous_last_seq;
    uint32_t current_last_seq;
    uint32_t current_recent_mask;
    uint32_t lost_count_increment;
    int advanced;
    int duplicate_or_old;
} tc_proto_rx_seq_update_t;

int tc_proto_encode_short(
    const tc_proto_header_t *header,
    const uint8_t *extras,
    size_t extras_len,
    const uint8_t *payload,
    size_t payload_len,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
);

int tc_proto_decode_short(
    const uint8_t *packet,
    size_t packet_len,
    tc_proto_header_t *header_out,
    const uint8_t **extras_out,
    size_t *extras_len_out,
    const uint8_t **payload_out,
    size_t *payload_len_out
);

int tc_proto_parse_extras(
    const uint8_t *extras,
    size_t extras_len,
    tc_proto_extra_t *out,
    size_t out_cap,
    size_t *out_count
);

void tc_proto_update_rx_seq(
    tc_proto_rx_seq_state_t *state,
    uint32_t incoming_seq,
    tc_proto_rx_seq_update_t *update_out
);

int tc_proto_header_acks(uint32_t ack_seq, uint32_t ack_mask, uint32_t candidate_seq);

#ifdef __cplusplus
}
#endif

#endif
