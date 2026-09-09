"""A structurally guessable master key must not reach production.

`NCE_MASTER_KEY` wraps every signing key, memory DEK and stored credential, so a guessable
one means the encryption provides no confidentiality AND event signatures are forgeable --
the Merkle chain and the WORM trigger end up protecting a ledger anyone can re-sign.

This estate ran `0123456789abcdef` repeated to 42 characters in dev for months, and the
existing validator passed it because that check is length-and-invisible-characters only.
"""

from __future__ import annotations

import secrets

import pytest

from nce.config import (
    _fail_unless_nce_master_key_ok,
    _master_key_structural_weakness,
)

# The key this estate actually ran. Kept as a literal on purpose: it is a published
# placeholder with no secrecy value, and the guard's whole justification is that this
# specific shape used to pass.
_DEV_PLACEHOLDER = "0123456789abcdef0123456789abcdef0123456789"


def test_shannon_entropy_would_not_have_caught_it() -> None:
    """The reason this guard tests STRUCTURE and not entropy.

    The placeholder is uniform over 16 hex symbols, so its Shannon entropy is ~3.97
    bits/char -- statistically indistinguishable from `openssl rand -hex 21`. An entropy
    guard built the obvious way would have passed it. Pinning this so nobody later
    "simplifies" the detector into a Shannon threshold and reopens the hole.
    """

    import collections
    import math

    counts = collections.Counter(_DEV_PLACEHOLDER)
    n = len(_DEV_PLACEHOLDER)
    shannon = -sum(c / n * math.log2(c / n) for c in counts.values())

    assert shannon > 3.9, (
        f"the placeholder's Shannon entropy is {shannon:.2f} bits/char -- high. If a future "
        "detector relies on entropy alone it will pass this key again."
    )
    assert _master_key_structural_weakness(_DEV_PLACEHOLDER) is not None, (
        "the structural detector must catch what entropy cannot"
    )


def test_the_actual_dev_key_is_detected() -> None:
    weakness = _master_key_structural_weakness(_DEV_PLACEHOLDER)
    assert weakness is not None
    assert "pattern repeated" in weakness


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("single character repeated", "a" * 40),
        ("two-character pattern", "ab" * 20),
        ("ascending run", "".join(chr(33 + i) for i in range(40))),
        ("placeholder word", "changeme" + "f3a9" * 8),
    ],
)
def test_weak_shapes_are_detected(label: str, key: str) -> None:
    assert _master_key_structural_weakness(key) is not None, f"missed: {label}"


@pytest.mark.parametrize("generator", ["token_hex", "token_urlsafe"])
def test_real_key_material_is_not_rejected(generator: str) -> None:
    """No false positives on the commands an operator would actually run.

    A guard that rejects `openssl rand -hex 32` gets disabled within a day, and then it
    protects nothing. Repeated so a single unlucky draw cannot make this pass or fail by
    chance.
    """

    make = getattr(secrets, generator)
    for _ in range(50):
        key = make(32)
        assert _master_key_structural_weakness(key) is None, (
            f"{generator} produced a key the detector calls weak -- false positives will "
            "get this guard switched off"
        )


def test_the_message_never_contains_the_key() -> None:
    """A guard that logs the secret it protects is worse than no guard.

    The weakness string is written to a WARNING in dev and into a RuntimeError in
    production, so it must describe the shape without quoting any of it.
    """

    for key in (_DEV_PLACEHOLDER, "z" * 40, "changeme" + "0" * 32):
        weakness = _master_key_structural_weakness(key)
        assert weakness is not None
        assert key not in weakness
        # Nor any run long enough to be a useful crib.
        for start in range(0, len(key) - 8):
            assert key[start : start + 8] not in weakness, (
                "the message leaks an 8-character run of the key"
            )


def test_advisory_in_dev_fatal_in_production() -> None:
    """Posture gate: a developer's throwaway key must not brick their stack.

    Same shape the schema-skew and master-key-mismatch checks already use -- report
    outside production, refuse inside it. Without the dev half this lands as an
    unbootable stack for everyone currently running a placeholder.
    """

    # dev: advisory only
    _fail_unless_nce_master_key_ok(_DEV_PLACEHOLDER, is_prod=False)

    # production: refuses, and says why
    with pytest.raises(RuntimeError) as excinfo:
        _fail_unless_nce_master_key_ok(_DEV_PLACEHOLDER, is_prod=True)
    message = str(excinfo.value)
    assert "structurally weak" in message
    assert "pattern repeated" in message
    assert _DEV_PLACEHOLDER not in message, "the fatal error must not leak the key either"

    # a real key passes in production
    _fail_unless_nce_master_key_ok(secrets.token_urlsafe(48), is_prod=True)


def test_the_default_is_dev_safe() -> None:
    """`is_prod` defaults to False so existing callers keep working.

    The signature change is additive on purpose. Making the strict path the default would
    have broken every existing caller and test that calls this validator directly -- the
    same mistake as raising an exception outside the family everyone catches.
    """

    _fail_unless_nce_master_key_ok(_DEV_PLACEHOLDER)


def test_length_and_invisible_character_checks_still_apply() -> None:
    """The new check is additive: the pre-existing guards must still fire."""

    with pytest.raises(RuntimeError, match="missing or too short"):
        _fail_unless_nce_master_key_ok("short", is_prod=False)

    with pytest.raises(RuntimeError, match="invisible control"):
        _fail_unless_nce_master_key_ok("﻿" + secrets.token_urlsafe(48), is_prod=False)
