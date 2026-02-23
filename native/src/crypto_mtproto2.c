#include "telecalls/crypto_mtproto2.h"

#include <openssl/aes.h>
#include <openssl/rand.h>
#include <openssl/sha.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>

#define TC_MSG_KEY_LEN 16U
#define TC_AES_BLOCK 16U

static uint64_t tc_debug_now_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return ((uint64_t)tv.tv_sec * 1000ULL) + ((uint64_t)tv.tv_usec / 1000ULL);
}

static void tc_agent_debug_log_crypto(
    const char *run_id,
    const char *hypothesis_id,
    const char *location,
    const char *message,
    const char *data_json
) {
    static uint64_t seq = 0U;
    (void)mkdir("/Users/meniwap/satla/.cursor", 0777);
    FILE *handle = fopen("/Users/meniwap/satla/.cursor/debug.log", "a");
    uint64_t ts = 0U;
    if (handle == NULL) {
        return;
    }
    ts = tc_debug_now_ms();
    seq += 1U;
    fprintf(
        handle,
        "{\"id\":\"c2_%llu_%llu\",\"timestamp\":%llu,\"location\":\"%s\",\"message\":\"%s\","
        "\"runId\":\"%s\",\"hypothesisId\":\"%s\",\"data\":%s}\n",
        (unsigned long long)ts,
        (unsigned long long)seq,
        (unsigned long long)ts,
        location,
        message,
        run_id,
        hypothesis_id,
        data_json
    );
    fclose(handle);
}

/*
 * AES-256-CTR key/IV derivation for tgcalls signaling.
 * Same SHA256 KDF as MTProto2 but produces a 16-byte CTR IV
 * instead of a 32-byte IGE IV.
 */
static int tc_mtproto2_kdf2_ctr(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t msg_key[16],
    size_t x,
    uint8_t aes_key[32],
    uint8_t aes_iv[16]
) {
    uint8_t sha256a[SHA256_DIGEST_LENGTH];
    uint8_t sha256b[SHA256_DIGEST_LENGTH];
    SHA256_CTX sha;

    if (auth_key == NULL || msg_key == NULL || aes_key == NULL || aes_iv == NULL) {
        return -1;
    }
    if (auth_key_len < (76U + x)) {
        return -1;
    }

    SHA256_Init(&sha);
    SHA256_Update(&sha, msg_key, TC_MSG_KEY_LEN);
    SHA256_Update(&sha, auth_key + x, 36U);
    SHA256_Final(sha256a, &sha);

    SHA256_Init(&sha);
    SHA256_Update(&sha, auth_key + 40U + x, 36U);
    SHA256_Update(&sha, msg_key, TC_MSG_KEY_LEN);
    SHA256_Final(sha256b, &sha);

    /* AES key (32 bytes) – same layout as IGE */
    memcpy(aes_key, sha256a, 8U);
    memcpy(aes_key + 8U, sha256b + 8U, 16U);
    memcpy(aes_key + 24U, sha256a + 24U, 8U);

    /* AES IV (16 bytes) – differs from 32-byte IGE IV */
    memcpy(aes_iv, sha256b, 4U);
    memcpy(aes_iv + 4U, sha256a + 8U, 8U);
    memcpy(aes_iv + 12U, sha256b + 24U, 4U);

    return 0;
}

/*
 * Portable AES-256-CTR encrypt/decrypt.
 * Uses AES_encrypt (single-block) + XOR for portability across
 * all OpenSSL/LibreSSL versions without requiring <openssl/modes.h>.
 */
static void tc_aes_ctr128(
    const uint8_t *in,
    uint8_t *out,
    size_t len,
    const AES_KEY *key,
    uint8_t iv[16]
) {
    uint8_t eblock[TC_AES_BLOCK];
    size_t pos = 0U;
    size_t chunk = 0U;
    size_t k = 0U;
    int j = 0;

    while (pos < len) {
        AES_encrypt(iv, eblock, key);

        /* Increment big-endian counter (rightmost bytes) */
        for (j = 15; j >= 0; j--) {
            iv[j] = (uint8_t)(iv[j] + 1U);
            if (iv[j] != 0U) {
                break;
            }
        }

        chunk = len - pos;
        if (chunk > TC_AES_BLOCK) {
            chunk = TC_AES_BLOCK;
        }

        for (k = 0U; k < chunk; k++) {
            out[pos + k] = in[pos + k] ^ eblock[k];
        }
        pos += chunk;
    }
}

