#ifndef TELECALLS_CRYPTO_MTPROTO2_H
#define TELECALLS_CRYPTO_MTPROTO2_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum tc_crypto_mode {
    TC_CRYPTO_MODE_NONE = 0,
    TC_CRYPTO_MODE_CTR = 1,
    TC_CRYPTO_MODE_SHORT = 2
};

enum tc_ctr_kdf_offset_family {
    TC_CTR_KDF_SIGNALING_128 = 0,
    TC_CTR_KDF_SIGNALING_0 = 1
};

enum tc_ctr_hash_mode {
    TC_CTR_HASH_SEQ_PLUS_PAYLOAD = 0,
    TC_CTR_HASH_PAYLOAD_ONLY = 1
};

enum tc_crypto_result {
    TC_CRYPTO_OK = 0,
    TC_CRYPTO_ERR_INVALID_ARG = -1,
    TC_CRYPTO_ERR_CIPHER_LEN_INVALID = -2,
    TC_CRYPTO_ERR_MSG_KEY_MISMATCH = -3,
    TC_CRYPTO_ERR_PADDING_INVALID = -4,
    TC_CRYPTO_ERR_AES_FAILURE = -5,
    TC_CRYPTO_ERR_HEADER_INVALID = -6,
    TC_CRYPTO_ERR_OUTPUT_TOO_SMALL = -7
};

typedef struct tc_crypto_debug_t {
    int reason_code;
    uint32_t msg_key_match;
    size_t cipher_len;
    size_t plain_len;
    int role_dir;
    int mode;
    uint32_t header_seq;
    uint32_t flags_or_reserved;
} tc_crypto_debug_t;

int tc_mtproto2_kdf2(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t msg_key[16],
    size_t x,
    uint8_t aes_key[32],
    uint8_t aes_iv[32]
);

int tc_mtproto2_encrypt_short(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *plain,
    size_t plain_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
);

int tc_mtproto2_decrypt_short(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *encrypted,
    size_t encrypted_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
);

int tc_mtproto2_decrypt_short_ex(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *encrypted,
    size_t encrypted_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len,
    tc_crypto_debug_t *dbg
);

/*
 * AES-CTR encrypt/decrypt for tgcalls signaling compatibility.
 *
 * Official tgcalls (all versions: 2.7.7 through 11.0.0) uses:
 *   - AES-256-CTR (not IGE)
 *   - Key offset x + 128 for signaling
 *   - 16-byte CTR IV (not 32-byte IGE IV)
 *   - Packet format: [16-byte msgKey] + [CTR-encrypted: [4-byte seq NBO] + [payload]]
 */
int tc_mtproto2_encrypt_ctr(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *plain,
    size_t plain_len,
    uint32_t seq,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
);

int tc_mtproto2_decrypt_ctr(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *encrypted,
    size_t encrypted_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len,
    uint32_t *seq_out
);

int tc_mtproto2_decrypt_ctr_ex(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *encrypted,
    size_t encrypted_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len,
    uint32_t *seq_out,
    tc_crypto_debug_t *dbg
);

int tc_mtproto2_decrypt_ctr_variant_ex(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *encrypted,
    size_t encrypted_len,
    int is_outgoing,
    int kdf_offset_family,
    int hash_mode,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len,
    uint32_t *seq_out,
    tc_crypto_debug_t *dbg
);

#ifdef __cplusplus
}
#endif

#endif
