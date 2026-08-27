"""File-based secrets (``*_FILE`` convention) — Batch 118.

Asserts that :func:`nce.config.secret_env` (and ``signing.require_master_key``,
which resolves through it) support the Docker/K8s ``<NAME>_FILE`` convention so
secrets need not live in the process environment (closing the
``/proc/<pid>/environ`` exposure vector):

  * ``NCE_X_FILE`` pointing at a file loads the secret from that file;
  * the plain ``NCE_X`` env var remains a backward-compatible fallback;
  * ``*_FILE`` takes precedence over ``NCE_X`` when BOTH are set;
  * a single trailing newline in the file is stripped;
  * a missing/unreadable ``*_FILE`` fails closed with a clear, non-secret error;
  * the secret value never appears in logs / error messages (redaction).

These are unit-level (no Docker): they exercise ``secret_env`` directly and the
``MasterKey.from_env`` resolution path, using ``monkeypatch`` + ``tmp_path``.
"""

from __future__ import annotations

import logging

import pytest

from nce.config import secret_env
from nce.signing import require_master_key

# A master key long enough to satisfy the >= 32 UTF-8 byte floor.
_MASTER_KEY_VALUE = "file-sourced-master-key-0123456789-ABCDEF"
_API_KEY_VALUE = "file-sourced-api-key-value"


# ---------------------------------------------------------------------------
# secret_env: file resolution, fallback, precedence
# ---------------------------------------------------------------------------


def test_file_var_is_honored(monkeypatch, tmp_path) -> None:
    """NCE_X_FILE pointing at a temp file loads the secret from the file."""
    secret_file = tmp_path / "api_key"
    secret_file.write_text(_API_KEY_VALUE, encoding="utf-8")

    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    assert secret_env("NCE_API_KEY", "default-unused") == _API_KEY_VALUE


def test_env_fallback_when_file_unset(monkeypatch) -> None:
    """With no *_FILE set, the plain env var is used (backward compatible)."""
    monkeypatch.delenv("NCE_API_KEY_FILE", raising=False)
    monkeypatch.setenv("NCE_API_KEY", "from-env")

    assert secret_env("NCE_API_KEY", "default") == "from-env"


def test_default_when_neither_set(monkeypatch) -> None:
    """With neither *_FILE nor the plain var set, the default is returned."""
    monkeypatch.delenv("NCE_API_KEY_FILE", raising=False)
    monkeypatch.delenv("NCE_API_KEY", raising=False)

    assert secret_env("NCE_API_KEY", "the-default") == "the-default"


def test_file_takes_precedence_over_env(monkeypatch, tmp_path) -> None:
    """When BOTH the file and the plain env var are set, the file wins."""
    secret_file = tmp_path / "api_key"
    secret_file.write_text(_API_KEY_VALUE, encoding="utf-8")

    monkeypatch.setenv("NCE_API_KEY", "from-env-should-be-ignored")
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    assert secret_env("NCE_API_KEY", "default") == _API_KEY_VALUE


def test_single_trailing_newline_stripped(monkeypatch, tmp_path) -> None:
    """Exactly one trailing newline (editor / tooling artifact) is stripped.

    Uses ``write_bytes`` so the on-disk bytes are exact (Windows text mode would
    otherwise translate ``\\n`` to ``\\r\\n`` on write).
    """
    secret_file = tmp_path / "api_key"
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    secret_file.write_bytes((_API_KEY_VALUE + "\n").encode("utf-8"))
    assert secret_env("NCE_API_KEY") == _API_KEY_VALUE

    # \r\n is also stripped as a single trailing newline.
    secret_file.write_bytes((_API_KEY_VALUE + "\r\n").encode("utf-8"))
    assert secret_env("NCE_API_KEY") == _API_KEY_VALUE

    # Only ONE trailing newline is removed; internal/extra ones are preserved.
    secret_file.write_bytes((_API_KEY_VALUE + "\n\n").encode("utf-8"))
    assert secret_env("NCE_API_KEY") == _API_KEY_VALUE + "\n"


def test_blank_file_var_falls_back_to_env(monkeypatch) -> None:
    """An empty / whitespace *_FILE value is treated as unset (env fallback)."""
    monkeypatch.setenv("NCE_API_KEY_FILE", "   ")
    monkeypatch.setenv("NCE_API_KEY", "from-env")
    assert secret_env("NCE_API_KEY", "default") == "from-env"


# ---------------------------------------------------------------------------
# Fail-closed on a missing / unreadable file — never leaks the secret
# ---------------------------------------------------------------------------


