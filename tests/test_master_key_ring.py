"""Two master keys at once, so a rotation has no unsafe window.

Without a ring, changing ``NCE_MASTER_KEY`` is a big-bang cutover: the instant it changes,
every blob wrapped under the old key is undecryptable. With one, old blobs keep opening
under ``NCE_MASTER_KEY_PREVIOUS`` while new writes use the new key, and a sweep closes the
gap.
"""

from __future__ import annotations

import os

import pytest

from nce.master_key_ring import (
    NoKeyOpensBlobError,
    blob_is_on_primary,
    decrypt_with_ring,
    master_key_ring,
    previous_master_key,
    rewrap,
)
from nce.signing import (
    _ENCRYPTED_KEY_BLOB_V5,
    _MASTER_KEY_FP_LEN,
    MasterKey,
    SigningKeyDecryptionError,
    decrypt_signing_key,
    encrypt_signing_key,
    master_key_fingerprint,
)

_NEW = b"N" * 40
_OLD = b"O" * 40
_ALIEN = b"A" * 40


def test_no_previous_key_means_a_ring_of_one() -> None:
    """The default posture: no rotation in progress, nothing extra to try."""

    assert previous_master_key() is None
    assert len(master_key_ring()) == 1


def test_previous_key_joins_the_ring_after_the_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order matters: primary first, so the common path costs one derivation."""

    monkeypatch.setenv("NCE_MASTER_KEY_PREVIOUS", _OLD.decode())
    ring = master_key_ring()
    assert len(ring) == 2
    assert master_key_fingerprint(ring[1]) == master_key_fingerprint(MasterKey(_OLD))


def test_untagged_blob_returns_None_not_False() -> None:
    """None means "unknowable from the bytes", which is NOT the same as "no".

    Every blob written before v5 is untagged. Treating unknowable as "not on the primary"
    would make the sweep rewrite rows needlessly; treating it as "on the primary" would
    make it skip rows that must be re-wrapped. Both are wrong, so the third state exists.
    """

    new = MasterKey(_NEW)
    tagged = encrypt_signing_key(os.urandom(32), new)
    untagged = tagged[len(_ENCRYPTED_KEY_BLOB_V5) + _MASTER_KEY_FP_LEN :]

    assert blob_is_on_primary(tagged, new) is True
    assert blob_is_on_primary(untagged, new) is None
    assert blob_is_on_primary(encrypt_signing_key(b"x", MasterKey(_OLD)), new) is False


def test_ring_reports_which_key_opened_the_blob() -> None:
    """ "Opened by the previous key" is precisely the signal that a row needs re-wrapping."""

    new, old = MasterKey(_NEW), MasterKey(_OLD)
    raw = os.urandom(32)
    blob = encrypt_signing_key(raw, old)

    plaintext, opened_by = decrypt_with_ring(blob, [new, old])
    assert plaintext == raw
    assert master_key_fingerprint(opened_by) == master_key_fingerprint(old)


def test_no_key_opens_it_is_reported_not_guessed_at() -> None:
    new, old, alien = MasterKey(_NEW), MasterKey(_OLD), MasterKey(_ALIEN)
    blob = encrypt_signing_key(os.urandom(32), alien)

    with pytest.raises(NoKeyOpensBlobError) as excinfo:
        decrypt_with_ring(blob, [new, old])

    message = str(excinfo.value)
    assert master_key_fingerprint(alien) in message, "must name the fingerprint the blob records"
    assert "not necessarily corrupt" in message
    assert "Do NOT rewrite or rotate" in message, (
        "rotating on a decrypt failure is what destroyed the key twice on 2026-09-07"
    )


def test_no_key_opens_error_is_caught_by_existing_handlers() -> None:
    """Additive error contract, same rule #123 got wrong on its first attempt.

    A sibling exception would escape every ``except SigningKeyDecryptionError`` written to
    fail closed.
    """

    assert issubclass(NoKeyOpensBlobError, SigningKeyDecryptionError)

    blob = encrypt_signing_key(os.urandom(32), MasterKey(_ALIEN))
    with pytest.raises(SigningKeyDecryptionError):
        decrypt_with_ring(blob, [MasterKey(_NEW)])


def test_rewrap_moves_a_blob_to_the_primary_key() -> None:
    new, old = MasterKey(_NEW), MasterKey(_OLD)
    raw = os.urandom(32)

    moved = rewrap(encrypt_signing_key(raw, old), new, [new, old])
    assert moved is not None
    assert blob_is_on_primary(moved, new) is True
    assert decrypt_signing_key(moved, new) == raw


def test_rewrap_is_idempotent() -> None:
    """A second sweep must write nothing.

    Returning None for "already on the primary key" is what makes the sweep re-runnable.
    Without it, every run would rewrite every row -- churn on exactly the data least worth
    churning.
    """

    new = MasterKey(_NEW)
    already = encrypt_signing_key(os.urandom(32), new)
    assert rewrap(already, new, [new]) is None


def test_rewrap_refuses_a_blob_it_cannot_open() -> None:
    """It must raise, not return the original or a garbage blob.

    Silently returning something writable here is the failure that would corrupt a column
    during a rotation and only surface after the old key was retired.
    """

    new, alien = MasterKey(_NEW), MasterKey(_ALIEN)
    with pytest.raises(NoKeyOpensBlobError):
        rewrap(encrypt_signing_key(os.urandom(32), alien), new, [new])


def test_previous_key_is_never_used_for_wrapping() -> None:
    """PREVIOUS is decrypt-only. A rotation that kept writing to it would never finish."""

    new, old = MasterKey(_NEW), MasterKey(_OLD)
    moved = rewrap(encrypt_signing_key(os.urandom(32), old), new, [new, old])
    assert moved is not None
    head = len(_ENCRYPTED_KEY_BLOB_V5)
    tag = moved[head : head + _MASTER_KEY_FP_LEN]
    assert tag.hex() == master_key_fingerprint(new)
    assert tag.hex() != master_key_fingerprint(old)
