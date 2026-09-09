"""More than one master key at a time, so a rotation needs no downtime.

Without this, changing ``NCE_MASTER_KEY`` is a big-bang cutover: the instant the variable
changes, every blob wrapped under the old key is undecryptable, so the swap has to be
simultaneous across every process and every wrapped column -- and if anything is missed,
the loss is permanent and silent until something tries to read it.

With a ring, rotation becomes a sequence with no unsafe window:

1. set ``NCE_MASTER_KEY_PREVIOUS`` to the old key, ``NCE_MASTER_KEY`` to the new one;
2. restart -- old blobs still open under PREVIOUS, new writes are wrapped under the new key;
3. run ``scripts/rewrap_master_key.py --apply`` to re-wrap what is left;
4. when ``scripts/rewrap_master_key.py`` reports zero blobs off the primary key
   (``foreign_key_blob_count`` there), drop ``NCE_MASTER_KEY_PREVIOUS``.

Step 4 is the point of the counter: retiring the old key becomes evidence-based rather than
a guess. The 2026-09-07 incident happened because nobody could answer "which key opens
this?" -- v5 tags (#123) answer it per blob, and this module answers it per deployment.

PREVIOUS IS DECRYPT-ONLY. Nothing here ever wraps under it. A rotation that kept writing
under the old key would never finish.
"""

from __future__ import annotations

import logging

from nce.config import secret_env
from nce.signing import (
    _ENCRYPTED_KEY_BLOB_V5,
    _MASTER_KEY_FP_LEN,
    MasterKey,
    SigningKeyDecryptionError,
    decrypt_signing_key,
    master_key_fingerprint,
    master_key_fingerprint_bytes,
)

log = logging.getLogger("nce.master_key_ring")

_PREVIOUS_ENV = "NCE_MASTER_KEY_PREVIOUS"


class NoKeyOpensBlobError(SigningKeyDecryptionError):
    """No key on the ring decrypts this blob.

    Subclasses ``SigningKeyDecryptionError`` so existing fail-closed handlers still catch
    it -- the same additive-error-contract rule that #123 got wrong on its first attempt.

    This is a REAL state in this database, not a theoretical one: two retired signing keys
    are wrapped under ``tests/conftest.py``'s ``"x" * 32``, so no deployment key opens them.
    The sweep must report such rows and leave them untouched, never treat them as corrupt
    and never rewrite them.
    """


def previous_master_key() -> MasterKey | None:
    """The decrypt-only predecessor key, or None when no rotation is in progress."""

    raw = secret_env(_PREVIOUS_ENV, "").strip()
    if not raw:
        return None
    return MasterKey(raw.encode("utf-8"))


def master_key_ring() -> list[MasterKey]:
    """Every key that may open a blob, primary first.

    Order matters: the primary is tried first so the common path costs one derivation, and
    it is also the key everything is re-wrapped *to*.
    """

    ring = [MasterKey.from_env()]
    previous = previous_master_key()
    if previous is not None:
        ring.append(previous)
    return ring


def blob_is_on_primary(blob: bytes, primary: MasterKey) -> bool | None:
    """True/False if the blob is v5-tagged, None if it carries no key identity.

    None is not "no" -- it means unknowable from the bytes alone. Pre-v5 blobs are
    untagged, so the sweep has to attempt a decrypt to find out. Conflating the two would
    make the sweep either skip rows it must re-wrap or rewrite rows needlessly.
    """

    if not blob.startswith(_ENCRYPTED_KEY_BLOB_V5):
        return None
    head = len(_ENCRYPTED_KEY_BLOB_V5)
    recorded = blob[head : head + _MASTER_KEY_FP_LEN]
    return recorded == master_key_fingerprint_bytes(primary)


def decrypt_with_ring(blob: bytes, ring: list[MasterKey] | None = None) -> tuple[bytes, MasterKey]:
    """Decrypt *blob* with the first key on the ring that opens it.

    Returns ``(plaintext, key_that_opened_it)`` -- the caller needs to know *which* key
    worked, because "opened by the previous key" is exactly the signal that a row still
    needs re-wrapping.

    Raises :class:`NoKeyOpensBlobError` when none do, naming the fingerprint the blob
    records (when it has one) so an operator can go looking for the right key instead of
    guessing whether the data is corrupt.
    """

    keys = ring if ring is not None else master_key_ring()
    for key in keys:
        try:
            return decrypt_signing_key(blob, key), key
        except SigningKeyDecryptionError:
            continue

    recorded = ""
    if blob.startswith(_ENCRYPTED_KEY_BLOB_V5):
        head = len(_ENCRYPTED_KEY_BLOB_V5)
        recorded = (
            " The blob records master-key fingerprint "
            f"{blob[head : head + _MASTER_KEY_FP_LEN].hex()}, which is not on the ring."
        )
    tried = ", ".join(master_key_fingerprint(k) for k in keys) or "<none>"
    raise NoKeyOpensBlobError(
        f"no master key on the ring opens this blob (tried: {tried})."
        + recorded
        + " The data is not necessarily corrupt. Do NOT rewrite or rotate it -- find the key."
    )


def rewrap(blob: bytes, primary: MasterKey, ring: list[MasterKey] | None = None) -> bytes | None:
    """Re-wrap *blob* under *primary*, or None if it is already there.

    Returning None for "already on the primary key" keeps the sweep idempotent: a second
    run writes nothing. Verification that the new blob opens under the primary is the
    caller's job and must happen BEFORE the write -- see the sweep script.
    """

    from nce.signing import encrypt_signing_key

    if blob_is_on_primary(blob, primary) is True:
        return None

    plaintext, opened_by = decrypt_with_ring(blob, ring)
    if master_key_fingerprint(opened_by) == master_key_fingerprint(primary) and blob.startswith(
        _ENCRYPTED_KEY_BLOB_V5
    ):
        return None
    return encrypt_signing_key(plaintext, primary)
