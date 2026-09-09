"""v5 blobs record WHICH master key wrapped them.

Before v5, a blob carried no key identity, so `decrypt_signing_key` could only report
"either the master key is wrong or the signing key blob has been corrupted". An operator
cannot act on that, and the ambiguity is what turned the 2026-09-07 incident into a
multi-hour investigation: the deployment held one key, the active signing key had been
re-wrapped under another, and telling that apart from data corruption required unwrapping
candidate keys by hand.

v5 is an envelope -- `TC5 || fp(8) || <v3-or-v4 blob>` -- so the wrong-key case is decidable
*before* any crypto runs, and the error names both fingerprints.
"""

from __future__ import annotations

import os

import pytest

from nce.signing import (
    _ENCRYPTED_KEY_BLOB_V3,
    _ENCRYPTED_KEY_BLOB_V4,
    _ENCRYPTED_KEY_BLOB_V5,
    _MASTER_KEY_FP_LEN,
    MasterKey,
    MasterKeyFingerprintMismatch,
    SigningKeyDecryptionError,
    decrypt_signing_key,
    encrypt_signing_key,
    master_key_fingerprint,
    master_key_fingerprint_bytes,
)

_HEAD = len(_ENCRYPTED_KEY_BLOB_V5) + _MASTER_KEY_FP_LEN


def test_new_blobs_are_tagged_with_the_wrapping_key() -> None:
    """The tag is the same number the boot log prints, not a parallel identifier."""

    mk = MasterKey(b"k" * 40)
    blob = encrypt_signing_key(os.urandom(32), mk)

    assert blob.startswith(_ENCRYPTED_KEY_BLOB_V5)
    tag = blob[len(_ENCRYPTED_KEY_BLOB_V5) : _HEAD]
    assert tag == master_key_fingerprint_bytes(mk)
    # One derivation, two representations: a blob's tag can never disagree with the
    # fingerprint an operator reads in the log or stores beside an escrow copy.
    assert tag.hex() == master_key_fingerprint(mk)
    mk.zero()


def test_roundtrip_under_the_same_key() -> None:
    mk = MasterKey(b"r" * 40)
    raw = os.urandom(32)
    assert decrypt_signing_key(encrypt_signing_key(raw, mk), mk) == raw
    mk.zero()


def test_wrong_key_names_both_fingerprints() -> None:
    """The whole point: a wrong key is identified, not guessed at."""

    mk1, mk2 = MasterKey(b"1" * 40), MasterKey(b"2" * 40)
    blob = encrypt_signing_key(os.urandom(32), mk1)

    with pytest.raises(MasterKeyFingerprintMismatch) as excinfo:
        decrypt_signing_key(blob, mk2)

    message = str(excinfo.value)
    assert master_key_fingerprint(mk1) in message, "must name the key that WRAPPED it"
    assert master_key_fingerprint(mk2) in message, "must name the key that was SUPPLIED"
    assert "NOT corrupt" in message, "must rule out the corruption reading explicitly"
    assert "Do NOT rotate" in message, (
        "must warn against the response that makes the loss permanent -- rotating on a "
        "decrypt failure is what destroyed the key twice on 2026-09-07"
    )
    mk1.zero()
    mk2.zero()


def test_mismatch_is_caught_by_handlers_that_expect_the_old_error() -> None:
    """REGRESSION GUARD. This is the bug I shipped in the first draft of v5.

    ``MasterKeyFingerprintMismatch`` was originally a SIBLING of
    ``SigningKeyDecryptionError``, so every existing ``except SigningKeyDecryptionError``
    stopped catching wrong-key errors on v5 blobs -- the boot check, ``conftest.py``'s
    seeding guard, the relay. Handlers written to fail CLOSED would have let a wrong key
    through. Seven tests caught it; without them it would have been a silent hole in
    exactly the code paths that exist to be paranoid.

    Precision must be ADDITIVE to an error contract, never a replacement for it.
    """

    assert issubclass(MasterKeyFingerprintMismatch, SigningKeyDecryptionError)

    mk1, mk2 = MasterKey(b"a" * 40), MasterKey(b"b" * 40)
    blob = encrypt_signing_key(os.urandom(32), mk1)
    with pytest.raises(SigningKeyDecryptionError):
        decrypt_signing_key(blob, mk2)
    mk1.zero()
    mk2.zero()


def test_untagged_blobs_still_decrypt() -> None:
    """Every blob already in the database is untagged. They must keep working.

    Proven by construction rather than by fixture: the inner bytes of a v5 blob ARE a
    complete v3/v4 blob, so decrypting them directly is exactly the pre-v5 path.
    """

    mk = MasterKey(b"u" * 40)
    raw = os.urandom(32)
    inner = encrypt_signing_key(raw, mk)[_HEAD:]

    assert inner.startswith((_ENCRYPTED_KEY_BLOB_V3, _ENCRYPTED_KEY_BLOB_V4))
    assert decrypt_signing_key(inner, mk) == raw
    mk.zero()


def test_untagged_blob_with_wrong_key_keeps_the_old_ambiguous_failure() -> None:
    """An untagged blob has nothing to compare, so it cannot be more precise.

    Asserted so the limit is explicit: v5 improves diagnostics for blobs written AFTER
    it lands. Pre-existing rows keep the old behaviour until a rewrap sweep re-wraps them,
    which is the next piece of this work.
    """

    mk1, mk2 = MasterKey(b"x" * 40), MasterKey(b"y" * 40)
    inner = encrypt_signing_key(os.urandom(32), mk1)[_HEAD:]

    with pytest.raises(SigningKeyDecryptionError) as excinfo:
        decrypt_signing_key(inner, mk2)
    assert not isinstance(excinfo.value, MasterKeyFingerprintMismatch), (
        "an untagged blob cannot yield a fingerprint mismatch -- there is no tag to compare"
    )
    mk1.zero()
    mk2.zero()


def test_the_tag_is_checked_before_any_decryption() -> None:
    """Discriminating control: tag-vs-payload failures must be distinguishable.

    A correct tag with a corrupted payload is an AEAD failure. A correct payload under the
    wrong tag is a mismatch. If the implementation checked the payload first, or ignored
    the tag, one of these two would come back as the other -- and the diagnostic would be
    worthless precisely when it matters.
    """

    mk1, mk2 = MasterKey(b"p" * 40), MasterKey(b"q" * 40)
    good = encrypt_signing_key(os.urandom(32), mk1)

    # Right tag, corrupted inner payload -> AEAD failure, NOT a mismatch.
    corrupted = bytearray(good)
    corrupted[-1] ^= 0xFF
    with pytest.raises(SigningKeyDecryptionError) as aead:
        decrypt_signing_key(bytes(corrupted), mk1)
    assert not isinstance(aead.value, MasterKeyFingerprintMismatch)

    # Intact payload, wrong key -> mismatch, and no crypto was needed to know.
    with pytest.raises(MasterKeyFingerprintMismatch):
        decrypt_signing_key(good, mk2)

    mk1.zero()
    mk2.zero()


def test_truncated_v5_blob_fails_clearly() -> None:
    """A v5 header with no room for a fingerprint must not be read out of bounds."""

    mk = MasterKey(b"t" * 40)
    with pytest.raises(SigningKeyDecryptionError, match="too short"):
        decrypt_signing_key(_ENCRYPTED_KEY_BLOB_V5 + b"\x00" * 4, mk)
    mk.zero()
