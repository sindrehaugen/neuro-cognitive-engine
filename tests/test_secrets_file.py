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
