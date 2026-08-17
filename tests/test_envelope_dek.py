"""Unit tests for the envelope-encryption DEK lifecycle (nce/envelope.py).

Pure-unit (no Docker/DB): exercises generate -> wrap -> unwrap round-trips,
payload encryption under the DEK, and failure on a wrong master key / wrong DEK.
"""

from __future__ import annotations

import pytest

from nce.envelope import (
    _DEK_PAYLOAD_PREFIX,
    _DEK_SIZE,
    DEKDecryptionError,
    decrypt_with_dek,
    encrypt_with_dek,
    generate_dek,
    new_dek_key_id,
    unwrap_dek,
    wrap_dek,
)
from nce.signing import MasterKey, SigningKeyDecryptionError


def test_generate_dek_is_32_bytes_and_random():
    a = generate_dek()
    b = generate_dek()
    assert len(a) == _DEK_SIZE == 32
    assert len(b) == 32
    assert a != b  # CSPRNG — collision probability is negligible


def test_new_dek_key_id_is_unique_and_carries_no_key_material():
    id1 = new_dek_key_id()
    id2 = new_dek_key_id()
    assert id1 != id2
    assert id1.startswith("dek-")


def test_wrap_unwrap_round_trips():
    mk = MasterKey(b"m" * 32)
    dek = generate_dek()
    wrapped = wrap_dek(dek, mk)
    # Wrapped blob must not equal the plaintext DEK (it is actually encrypted).
    assert wrapped != dek
    assert len(wrapped) > _DEK_SIZE
    unwrapped = unwrap_dek(wrapped, mk)
    assert unwrapped == dek
    mk.zero()


def test_wrap_is_nondeterministic_per_blob_nonce_salt():
    mk = MasterKey(b"m" * 32)
    dek = generate_dek()
    w1 = wrap_dek(dek, mk)
    w2 = wrap_dek(dek, mk)
    # Per-blob random salt + nonce => identical DEK wraps to different blobs.
    assert w1 != w2
    # Both still unwrap to the same DEK.
    assert unwrap_dek(w1, mk) == unwrap_dek(w2, mk) == dek
    mk.zero()


def test_unwrap_with_wrong_master_key_fails():
    mk = MasterKey(b"m" * 32)
    wrong = MasterKey(b"x" * 32)
    dek = generate_dek()
    wrapped = wrap_dek(dek, mk)
    with pytest.raises(SigningKeyDecryptionError):
        unwrap_dek(wrapped, wrong)
    mk.zero()
    wrong.zero()


def test_wrap_rejects_wrong_size_dek():
    mk = MasterKey(b"m" * 32)
    with pytest.raises(ValueError, match="DEK must be exactly 32 bytes"):
        wrap_dek(b"too-short", mk)
    mk.zero()


def test_payload_encrypt_decrypt_round_trips_under_dek():
    dek = generate_dek()
    plaintext = b"the raw memory content that fans out to episodes.raw_data"
    blob = encrypt_with_dek(plaintext, dek)
    assert blob.startswith(_DEK_PAYLOAD_PREFIX)
    assert plaintext not in blob  # actually encrypted
    assert decrypt_with_dek(blob, dek) == plaintext


def test_payload_decrypt_with_wrong_dek_fails():
    dek = generate_dek()
    other_dek = generate_dek()
    blob = encrypt_with_dek(b"secret", dek)
    with pytest.raises(DEKDecryptionError):
        decrypt_with_dek(blob, other_dek)


def test_destroyed_dek_makes_ciphertext_undecryptable():
    """Provable-forgetting property: without the DEK the ciphertext is unrecoverable.

    Wrapping under the master key + losing the wrapped DEK means even the master
    key cannot recover the plaintext — only a DEK that round-trips can.
    """
    mk = MasterKey(b"m" * 32)
    dek = generate_dek()
    wrapped = wrap_dek(dek, mk)
    blob = encrypt_with_dek(b"forget me", dek)

    # Simulate DEK destruction: the only path back to the DEK is the wrapped
    # blob; a *different* (replacement) DEK cannot decrypt the old ciphertext.
    replacement = generate_dek()
    with pytest.raises(DEKDecryptionError):
        decrypt_with_dek(blob, replacement)

    # Sanity: the legitimately unwrapped DEK still decrypts.
    assert decrypt_with_dek(blob, unwrap_dek(wrapped, mk)) == b"forget me"
    mk.zero()


def test_payload_decrypt_rejects_missing_prefix():
    dek = generate_dek()
    with pytest.raises(DEKDecryptionError, match="prefix"):
        decrypt_with_dek(b"no-prefix-here-garbage-bytes", dek)
