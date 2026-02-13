#ifndef TELECALLS_CODEC_OPUS_H
#define TELECALLS_CODEC_OPUS_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tc_opus_codec_t tc_opus_codec_t;

enum tc_opus_result {
    TC_OPUS_OK = 0,
    TC_OPUS_ERR = -1,
    TC_OPUS_ERR_UNAVAILABLE = -2,
    TC_OPUS_ERR_INVALID_ARG = -3
};

tc_opus_codec_t *tc_opus_codec_create(int sample_rate, int channels, int bitrate_bps);
int tc_opus_codec_set_bitrate(tc_opus_codec_t *codec, int bitrate_bps);
int tc_opus_encode(
    tc_opus_codec_t *codec,
    const int16_t *pcm,
    int frame_size,
    uint8_t *out,
    int out_cap
);
int tc_opus_decode(
    tc_opus_codec_t *codec,
    const uint8_t *data,
    int len,
    int16_t *pcm_out,
    int frame_size
);
void tc_opus_codec_destroy(tc_opus_codec_t *codec);

#ifdef __cplusplus
}
#endif

#endif
