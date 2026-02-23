#include "telecalls/proto.h"

#include <string.h>

#define TC_SHORT_HEADER_LEN 14U

static void tc_write_u32_le(uint8_t *out, uint32_t value) {
    out[0] = (uint8_t)(value & 0xFFU);
    out[1] = (uint8_t)((value >> 8) & 0xFFU);
    out[2] = (uint8_t)((value >> 16) & 0xFFU);
    out[3] = (uint8_t)((value >> 24) & 0xFFU);
}

static uint32_t tc_read_u32_le(const uint8_t *in) {
    return ((uint32_t)in[0]) |
           ((uint32_t)in[1] << 8) |
           ((uint32_t)in[2] << 16) |
           ((uint32_t)in[3] << 24);
}

int tc_proto_encode_short(
    const tc_proto_header_t *header,
    const uint8_t *extras,
    size_t extras_len,
    const uint8_t *payload,
    size_t payload_len,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
) {
    size_t total = 0U;
    uint8_t flags = 0U;

    if (header == NULL || out == NULL || out_len == NULL) {
        return -1;
    }
    if (extras_len > 0U && extras == NULL) {
        return -1;
    }
    if (payload_len > 0U && payload == NULL) {
        return -1;
    }

    flags = header->flags;
    if (extras_len > 0U) {
        flags = (uint8_t)(flags | TC_XPFLAG_HAS_EXTRA);
    }

    total = TC_SHORT_HEADER_LEN + extras_len + payload_len;
    if (total > out_cap) {
        return -1;
    }

    out[0] = header->type;
    tc_write_u32_le(out + 1U, header->ack_id);
    tc_write_u32_le(out + 5U, header->seq);
    tc_write_u32_le(out + 9U, header->recent_mask);
    out[13] = flags;

    if (extras_len > 0U) {
        memcpy(out + TC_SHORT_HEADER_LEN, extras, extras_len);
    }
    if (payload_len > 0U) {
        memcpy(out + TC_SHORT_HEADER_LEN + extras_len, payload, payload_len);
    }

    *out_len = total;
    return 0;
}

int tc_proto_decode_short(
    const uint8_t *packet,
    size_t packet_len,
    tc_proto_header_t *header_out,
    const uint8_t **extras_out,
    size_t *extras_len_out,
    const uint8_t **payload_out,
    size_t *payload_len_out
) {
    size_t cursor = TC_SHORT_HEADER_LEN;
    size_t extras_len = 0U;

    if (packet == NULL || packet_len < TC_SHORT_HEADER_LEN || header_out == NULL ||
        extras_out == NULL || extras_len_out == NULL || payload_out == NULL ||
        payload_len_out == NULL) {
        return -1;
    }

    header_out->type = packet[0];
    header_out->ack_id = tc_read_u32_le(packet + 1U);
    header_out->seq = tc_read_u32_le(packet + 5U);
    header_out->recent_mask = tc_read_u32_le(packet + 9U);
    header_out->flags = packet[13];

    if ((header_out->flags & TC_XPFLAG_HAS_EXTRA) != 0U) {
        uint8_t count = 0U;
        size_t i = 0U;
        if (cursor >= packet_len) {
            return -1;
        }
        count = packet[cursor++];
        for (i = 0U; i < count; i++) {
            uint8_t item_len = 0U;
            if (cursor >= packet_len) {
                return -1;
            }
            item_len = packet[cursor++];
            if (item_len == 0U) {
                return -1;
            }
            if ((size_t)item_len > (packet_len - cursor)) {
                return -1;
            }
            cursor += (size_t)item_len;
        }
        extras_len = cursor - TC_SHORT_HEADER_LEN;
    }

    *extras_out = (extras_len > 0U) ? (packet + TC_SHORT_HEADER_LEN) : NULL;
    *extras_len_out = extras_len;
    *payload_out = packet + cursor;
    *payload_len_out = packet_len - cursor;
    return 0;
}