static void tc_write_u16_le(uint8_t *out, uint16_t value) {
    out[0] = (uint8_t)(value & 0xFFU);
    out[1] = (uint8_t)((value >> 8) & 0xFFU);
}

static uint16_t tc_read_u16_le(const uint8_t *in) {
    return (uint16_t)(((uint16_t)in[0]) | ((uint16_t)in[1] << 8));
}

static void tc_crypto_debug_reset(
    tc_crypto_debug_t *dbg,
    int mode,
    int role_dir,
    size_t cipher_len
) {
    if (dbg == NULL) {
        return;
    }
    memset(dbg, 0, sizeof(*dbg));
    dbg->reason_code = TC_CRYPTO_OK;
    dbg->mode = mode;
    dbg->role_dir = role_dir;
    dbg->cipher_len = cipher_len;
}

static int tc_crypto_fail(tc_crypto_debug_t *dbg, int reason_code) {
    if (dbg != NULL) {
        dbg->reason_code = reason_code;
    }
    return -1;
}

int tc_mtproto2_kdf2(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t msg_key[16],
    size_t x,
    uint8_t aes_key[32],
    uint8_t aes_iv[32]
) {
    uint8_t sha256a[SHA256_DIGEST_LENGTH];
    uint8_t sha256b[SHA256_DIGEST_LENGTH];
    SHA256_CTX sha;

    if (auth_key == NULL || msg_key == NULL || aes_key == NULL || aes_iv == NULL) {
        return -1;
    }
    if (auth_key_len < (128U + x)) {
        return -1;
    }

    SHA256_Init(&sha);
    SHA256_Update(&sha, msg_key, TC_MSG_KEY_LEN);
    SHA256_Update(&sha, auth_key + x, 36U);
    SHA256_Final(sha256a, &sha);

    SHA256_Init(&sha);
    SHA256_Update(&sha, auth_key + 40U + x, 36U);
    SHA256_Update(&sha, msg_key, TC_MSG_KEY_LEN);
    SHA256_Final(sha256b, &sha);

    memcpy(aes_key, sha256a, 8U);
    memcpy(aes_key + 8U, sha256b + 8U, 16U);
    memcpy(aes_key + 24U, sha256a + 24U, 8U);

    memcpy(aes_iv, sha256b, 8U);
    memcpy(aes_iv + 8U, sha256a + 8U, 16U);
    memcpy(aes_iv + 24U, sha256b + 24U, 8U);

    return 0;
}

