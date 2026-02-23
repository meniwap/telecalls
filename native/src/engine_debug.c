#include "telecalls/engine_internal.h"

#include <string.h>

void tc_debug_stats_zero(tc_debug_stats_t *stats) {
    if (stats == NULL) {
        return;
    }
    memset(stats, 0, sizeof(*stats));
    stats->signaling_ctr_last_error_code = 0;
    stats->signaling_short_last_error_code = 0;
    stats->signaling_ctr_last_variant = 0U;
    stats->signaling_ctr_last_hash_mode = 0U;
    stats->signaling_best_failure_mode = 0U;
    stats->signaling_best_failure_code = 0;
    stats->signaling_decrypt_last_error_code = 0;
    stats->signaling_decrypt_last_error_stage = 0U;
    stats->signaling_proto_last_error_code = 0;
    stats->signaling_candidate_winner_index = -1;
}
