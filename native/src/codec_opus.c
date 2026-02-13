#include "telecalls/codec_opus.h"

#include <stdlib.h>
#include <string.h>

#ifdef TELECALLS_WITH_OPUS
#include <opus.h>

struct tc_opus_codec_t {
    OpusEncoder *enc;
    OpusDecoder *dec;
    int sample_rate;
    int channels;
    int bitrate_bps;
};

tc_opus_codec_t *tc_opus_codec_create(int sample_rate, int channels, int bitrate_bps) {
    int err = OPUS_OK;
    tc_opus_codec_t *codec = NULL;

    if (sample_rate <= 0 || channels <= 0 || bitrate_bps <= 0) {
        return NULL;
    }

    codec = (tc_opus_codec_t *)calloc(1U, sizeof(tc_opus_codec_t));
    if (codec == NULL) {
        return NULL;
    }

    codec->enc = opus_encoder_create(sample_rate, channels, OPUS_APPLICATION_VOIP, &err);
    if (codec->enc == NULL || err != OPUS_OK) {
        free(codec);
        return NULL;
    }

    codec->dec = opus_decoder_create(sample_rate, channels, &err);
    if (codec->dec == NULL || err != OPUS_OK) {
        opus_encoder_destroy(codec->enc);
        free(codec);
        return NULL;
    }

    codec->sample_rate = sample_rate;
    codec->channels = channels;
    codec->bitrate_bps = bitrate_bps;
    tc_opus_codec_set_bitrate(codec, bitrate_bps);
    return codec;
}

int tc_opus_codec_set_bitrate(tc_opus_codec_t *codec, int bitrate_bps) {
    if (codec == NULL || codec->enc == NULL || bitrate_bps <= 0) {
        return TC_OPUS_ERR_INVALID_ARG;
    }
    if (opus_encoder_ctl(codec->enc, OPUS_SET_BITRATE(bitrate_bps)) != OPUS_OK) {
        return TC_OPUS_ERR;
    }
    codec->bitrate_bps = bitrate_bps;
    return TC_OPUS_OK;
}

int tc_opus_encode(
    tc_opus_codec_t *codec,
    const int16_t *pcm,
    int frame_size,
    uint8_t *out,
    int out_cap
) {
    if (codec == NULL || codec->enc == NULL || pcm == NULL || out == NULL || frame_size <= 0 || out_cap <= 0) {
        return TC_OPUS_ERR_INVALID_ARG;
    }
    return opus_encode(codec->enc, pcm, frame_size, out, out_cap);
}

int tc_opus_decode(
    tc_opus_codec_t *codec,
    const uint8_t *data,
    int len,
    int16_t *pcm_out,
    int frame_size
) {
    if (codec == NULL || codec->dec == NULL || data == NULL || len <= 0 || pcm_out == NULL || frame_size <= 0) {
        return TC_OPUS_ERR_INVALID_ARG;
    }
    return opus_decode(codec->dec, data, len, pcm_out, frame_size, 0);
}

void tc_opus_codec_destroy(tc_opus_codec_t *codec) {
    if (codec == NULL) {
        return;
    }
    if (codec->enc != NULL) {
        opus_encoder_destroy(codec->enc);
    }
    if (codec->dec != NULL) {
        opus_decoder_destroy(codec->dec);
    }
    free(codec);
}

#else

struct tc_opus_codec_t {
    int unavailable;
};

tc_opus_codec_t *tc_opus_codec_create(int sample_rate, int channels, int bitrate_bps) {
    (void)sample_rate;
    (void)channels;
    (void)bitrate_bps;
    return NULL;
}

int tc_opus_codec_set_bitrate(tc_opus_codec_t *codec, int bitrate_bps) {
    (void)codec;
    (void)bitrate_bps;
    return TC_OPUS_ERR_UNAVAILABLE;
}

int tc_opus_encode(
    tc_opus_codec_t *codec,
    const int16_t *pcm,
    int frame_size,
    uint8_t *out,
    int out_cap
) {
    (void)codec;
    (void)pcm;
    (void)frame_size;
    (void)out;
    (void)out_cap;
    return TC_OPUS_ERR_UNAVAILABLE;
}

int tc_opus_decode(
    tc_opus_codec_t *codec,
    const uint8_t *data,
    int len,
    int16_t *pcm_out,
    int frame_size
) {
    (void)codec;
    (void)data;
    (void)len;
    (void)pcm_out;
    (void)frame_size;
    return TC_OPUS_ERR_UNAVAILABLE;
}

void tc_opus_codec_destroy(tc_opus_codec_t *codec) {
    (void)codec;
}

#endif