def test_missing_file_raises_clear_non_secret_error(monkeypatch, tmp_path) -> None:
    """A *_FILE that points at a non-existent path fails closed with a clear error."""
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("NCE_API_KEY_FILE", str(missing))
    monkeypatch.setenv("NCE_API_KEY", "env-value-must-not-rescue")

    with pytest.raises(RuntimeError) as exc_info:
        secret_env("NCE_API_KEY", "default")

    message = str(exc_info.value)
    # The error must name the env var + path (operator can act). The path is
    # repr()'d in the message (backslashes escaped on Windows), so assert on the
    # filename component, which is stable across platforms / quoting.
    assert "NCE_API_KEY_FILE" in message
    assert missing.name in message
    # ... and must NOT fall back to or leak any secret value.
    assert "env-value-must-not-rescue" not in message


# ---------------------------------------------------------------------------
# signing.require_master_key honors NCE_MASTER_KEY_FILE
# ---------------------------------------------------------------------------


def test_require_master_key_honors_file(monkeypatch, tmp_path) -> None:
    """The master key is loaded from NCE_MASTER_KEY_FILE when set."""
    key_file = tmp_path / "master_key"
    key_file.write_text(_MASTER_KEY_VALUE + "\n", encoding="utf-8")

    monkeypatch.delenv("NCE_MASTER_KEY", raising=False)
    monkeypatch.setenv("NCE_MASTER_KEY_FILE", str(key_file))

    with require_master_key() as mk:
        assert bytes(mk.key_bytes) == _MASTER_KEY_VALUE.encode("utf-8")


def test_require_master_key_file_takes_precedence(monkeypatch, tmp_path) -> None:
    """NCE_MASTER_KEY_FILE wins over NCE_MASTER_KEY when both are set."""
    key_file = tmp_path / "master_key"
    key_file.write_text(_MASTER_KEY_VALUE, encoding="utf-8")

    monkeypatch.setenv("NCE_MASTER_KEY", "x" * 40)  # valid env key, must be ignored
    monkeypatch.setenv("NCE_MASTER_KEY_FILE", str(key_file))

    with require_master_key() as mk:
        assert bytes(mk.key_bytes) == _MASTER_KEY_VALUE.encode("utf-8")


