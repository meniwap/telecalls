#ifndef TELECALLS_TRANSPORT_UDP_H
#define TELECALLS_TRANSPORT_UDP_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tc_udp_endpoint_t {
    uint32_t host_be;
    uint16_t port_be;
} tc_udp_endpoint_t;

typedef struct tc_udp_socket_t {
    int fd;
} tc_udp_socket_t;

int tc_udp_open(tc_udp_socket_t *sock);
int tc_udp_close(tc_udp_socket_t *sock);
int tc_udp_send(
    tc_udp_socket_t *sock,
    const tc_udp_endpoint_t *endpoint,
    const uint8_t *data,
    size_t len
);
int tc_udp_recv(
    tc_udp_socket_t *sock,
    tc_udp_endpoint_t *endpoint,
    uint8_t *out,
    size_t out_cap,
    int timeout_ms
);
int tc_udp_resolve_ipv4(const char *ip, uint16_t port, tc_udp_endpoint_t *out);

#ifdef __cplusplus
}
#endif

#endif
