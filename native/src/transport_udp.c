#include "telecalls/transport_udp.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

int tc_udp_open(tc_udp_socket_t *sock) {
    int fd = -1;
    struct sockaddr_in local_addr;

    if (sock == NULL) {
        return -1;
    }

    fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) {
        return -1;
    }

    memset(&local_addr, 0, sizeof(local_addr));
    local_addr.sin_family = AF_INET;
    local_addr.sin_addr.s_addr = htonl(INADDR_ANY);
    local_addr.sin_port = htons(0);

    if (bind(fd, (const struct sockaddr *)&local_addr, sizeof(local_addr)) != 0) {
        close(fd);
        return -1;
    }

    if (fcntl(fd, F_SETFL, O_NONBLOCK) != 0) {
        close(fd);
        return -1;
    }

    sock->fd = fd;
    return 0;
}

int tc_udp_close(tc_udp_socket_t *sock) {
    if (sock == NULL) {
        return -1;
    }
    if (sock->fd >= 0) {
        close(sock->fd);
        sock->fd = -1;
    }
    return 0;
}

int tc_udp_send(
    tc_udp_socket_t *sock,
    const tc_udp_endpoint_t *endpoint,
    const uint8_t *data,
    size_t len
) {
    struct sockaddr_in addr;
    ssize_t rc = 0;

    if (sock == NULL || endpoint == NULL || data == NULL || len == 0 || sock->fd < 0) {
        return -1;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = endpoint->host_be;
    addr.sin_port = endpoint->port_be;

    rc = sendto(sock->fd, data, len, 0, (const struct sockaddr *)&addr, sizeof(addr));
    if (rc < 0) {
        return -1;
    }
    return (int)rc;
}

int tc_udp_recv(
    tc_udp_socket_t *sock,
    tc_udp_endpoint_t *endpoint,
    uint8_t *out,
    size_t out_cap,
    int timeout_ms
) {
    fd_set rfds;
    struct timeval tv;
    int ready = 0;
    struct sockaddr_in addr;
    socklen_t addr_len = sizeof(addr);
    ssize_t rc = 0;

    if (sock == NULL || endpoint == NULL || out == NULL || out_cap == 0 || sock->fd < 0) {
        return -1;
    }

    FD_ZERO(&rfds);
    FD_SET(sock->fd, &rfds);

    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;

    ready = select(sock->fd + 1, &rfds, NULL, NULL, &tv);
    if (ready <= 0) {
        return 0;
    }

    memset(&addr, 0, sizeof(addr));
    rc = recvfrom(sock->fd, out, out_cap, 0, (struct sockaddr *)&addr, &addr_len);
    if (rc < 0) {
        if (errno == EWOULDBLOCK || errno == EAGAIN) {
            return 0;
        }
        return -1;
    }

    endpoint->host_be = addr.sin_addr.s_addr;
    endpoint->port_be = addr.sin_port;
    return (int)rc;
}

int tc_udp_resolve_ipv4(const char *ip, uint16_t port, tc_udp_endpoint_t *out) {
    struct in_addr addr;
    if (ip == NULL || out == NULL) {
        return -1;
    }
    if (inet_pton(AF_INET, ip, &addr) != 1) {
        return -1;
    }
    out->host_be = addr.s_addr;
    out->port_be = htons(port);
    return 0;
}
