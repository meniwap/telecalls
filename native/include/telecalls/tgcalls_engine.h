#ifndef TELECALLS_TGCALLS_ENGINE_H
#define TELECALLS_TGCALLS_ENGINE_H

#include "telecalls/engine.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tc_rtc_server_t {
    const char *host;
    uint16_t port;
    const char *username;
    const char *password;
    int is_turn;
    int is_tcp;
} tc_rtc_server_t;

/*
 * Optional API used by the tgcalls backend to receive ICE server
 * configuration derived from phoneConnectionWebrtc entries.
 *
 * Return codes:
 *   0  success
 *  -1  generic error
 *  -2  invalid args
 *  -4  not running / unsupported
 */
int tc_engine_set_rtc_servers(
    tc_engine_t *engine,
    const tc_rtc_server_t *servers,
    size_t count
);

#ifdef __cplusplus
}
#endif

#endif
