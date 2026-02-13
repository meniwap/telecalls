from __future__ import annotations

import os
import subprocess

import pytest

from telecraft.mtproto.crypto.aes_ige import AesIge


def test_encrypt_decrypt_roundtrip() -> None:
    key = bytes.fromhex("00" * 32)
    iv = bytes.fromhex("11" * 32)
    data = b"\x00" * 32
    aes = AesIge(key=key, iv=iv)
    ct = aes.encrypt(data)
    pt = aes.decrypt(ct)
    assert pt == data


@pytest.mark.skipif(os.environ.get("CI") == "true", reason="Avoid OpenSSL dependency in CI.")
def test_against_openssl_if_available(tmp_path) -> None:
    """
    Cross-check AES-256-IGE against OpenSSL when available locally.

    If OpenSSL is missing or doesn't support -aes-256-ige, we skip.
    """

    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f" * 2)
    iv = bytes.fromhex("0f0e0d0c0b0a09080706050403020100" * 2)
    pt = bytes.fromhex("00112233445566778899aabbccddeeffffeeddccbbaa99887766554433221100")

    aes = AesIge(key=key, iv=iv)
    expected = aes.encrypt(pt)  # our result (used only if openssl exists)

    in_file = tmp_path / "in.bin"
    out_file = tmp_path / "out.bin"
    in_file.write_bytes(pt)

    cmd = [
        "openssl",
        "enc",
        "-aes-256-ige",
        "-K",
        key.hex(),
        "-iv",
        iv.hex(),
        "-nopad",
        "-in",
        str(in_file),
        "-out",
        str(out_file),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        pytest.skip("OpenSSL not installed")
    except subprocess.CalledProcessError as e:
        pytest.skip(f"OpenSSL failed / no -aes-256-ige support: {e.stderr.strip()}")

    openssl_ct = out_file.read_bytes()
    assert openssl_ct == expected
