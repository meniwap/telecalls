#ifndef TELECALLS_MEDIA_PIPELINE_H
#define TELECALLS_MEDIA_PIPELINE_H

#include "telecalls/codec_opus.h"

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int tc_media_encode_frame(
    tc_opus_codec_t *codec,
    const int16_t *pcm,
    int frame_samples,
    uint8_t *encoded_out,
    int encoded_cap
);

int tc_media_decode_frame(
    tc_opus_codec_t *codec,
    const uint8_t *payload,
    int payload_len,
    int16_t *pcm_out,
    int frame_samples_cap
);

#ifdef __cplusplus
}
#endif

#endif