def test_require_master_key_env_fallback(monkeypatch) -> None:
    """With no *_FILE set, require_master_key falls back to the env var."""
    monkeypatch.delenv("NCE_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("NCE_MASTER_KEY", _MASTER_KEY_VALUE)

    with require_master_key() as mk:
        assert bytes(mk.key_bytes) == _MASTER_KEY_VALUE.encode("utf-8")


def test_require_master_key_missing_file_fails_closed(monkeypatch, tmp_path) -> None:
    """A NCE_MASTER_KEY_FILE pointing at a missing path fails closed."""
    missing = tmp_path / "nope"
    monkeypatch.setenv("NCE_MASTER_KEY_FILE", str(missing))
    monkeypatch.delenv("NCE_MASTER_KEY", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        require_master_key()
    assert "NCE_MASTER_KEY_FILE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# The secret value never appears in logs
# ---------------------------------------------------------------------------


def test_secret_value_never_logged(monkeypatch, tmp_path, caplog) -> None:
    """Reading a file-based secret emits no log line containing the secret."""
    secret_file = tmp_path / "master_key"
    secret_file.write_text(_MASTER_KEY_VALUE, encoding="utf-8")
    monkeypatch.delenv("NCE_MASTER_KEY", raising=False)
    monkeypatch.setenv("NCE_MASTER_KEY_FILE", str(secret_file))

    with caplog.at_level(logging.DEBUG):
        value = secret_env("NCE_MASTER_KEY")
        with require_master_key():
            pass

    # The helper returned the real secret (sanity) ...
    assert value == _MASTER_KEY_VALUE
    # ... but it must not have been written to any log record.
    for record in caplog.records:
        assert _MASTER_KEY_VALUE not in record.getMessage()
    assert _MASTER_KEY_VALUE not in caplog.text


def test_missing_file_error_is_not_logged_with_secret(monkeypatch, tmp_path, caplog) -> None:
    """The fail-closed error path logs no secret (there is none to read anyway)."""
    missing = tmp_path / "absent"
    monkeypatch.setenv("NCE_MASTER_KEY_FILE", str(missing))
    monkeypatch.setenv("NCE_MASTER_KEY", "env-secret-should-not-appear-anywhere!!")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError):
            secret_env("NCE_MASTER_KEY")

    assert "env-secret-should-not-appear-anywhere!!" not in caplog.text


# ---------------------------------------------------------------------------
# UTF-8 BOM at the read boundary — a malformed secret must not become a
# silently-wrong key (found 2026-08-18 on the production compose path).
#
# ``deploy/secrets/*`` written by Windows PowerShell 5.1 (``Out-File -Encoding
# utf8``) carry a 3-byte ``EF BB BF`` BOM.  The ``utf-8`` codec decodes that to
# U+FEFF and KEEPS it (only ``utf-8-sig`` strips it), and U+FEFF is not
# whitespace in Python, so ``.strip()`` never removes it.  The result was a
# 65-character master key where 64 was intended — a wrong key produced
# silently, the same failure shape as the June 2026 signing P0.
# ---------------------------------------------------------------------------

_BOM = "\ufeff"


def test_utf8_bom_is_stripped_from_file_secret(monkeypatch, tmp_path) -> None:
    """A BOM-prefixed secret file must not yield a U+FEFF-prefixed secret.

    This is the regression test for the live bug: on unfixed code the loaded
    value is ``"\ufeff" + _API_KEY_VALUE`` (one character longer than the
    file's visible content), which silently becomes a different key.
    """
    secret_file = tmp_path / "api_key"
    # Exact on-disk bytes: BOM + value, no trailing newline (mirrors the real
    # deploy/secrets/* files, which are 3 + 64 = 67 bytes).
    secret_file.write_bytes(b"\xef\xbb\xbf" + _API_KEY_VALUE.encode("utf-8"))

    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    loaded = secret_env("NCE_API_KEY")

    assert not loaded.startswith(_BOM), (
        "secret_env leaked a U+FEFF BOM into the secret value — the loaded "
        "secret differs from the file's visible content by an invisible "
        "leading character, silently producing a wrong key."
    )
    assert loaded == _API_KEY_VALUE
    assert len(loaded) == len(_API_KEY_VALUE)


def test_utf8_bom_strip_is_logged_at_warning(monkeypatch, tmp_path, caplog) -> None:
    """Stripping a BOM must be loud: an operator has a malformed secret file."""
    secret_file = tmp_path / "api_key"
    secret_file.write_bytes(b"\xef\xbb\xbf" + _API_KEY_VALUE.encode("utf-8"))

    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    # The warning is deduplicated per (var, path); clear the ledger so this
    # test does not depend on execution order (tmp_path is already unique,
    # but this makes the test self-contained under pytest-randomly).
    from nce.config import _BOM_WARNED

    _BOM_WARNED.clear()

    with caplog.at_level(logging.WARNING, logger="nce-config"):
        loaded = secret_env("NCE_API_KEY")

    assert loaded == _API_KEY_VALUE
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "stripping a BOM from a secret file must log at WARNING"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "NCE_API_KEY_FILE" in joined, "the warning must name the offending env var"
    # The warning must never echo the secret itself.
    assert _API_KEY_VALUE not in joined


def test_bom_does_not_survive_into_master_key(monkeypatch, tmp_path) -> None:
    """``MasterKey.from_env`` must not derive from BOM-contaminated material.

    ``from_env`` calls ``.strip()``, which does NOT remove U+FEFF, so on
    unfixed code the 3 BOM bytes are fed straight into the Argon2id/PBKDF2
    derivation and every DEK is wrapped under the wrong key.
    """
    secret_file = tmp_path / "master_key"
    secret_file.write_bytes(b"\xef\xbb\xbf" + _MASTER_KEY_VALUE.encode("utf-8"))

    monkeypatch.delenv("NCE_MASTER_KEY", raising=False)
    monkeypatch.setenv("NCE_MASTER_KEY_FILE", str(secret_file))

    with require_master_key() as mk:
        key_bytes = bytes(mk.key_bytes)

    assert not key_bytes.startswith(b"\xef\xbb\xbf"), (
        "BOM bytes reached the key-derivation input — the derived DEK-wrapping "
        "key differs from the one the file's visible content implies."
    )
    assert key_bytes == _MASTER_KEY_VALUE.encode("utf-8")


# ---------------------------------------------------------------------------
# Fail-closed at STARTUP on a malformed master key
#
# The real defect is not the BOM: it is that a malformed secret produces a
# *wrong key* rather than an error.  ``_fail_unless_nce_master_key_ok`` runs at
# import of ``nce.config`` and again from ``cfg.validate()``, so a key that is
# malformed on its face must be rejected there — not survive to a decrypt
# failure later, where it is indistinguishable from data corruption.
#
# Note the pre-existing floor (>= 32 UTF-8 bytes) does NOT catch this: a
# BOM-prefixed 64-char key is 67 UTF-8 bytes and sails through.
# ---------------------------------------------------------------------------


def test_bom_prefixed_master_key_rejected_at_startup() -> None:
    """A U+FEFF-carrying master key must raise at the startup guard."""
    from nce.config import _fail_unless_nce_master_key_ok

    with pytest.raises(RuntimeError) as exc_info:
        _fail_unless_nce_master_key_ok(_BOM + _MASTER_KEY_VALUE)

    message = str(exc_info.value)
    assert _MASTER_KEY_VALUE not in message, "the guard must never echo the key"


def test_bom_master_key_passes_the_old_length_floor() -> None:
    """Pin WHY the pre-existing guard missed this: length alone is not enough.

    If this ever fails, the length floor has changed and the BOM check is no
    longer the only thing standing between a BOM'd key and silent acceptance.
    """
    from nce.config import _MASTER_KEY_MIN_UTF8_BYTES

    contaminated = _BOM + _MASTER_KEY_VALUE
    assert len(contaminated.encode("utf-8")) >= _MASTER_KEY_MIN_UTF8_BYTES
    # ...and U+FEFF is not whitespace, so .strip() cannot save us.
    assert contaminated.strip().startswith(_BOM)


def test_control_character_master_key_rejected_at_startup() -> None:
    """Other invisible encoding artifacts fail closed too (not just U+FEFF)."""
    from nce.config import _fail_unless_nce_master_key_ok

    # Invisible characters that survive .strip() (U+2028 / U+00A0 do not,
    # so they are correctly NOT in this list).
    for bad in ("\x00", "\u200b", "\u200e", "\u00ad"):
        with pytest.raises(RuntimeError):
            _fail_unless_nce_master_key_ok(bad + _MASTER_KEY_VALUE)


def test_wellformed_master_key_still_accepted() -> None:
    """The guard must not reject legitimate keys (no false positives).

    Covers the shapes actually used across this repo: hex, ASCII-punctuation
    dev keys, and the >= 32-byte floor exactly.
    """
    from nce.config import _fail_unless_nce_master_key_ok

    for good in (
        "0" * 64,
        "09f33" + "a" * 59,
        "dev-trimcp-master-key-change-in-prod-32chars!!",
        "x" * 32,
        _MASTER_KEY_VALUE,
        "  " + _MASTER_KEY_VALUE + "\n",  # surrounding real whitespace is fine
    ):
        _fail_unless_nce_master_key_ok(good)


def test_internal_whitespace_master_key_still_accepted() -> None:
    """A key with INTERNAL whitespace must still be accepted.

    Regression guard. ``.strip()`` only removes LEADING/TRAILING whitespace, so
    an internal newline survives into the Cc/Cf scan -- and LF/CR/TAB are all
    category Cc. A naive "reject all Cc/Cf" rule therefore rejects
    ``openssl rand -base64 64``, whose output base64-wraps at 64 columns and so
    legitimately contains a newline. That would raise at ``nce.config`` import
    and the server would refuse to start on a key ``main`` accepted -- a worse
    outcome than the BOM bug this guard exists to catch.

    The rule must reject only invisibles that are NOT whitespace.
    """
    from nce.config import _fail_unless_nce_master_key_ok

    b64_wrapped = (
        "dEmRXuAPeMUuK8/kTOaOUgBGt3UGksMJ/2n8BXHbOiXdYpG/YQVSuwrIu12IQEJJ\nXT0siLl470K22p3HjfSk1w=="
    )
    for good in (
        b64_wrapped,  # openssl rand -base64 64, as emitted
        "0" * 32 + "\n" + "0" * 32,  # hex wrapped across two lines
        "0" * 32 + "\r\n" + "0" * 32,  # ...with CRLF (Windows)
        "abcdefgh" * 4 + "\t" + "ijklmnop" * 4,  # internal tab
    ):
        _fail_unless_nce_master_key_ok(good)


def test_internal_invisible_nonspace_still_rejected() -> None:
    """Narrowing to non-whitespace must not weaken the BOM catch.

    The invisibles that actually caused the incident are not whitespace, so
    every one of them must still fail closed even when it appears INTERNALLY
    (where ``.strip()`` cannot reach it).
    """
    from nce.config import _fail_unless_nce_master_key_ok

    for bad in ("\ufeff", "\x00", "\u200b", "\u200e", "\u00ad"):
        with pytest.raises(RuntimeError):
            _fail_unless_nce_master_key_ok("0" * 32 + bad + "0" * 32)


# ---------------------------------------------------------------------------
# Closing the rest of the read-boundary class.
#
# The first pass guarded only the ``*_FILE`` branch against a single UTF-8 BOM.
# Three sibling shapes of the SAME operator mistake were still unguarded. All
# three were verified against the pre-fix code before these tests were written.
#
# Note what PowerShell 5.1 actually does with the recipe the compose file used
# to document (``printf '%s' "$SECRET" > file``):
#     >  redirection            -> UTF-16LE + BOM  (ff fe)   <- most literal port
#     Out-File -Encoding utf8   -> UTF-8    + BOM  (ef bb bf) <- what bit us
#     Set-Content (default)     -> clean, no BOM
# So the UTF-16 case is not exotic; it is the likelier translation of the two.
# ---------------------------------------------------------------------------


def test_bom_in_plain_env_var_is_stripped(monkeypatch) -> None:
    """A BOM in the plain env var must be stripped too, not just in a *_FILE.

    The env branch was untouched by the first fix, so a BOM'd ``NCE_API_KEY``
    (e.g. from an env_file written by PowerShell) silently produced a 65-char
    secret. Only NCE_MASTER_KEY was protected, and only because the startup
    guard rejects U+FEFF -- the other ten secrets resolved through secret_env
    had no such backstop.
    """
    monkeypatch.delenv("NCE_API_KEY_FILE", raising=False)
    monkeypatch.setenv("NCE_API_KEY", _BOM + _API_KEY_VALUE)

    loaded = secret_env("NCE_API_KEY")

    assert not loaded.startswith(_BOM), "BOM survived the plain-env branch"
    assert loaded == _API_KEY_VALUE


def test_utf16_secret_file_fails_closed_with_clear_error(monkeypatch, tmp_path) -> None:
    """A UTF-16 secret file must fail closed the way the docstring promises.

    ``read_text(encoding="utf-8")`` on UTF-16 raises UnicodeDecodeError, which is
    a ValueError -- not the documented RuntimeError naming the env var and path,
    and it escaped the ``except OSError`` handler entirely.
    """
    secret_file = tmp_path / "utf16_key"
    secret_file.write_bytes(b"\xff\xfe" + _API_KEY_VALUE.encode("utf-16-le"))

    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    with pytest.raises(RuntimeError) as exc_info:
        secret_env("NCE_API_KEY")

    message = str(exc_info.value)
    assert "NCE_API_KEY_FILE" in message, "the error must name the env var"
    assert "UTF-16" in message or "utf-16" in message, "the error should name the detected encoding"
    # Never echo the secret, even partially decoded.
    assert _API_KEY_VALUE not in message


def test_non_utf8_secret_file_fails_closed(monkeypatch, tmp_path) -> None:
    """Arbitrary non-UTF-8 bytes must also raise the documented RuntimeError."""
    secret_file = tmp_path / "binary_key"
    secret_file.write_bytes(b"\x80\x81\x82\x83" * 8)

    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    with pytest.raises(RuntimeError) as exc_info:
        secret_env("NCE_API_KEY")
    assert "NCE_API_KEY_FILE" in str(exc_info.value)


def test_stacked_boms_are_all_stripped(monkeypatch, tmp_path) -> None:
    """Repeated BOMs must all go -- the warning claims "the BOM has been stripped".

    Stripping exactly one left a second U+FEFF in the secret while telling the
    operator it had been dealt with: a wrong key AND a misleading log line.
    """
    secret_file = tmp_path / "double_bom"
    secret_file.write_bytes(b"\xef\xbb\xbf" * 3 + _API_KEY_VALUE.encode("utf-8"))

    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    loaded = secret_env("NCE_API_KEY")

    assert _BOM not in loaded, "a stacked BOM survived"
    assert loaded == _API_KEY_VALUE


def test_bom_strip_still_preserves_trailing_newline_handling(monkeypatch, tmp_path) -> None:
    """BOM stripping must compose with the pre-existing trailing-newline rule."""
    secret_file = tmp_path / "bom_and_newline"
    monkeypatch.delenv("NCE_API_KEY", raising=False)
    monkeypatch.setenv("NCE_API_KEY_FILE", str(secret_file))

    secret_file.write_bytes(b"\xef\xbb\xbf" + (_API_KEY_VALUE + "\r\n").encode("utf-8"))
    assert secret_env("NCE_API_KEY") == _API_KEY_VALUE

    # Only ONE trailing newline is removed, BOM or not.
    secret_file.write_bytes(b"\xef\xbb\xbf" + (_API_KEY_VALUE + "\n\n").encode("utf-8"))
    assert secret_env("NCE_API_KEY") == _API_KEY_VALUE + "\n"