int tc_mtproto2_encrypt_short(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *plain,
    size_t plain_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
) {
    uint8_t *inner = NULL;
    uint8_t msg_key[TC_MSG_KEY_LEN];
    uint8_t msg_key_large[SHA256_DIGEST_LENGTH];
    uint8_t aes_key[32];
    uint8_t aes_iv[32];
    AES_KEY enc_key;
    SHA256_CTX sha;
    size_t x = (is_outgoing != 0) ? 0U : 8U;
    size_t inner_len = 0U;
    size_t pad_len = 0U;
    size_t total_len = 0U;

    if (auth_key == NULL || plain == NULL || out == NULL || out_len == NULL) {
        return -1;
    }
    if (plain_len > 0xFFFFU) {
        return -1;
    }
    if (auth_key_len < (128U + x)) {
        return -1;
    }

    inner_len = 2U + plain_len;
    pad_len = TC_AES_BLOCK - (inner_len % TC_AES_BLOCK);
    if (pad_len < TC_AES_BLOCK) {
        pad_len += TC_AES_BLOCK;
    }
    total_len = inner_len + pad_len;

    if (out_cap < (TC_MSG_KEY_LEN + total_len)) {
        return -1;
    }

    inner = (uint8_t *)malloc(total_len);
    if (inner == NULL) {
        return -1;
    }
    memset(inner, 0, total_len);

    tc_write_u16_le(inner, (uint16_t)plain_len);
    if (plain_len > 0U) {
        memcpy(inner + 2U, plain, plain_len);
    }
    if (RAND_bytes(inner + inner_len, (int)pad_len) != 1) {
        memset(inner + inner_len, 0, pad_len);
    }

    SHA256_Init(&sha);
    SHA256_Update(&sha, auth_key + 88U + x, 32U);
    SHA256_Update(&sha, inner, total_len);
    SHA256_Final(msg_key_large, &sha);
    memcpy(msg_key, msg_key_large + 8U, TC_MSG_KEY_LEN);

    if (tc_mtproto2_kdf2(auth_key, auth_key_len, msg_key, x, aes_key, aes_iv) != 0) {
        memset(inner, 0, total_len);
        free(inner);
        return -1;
    }

    if (AES_set_encrypt_key(aes_key, 256, &enc_key) != 0) {
        memset(inner, 0, total_len);
        free(inner);
        return -1;
    }

    memcpy(out, msg_key, TC_MSG_KEY_LEN);
    AES_ige_encrypt(inner, out + TC_MSG_KEY_LEN, total_len, &enc_key, aes_iv, AES_ENCRYPT);
    *out_len = TC_MSG_KEY_LEN + total_len;

    memset(inner, 0, total_len);
    free(inner);
    return 0;
}

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
) {
    const uint8_t *msg_key = NULL;
    const uint8_t *cipher = NULL;
    uint8_t *inner = NULL;
    uint8_t aes_key[32];
    uint8_t aes_iv[32];
    uint8_t msg_key_large[SHA256_DIGEST_LENGTH];
    uint8_t msg_key_large_alt[SHA256_DIGEST_LENGTH];
    AES_KEY dec_key;
    SHA256_CTX sha;
    size_t x = (is_outgoing != 0) ? 8U : 0U;
    size_t cipher_len = 0U;
    size_t plain_len = 0U;
    uint16_t declared_len = 0U;

    tc_crypto_debug_reset(
        dbg,
        TC_CRYPTO_MODE_SHORT,
        is_outgoing != 0 ? 1 : 0,
        encrypted_len >= TC_MSG_KEY_LEN ? (encrypted_len - TC_MSG_KEY_LEN) : 0U
    );

    if (auth_key == NULL || encrypted == NULL || out == NULL || out_len == NULL) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }
    if (encrypted_len < (TC_MSG_KEY_LEN + TC_AES_BLOCK)) {
        // #region agent log
        {
            char data_json[192];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"reason\":\"encrypted_too_short\",\"encrypted_len\":%llu,\"is_outgoing\":%d}",
                (unsigned long long)encrypted_len,
                (int)is_outgoing
            );
            tc_agent_debug_log_crypto(
                "run1",
                "H2",
                "crypto_mtproto2.c:173",
                "decrypt_short rejected packet shape",
                data_json
            );
        }
        // #endregion
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_CIPHER_LEN_INVALID);
    }
    if (auth_key_len < (128U + x)) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }

    msg_key = encrypted;
    cipher = encrypted + TC_MSG_KEY_LEN;
    cipher_len = encrypted_len - TC_MSG_KEY_LEN;

    if ((cipher_len % TC_AES_BLOCK) != 0U) {
        // #region agent log
        {
            char data_json[224];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"reason\":\"cipher_len_not_block_aligned\",\"encrypted_len\":%llu,"
                "\"cipher_len\":%llu,\"is_outgoing\":%d}",
                (unsigned long long)encrypted_len,
                (unsigned long long)cipher_len,
                (int)is_outgoing
            );
            tc_agent_debug_log_crypto(
                "run1",
                "H2",
                "crypto_mtproto2.c:184",
                "decrypt_short rejected packet alignment",
                data_json
            );
        }
        // #endregion
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_CIPHER_LEN_INVALID);
    }
    if (dbg != NULL) {
        dbg->cipher_len = cipher_len;
    }

    if (tc_mtproto2_kdf2(auth_key, auth_key_len, msg_key, x, aes_key, aes_iv) != 0) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }

    if (AES_set_decrypt_key(aes_key, 256, &dec_key) != 0) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_AES_FAILURE);
    }

    inner = (uint8_t *)malloc(cipher_len);
    if (inner == NULL) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_AES_FAILURE);
    }
    AES_ige_encrypt(cipher, inner, cipher_len, &dec_key, aes_iv, AES_DECRYPT);

    SHA256_Init(&sha);
    SHA256_Update(&sha, auth_key + 88U + x, 32U);
    SHA256_Update(&sha, inner, cipher_len);
    SHA256_Final(msg_key_large, &sha);

    if (memcmp(msg_key, msg_key_large + 8U, TC_MSG_KEY_LEN) != 0) {
        if (cipher_len <= 2U) {
            memset(inner, 0, cipher_len);
            free(inner);
            return tc_crypto_fail(dbg, TC_CRYPTO_ERR_MSG_KEY_MISMATCH);
        }
        /* Compatibility path for peers deriving msg_key without the short-length prefix. */
        SHA256_Init(&sha);
        SHA256_Update(&sha, auth_key + 88U + x, 32U);
        SHA256_Update(&sha, inner + 2U, cipher_len - 2U);
        SHA256_Final(msg_key_large_alt, &sha);
        if (memcmp(msg_key, msg_key_large_alt + 8U, TC_MSG_KEY_LEN) != 0) {
            // #region agent log
            {
                char data_json[208];
                snprintf(
                    data_json,
                    sizeof(data_json),
                    "{\"reason\":\"msg_key_mismatch\",\"cipher_len\":%llu,\"is_outgoing\":%d}",
                    (unsigned long long)cipher_len,
                    (int)is_outgoing
                );
                tc_agent_debug_log_crypto(
                    "run1",
                    "H1",
                    "crypto_mtproto2.c:218",
                    "decrypt_short msg_key mismatch",
                    data_json
                );
            }
            // #endregion
            memset(inner, 0, cipher_len);
            free(inner);
            return tc_crypto_fail(dbg, TC_CRYPTO_ERR_MSG_KEY_MISMATCH);
        }
    }
    if (dbg != NULL) {
        dbg->msg_key_match = 1U;
    }

    declared_len = tc_read_u16_le(inner);
    if ((size_t)declared_len > (cipher_len - 2U)) {
        // #region agent log
        {
            char data_json[224];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"reason\":\"declared_len_overflow\",\"declared_len\":%u,\"cipher_len\":%llu,"
                "\"is_outgoing\":%d}",
                (unsigned int)declared_len,
                (unsigned long long)cipher_len,
                (int)is_outgoing
            );
            tc_agent_debug_log_crypto(
                "run1",
                "H2",
                "crypto_mtproto2.c:226",
                "decrypt_short invalid declared_len",
                data_json
            );
        }
        // #endregion
        memset(inner, 0, cipher_len);
        free(inner);
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_PADDING_INVALID);
    }
    if ((cipher_len - 2U - (size_t)declared_len) < TC_AES_BLOCK) {
        memset(inner, 0, cipher_len);
        free(inner);
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_PADDING_INVALID);
    }

    plain_len = (size_t)declared_len;
    if (plain_len > out_cap) {
        memset(inner, 0, cipher_len);
        free(inner);
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_OUTPUT_TOO_SMALL);
    }

    if (plain_len > 0U) {
        memcpy(out, inner + 2U, plain_len);
    }
    *out_len = plain_len;
    if (dbg != NULL) {
        dbg->plain_len = plain_len;
        dbg->reason_code = TC_CRYPTO_OK;
    }

    memset(inner, 0, cipher_len);
    free(inner);
    return 0;
}

