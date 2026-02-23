#include "telecalls/reflector_transport.h"

#include <string.h>

void tc_reflector_transport_init(tc_reflector_transport_t *transport) {
    if (transport == NULL) {
        return;
    }
    memset(transport, 0, sizeof(*transport));
    transport->sock.fd = -1;
}

void tc_reflector_transport_reset(tc_reflector_transport_t *transport) {
    if (transport == NULL) {
        return;
    }
    if (transport->socket_open) {
        (void)tc_udp_close(&transport->sock);
    } else {
        transport->sock.fd = -1;
    }
    memset(&transport->remote, 0, sizeof(transport->remote));
    transport->socket_open = 0;
    transport->remote_ready = 0;
}

int tc_reflector_transport_set_remote_ipv4(
    tc_reflector_transport_t *transport,
    const char *ip,
    uint16_t port
) {
    tc_udp_endpoint_t endpoint;

    if (transport == NULL || ip == NULL || port == 0U) {
        return -1;
    }

    if (!transport->socket_open) {
        transport->sock.fd = -1;
        if (tc_udp_open(&transport->sock) != 0) {
            transport->sock.fd = -1;
            return -1;
        }
        transport->socket_open = 1;
    }

    memset(&endpoint, 0, sizeof(endpoint));
    if (tc_udp_resolve_ipv4(ip, port, &endpoint) != 0) {
        return -1;
    }

    transport->remote = endpoint;
    transport->remote_ready = 1;
    return 0;
}

int tc_reflector_transport_send(
    tc_reflector_transport_t *transport,
    const uint8_t *data,
    size_t len
) {
    if (transport == NULL || data == NULL || len == 0U) {
        return -1;
    }
    if (!transport->socket_open || !transport->remote_ready) {
        return -1;
    }
    return tc_udp_send(&transport->sock, &transport->remote, data, len);
}

int tc_reflector_transport_recv(
    tc_reflector_transport_t *transport,
    uint8_t *out,
    size_t out_cap,
    int timeout_ms,
    int *status_out
) {
    tc_udp_endpoint_t src;
    int rc = 0;

    if (status_out != NULL) {
        *status_out = TC_REFLECTOR_RECV_NONE;
    }

    if (transport == NULL || out == NULL || out_cap == 0U) {
        if (status_out != NULL) {
            *status_out = TC_REFLECTOR_RECV_ERROR;
        }
        return -1;
    }
    if (!transport->socket_open || !transport->remote_ready) {
        return 0;
    }

    memset(&src, 0, sizeof(src));
    rc = tc_udp_recv(&transport->sock, &src, out, out_cap, timeout_ms);
    if (rc <= 0) {
        if (status_out != NULL) {
            *status_out = (rc < 0) ? TC_REFLECTOR_RECV_ERROR : TC_REFLECTOR_RECV_TIMEOUT;
        }
        return rc;
    }

    if (src.host_be != transport->remote.host_be || src.port_be != transport->remote.port_be) {
        if (status_out != NULL) {
            *status_out = TC_REFLECTOR_RECV_SOURCE_MISMATCH;
        }
        return 0;
    }
    if (status_out != NULL) {
        *status_out = TC_REFLECTOR_RECV_DATA;
    }
    return rc;
}