int tc_proto_parse_extras(
    const uint8_t *extras,
    size_t extras_len,
    tc_proto_extra_t *out,
    size_t out_cap,
    size_t *out_count
) {
    uint8_t count = 0U;
    size_t cursor = 0U;
    size_t produced = 0U;
    size_t i = 0U;

    if (out_count == NULL) {
        return -1;
    }
    *out_count = 0U;

    if (extras == NULL || extras_len == 0U) {
        return 0;
    }

    if (cursor >= extras_len) {
        return -1;
    }
    count = extras[cursor++];

    for (i = 0U; i < count; i++) {
        uint8_t item_len = 0U;
        uint8_t type = 0U;
        const uint8_t *data = NULL;
        size_t data_len = 0U;

        if (cursor >= extras_len) {
            return -1;
        }
        item_len = extras[cursor++];
        if (item_len == 0U) {
            return -1;
        }
        if ((size_t)item_len > (extras_len - cursor)) {
            return -1;
        }

        type = extras[cursor++];
        data = extras + cursor;
        data_len = (size_t)item_len - 1U;
        cursor += data_len;

        if (out != NULL && produced < out_cap) {
            out[produced].type = type;
            out[produced].data = data;
            out[produced].len = data_len;
        }
        produced += 1U;
    }

    *out_count = produced;
    return 0;
}

void tc_proto_update_rx_seq(
    tc_proto_rx_seq_state_t *state,
    uint32_t incoming_seq,
    tc_proto_rx_seq_update_t *update_out
) {
    tc_proto_rx_seq_update_t local_update;

    memset(&local_update, 0, sizeof(local_update));
    if (state == NULL) {
        if (update_out != NULL) {
            *update_out = local_update;
        }
        return;
    }

    local_update.previous_last_seq = state->last_remote_seq;
    local_update.current_last_seq = state->last_remote_seq;
    local_update.current_recent_mask = state->recent_mask;

    if (state->last_remote_seq == 0U && state->recent_mask == 0U) {
        state->last_remote_seq = incoming_seq;
        state->recent_mask = 0U;
        local_update.current_last_seq = incoming_seq;
        local_update.current_recent_mask = 0U;
        local_update.advanced = 1;
        if (update_out != NULL) {
            *update_out = local_update;
        }
        return;
    }

    if (incoming_seq > state->last_remote_seq) {
        uint32_t delta = incoming_seq - state->last_remote_seq;
        uint32_t shifted = 0U;

        if (delta >= 32U) {
            shifted = 1U;
        } else {
            shifted = (state->recent_mask << delta) | (1U << (delta - 1U));
        }

        if (delta > 1U) {
            local_update.lost_count_increment = delta - 1U;
        }

        state->last_remote_seq = incoming_seq;
        state->recent_mask = shifted;
        local_update.current_last_seq = state->last_remote_seq;
        local_update.current_recent_mask = state->recent_mask;
        local_update.advanced = 1;
    } else {
        uint32_t back = state->last_remote_seq - incoming_seq;
        if (back == 0U) {
            local_update.duplicate_or_old = 1;
        } else if (back <= 32U) {
            state->recent_mask |= (1U << (back - 1U));
            local_update.current_recent_mask = state->recent_mask;
        } else {
            local_update.duplicate_or_old = 1;
        }
    }

    if (update_out != NULL) {
        *update_out = local_update;
    }
}

int tc_proto_header_acks(uint32_t ack_seq, uint32_t ack_mask, uint32_t candidate_seq) {
    uint32_t delta = 0U;

    if (candidate_seq == ack_seq) {
        return 1;
    }
    if (candidate_seq > ack_seq) {
        return 0;
    }

    delta = ack_seq - candidate_seq;
    if (delta == 0U || delta > 32U) {
        return 0;
    }
    return ((ack_mask >> (delta - 1U)) & 1U) ? 1 : 0;
}
