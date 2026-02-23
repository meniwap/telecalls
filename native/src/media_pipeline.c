#include "telecalls/media_pipeline.h"

int tc_media_encode_frame(
    tc_opus_codec_t *codec,
    const int16_t *pcm,
    int frame_samples,
    uint8_t *encoded_out,
    int encoded_cap
) {
    if (codec == NULL || pcm == NULL || encoded_out == NULL || frame_samples <= 0 || encoded_cap <= 0) {
        return -1;
    }
    return tc_opus_encode(codec, pcm, frame_samples, encoded_out, encoded_cap);
}

int tc_media_decode_frame(
    tc_opus_codec_t *codec,
    const uint8_t *payload,
    int payload_len,
    int16_t *pcm_out,
    int frame_samples_cap
) {
    if (codec == NULL || payload == NULL || payload_len <= 0 || pcm_out == NULL || frame_samples_cap <= 0) {
        return -1;
    }
    return tc_opus_decode(codec, payload, payload_len, pcm_out, frame_samples_cap);
}