int tc_mtproto2_decrypt_short(
    const uint8_t *auth_key,
    size_t auth_key_len,
    const uint8_t *encrypted,
    size_t encrypted_len,
    int is_outgoing,
    uint8_t *out,
    size_t out_cap,
    size_t *out_len
) {
    return tc_mtproto2_decrypt_short_ex(
        auth_key,
        auth_key_len,
        encrypted,
        encrypted_len,
        is_outgoing,
        out,
        out_cap,
        out_len,
        NULL
    );
}

/* ================================================================
 * AES-CTR encrypt/decrypt for tgcalls signaling compatibility.
 *
 * Wire format (matches official tgcalls EncryptedConnection):
 *   [16-byte msgKey] + [AES-256-CTR encrypted: [4-byte seq NBO] + [payload]]
 *
 * Key offset for signaling:
 *   encrypt: x = (is_outgoing ? 0 : 8) + 128
 *   decrypt: x = (is_outgoing ? 8 : 0) + 128
 * ================================================================ */

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
) {
    uint8_t *inner = NULL;
    uint8_t msg_key[TC_MSG_KEY_LEN];
    uint8_t msg_key_large[SHA256_DIGEST_LENGTH];
    uint8_t aes_key[32];
    uint8_t aes_iv[16];
    AES_KEY enc_key;
    SHA256_CTX sha;
    size_t x = ((is_outgoing != 0) ? 0U : 8U) + 128U;
    size_t inner_len = 4U + plain_len;

    if (auth_key == NULL || out == NULL || out_len == NULL) {
        return -1;
    }
    if (plain_len > 0U && plain == NULL) {
        return -1;
    }
    if (auth_key_len < (120U + x)) {
        return -1;
    }
    if (out_cap < (TC_MSG_KEY_LEN + inner_len)) {
        return -1;
    }

    inner = (uint8_t *)malloc(inner_len);
    if (inner == NULL) {
        return -1;
    }

    /* 4-byte seq in network byte order (big-endian) */
    inner[0] = (uint8_t)((seq >> 24) & 0xFFU);
    inner[1] = (uint8_t)((seq >> 16) & 0xFFU);
    inner[2] = (uint8_t)((seq >> 8) & 0xFFU);
    inner[3] = (uint8_t)(seq & 0xFFU);

    if (plain_len > 0U) {
        memcpy(inner + 4U, plain, plain_len);
    }

    /* msgKey = SHA256(auth_key[88+x : 120+x] || inner)[8:24] */
    SHA256_Init(&sha);
    SHA256_Update(&sha, auth_key + 88U + x, 32U);
    SHA256_Update(&sha, inner, inner_len);
    SHA256_Final(msg_key_large, &sha);
    memcpy(msg_key, msg_key_large + 8U, TC_MSG_KEY_LEN);

    if (tc_mtproto2_kdf2_ctr(auth_key, auth_key_len, msg_key, x, aes_key, aes_iv) != 0) {
        memset(inner, 0, inner_len);
        free(inner);
        return -1;
    }

    if (AES_set_encrypt_key(aes_key, 256, &enc_key) != 0) {
        memset(inner, 0, inner_len);
        free(inner);
        return -1;
    }

    memcpy(out, msg_key, TC_MSG_KEY_LEN);
    tc_aes_ctr128(inner, out + TC_MSG_KEY_LEN, inner_len, &enc_key, aes_iv);
    *out_len = TC_MSG_KEY_LEN + inner_len;

    memset(inner, 0, inner_len);
    free(inner);
    return 0;
}

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
) {
    const uint8_t *msg_key = NULL;
    const uint8_t *cipher = NULL;
    uint8_t *decrypted = NULL;
    uint8_t aes_key[32];
    uint8_t aes_iv[16];
    uint8_t msg_key_large[SHA256_DIGEST_LENGTH];
    AES_KEY enc_key;
    SHA256_CTX sha;
    size_t role_x = ((is_outgoing != 0) ? 8U : 0U);
    size_t x = role_x;
    size_t cipher_len = 0U;
    size_t payload_len = 0U;
    uint32_t seq = 0U;
    size_t hash_offset = 0U;
    size_t hash_len = 0U;

    tc_crypto_debug_reset(
        dbg,
        TC_CRYPTO_MODE_CTR,
        is_outgoing != 0 ? 1 : 0,
        encrypted_len >= TC_MSG_KEY_LEN ? (encrypted_len - TC_MSG_KEY_LEN) : 0U
    );
    if (dbg != NULL) {
        dbg->flags_or_reserved = 0U;
    }

    if (auth_key == NULL || encrypted == NULL || out == NULL || out_len == NULL) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }
    if (kdf_offset_family != TC_CTR_KDF_SIGNALING_128 &&
        kdf_offset_family != TC_CTR_KDF_SIGNALING_0) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }
    if (hash_mode != TC_CTR_HASH_SEQ_PLUS_PAYLOAD && hash_mode != TC_CTR_HASH_PAYLOAD_ONLY) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }
    if (kdf_offset_family == TC_CTR_KDF_SIGNALING_128) {
        x += 128U;
    }
    if (encrypted_len < (TC_MSG_KEY_LEN + 5U)) {
        /* 16 msgKey + 4 seq + 1 minimum payload */
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_CIPHER_LEN_INVALID);
    }
    if (auth_key_len < (120U + x)) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }

    msg_key = encrypted;
    cipher = encrypted + TC_MSG_KEY_LEN;
    cipher_len = encrypted_len - TC_MSG_KEY_LEN;
    if (dbg != NULL) {
        dbg->cipher_len = cipher_len;
    }

    /* Derive AES key and CTR IV */
    if (tc_mtproto2_kdf2_ctr(auth_key, auth_key_len, msg_key, x, aes_key, aes_iv) != 0) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_INVALID_ARG);
    }

    /* AES-CTR always uses encrypt direction for the AES key schedule */
    if (AES_set_encrypt_key(aes_key, 256, &enc_key) != 0) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_AES_FAILURE);
    }

    decrypted = (uint8_t *)malloc(cipher_len);
    if (decrypted == NULL) {
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_AES_FAILURE);
    }

    tc_aes_ctr128(cipher, decrypted, cipher_len, &enc_key, aes_iv);

    hash_offset = 0U;
    hash_len = cipher_len;
    if (hash_mode == TC_CTR_HASH_PAYLOAD_ONLY) {
        if (cipher_len <= 4U) {
            memset(decrypted, 0, cipher_len);
            free(decrypted);
            return tc_crypto_fail(dbg, TC_CRYPTO_ERR_CIPHER_LEN_INVALID);
        }
        hash_offset = 4U;
        hash_len = cipher_len - 4U;
    }

    /* Verify msgKey = SHA256(auth_key[88+x : 120+x] || selected-plain)[8:24] */
    SHA256_Init(&sha);
    SHA256_Update(&sha, auth_key + 88U + x, 32U);
    SHA256_Update(&sha, decrypted + hash_offset, hash_len);
    SHA256_Final(msg_key_large, &sha);

    if (memcmp(msg_key, msg_key_large + 8U, TC_MSG_KEY_LEN) != 0) {
        /* #region agent log */
        {
            char data_json[256];
            snprintf(
                data_json,
                sizeof(data_json),
                "{\"reason\":\"ctr_msg_key_mismatch\",\"cipher_len\":%llu,\"is_outgoing\":%d,\"x\":%llu}",
                (unsigned long long)cipher_len,
                (int)is_outgoing,
                (unsigned long long)x
            );
            tc_agent_debug_log_crypto(
                "run1",
                "H19",
                "crypto_mtproto2.c:ctr_decrypt",
                "CTR decrypt msg_key mismatch",
                data_json
            );
        }
        /* #endregion */
        memset(decrypted, 0, cipher_len);
        free(decrypted);
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_MSG_KEY_MISMATCH);
    }
    if (dbg != NULL) {
        dbg->msg_key_match = 1U;
        dbg->flags_or_reserved = (uint32_t)(
            ((uint32_t)(kdf_offset_family & 0xFF) << 8U) | (uint32_t)(hash_mode & 0xFF)
        );
    }

    /* Extract 4-byte seq (network byte order / big-endian) */
    seq = ((uint32_t)decrypted[0] << 24) |
          ((uint32_t)decrypted[1] << 16) |
          ((uint32_t)decrypted[2] << 8) |
          ((uint32_t)decrypted[3]);
    if (seq_out != NULL) {
        *seq_out = seq;
    }
    if (dbg != NULL) {
        dbg->header_seq = seq;
    }

    /* Return payload (everything after seq) */
    payload_len = cipher_len - 4U;
    if (payload_len > out_cap) {
        memset(decrypted, 0, cipher_len);
        free(decrypted);
        return tc_crypto_fail(dbg, TC_CRYPTO_ERR_OUTPUT_TOO_SMALL);
    }
    if (payload_len > 0U) {
        memcpy(out, decrypted + 4U, payload_len);
    }
    *out_len = payload_len;
    if (dbg != NULL) {
        dbg->plain_len = payload_len;
        dbg->reason_code = TC_CRYPTO_OK;
    }

    memset(decrypted, 0, cipher_len);
    free(decrypted);
    return 0;
}

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
) {
    return tc_mtproto2_decrypt_ctr_variant_ex(
        auth_key,
        auth_key_len,
        encrypted,
        encrypted_len,
        is_outgoing,
        TC_CTR_KDF_SIGNALING_128,
        TC_CTR_HASH_SEQ_PLUS_PAYLOAD,
        out,
        out_cap,
        out_len,
        seq_out,
        dbg
    );
}

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
) {
    return tc_mtproto2_decrypt_ctr_ex(
        auth_key,
        auth_key_len,
        encrypted,
        encrypted_len,
        is_outgoing,
        out,
        out_cap,
        out_len,
        seq_out,
        NULL
    );
}
