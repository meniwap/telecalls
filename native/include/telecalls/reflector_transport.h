#ifndef TELECALLS_REFLECTOR_TRANSPORT_H
#define TELECALLS_REFLECTOR_TRANSPORT_H

#include "telecalls/transport_udp.h"

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tc_reflector_transport_t {
    tc_udp_socket_t sock;
    tc_udp_endpoint_t remote;
    int socket_open;
    int remote_ready;
} tc_reflector_transport_t;

enum tc_reflector_recv_status {
    TC_REFLECTOR_RECV_NONE = 0,
    TC_REFLECTOR_RECV_DATA = 1,
    TC_REFLECTOR_RECV_TIMEOUT = 2,
    TC_REFLECTOR_RECV_SOURCE_MISMATCH = 3,
    TC_REFLECTOR_RECV_ERROR = 4
};

void tc_reflector_transport_init(tc_reflector_transport_t *transport);
void tc_reflector_transport_reset(tc_reflector_transport_t *transport);
int tc_reflector_transport_set_remote_ipv4(
    tc_reflector_transport_t *transport,
    const char *ip,
    uint16_t port
);
int tc_reflector_transport_send(
    tc_reflector_transport_t *transport,
    const uint8_t *data,
    size_t len
);
int tc_reflector_transport_recv(
    tc_reflector_transport_t *transport,
    uint8_t *out,
    size_t out_cap,
    int timeout_ms,
    int *status_out
);

#ifdef __cplusplus
}
#endif

#endif
