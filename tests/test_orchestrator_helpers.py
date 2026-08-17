"""
Unit tests for NCEEngine helper methods.

These tests are fully isolated — no live DB, no network, no async fixtures.
They reproduce and guard against the P0 scoping bug where `_ensure_uuid`
returned None for string inputs (the `return UUID(str(val))` line was
erroneously placed inside `_warn_connect_not_called`).

Covering:
  - _ensure_uuid: None → None
  - _ensure_uuid: UUID → same UUID (no copy)
  - _ensure_uuid: str UUID → UUID object
  - _ensure_uuid: invalid str → ValueError (not swallowed)
  - _warn_connect_not_called: logs correctly and returns None (no NameError)
  - scoped_session guard: string namespace_id produces a proper UUID,
    not the literal string "None" that would break RLS policy
"""

import logging
from uuid import UUID, uuid4

import pytest

from nce.orchestrator import NCEEngine


@pytest.fixture
def engine() -> NCEEngine:
    """Return an unconnected engine (no DB required)."""
    return NCEEngine()


# ---------------------------------------------------------------------------
# _ensure_uuid — unit tests
# ---------------------------------------------------------------------------


class TestEnsureUuid:
    def test_none_returns_none(self, engine: NCEEngine) -> None:
        """_ensure_uuid(None) must return None."""
        assert engine._ensure_uuid(None) is None

    def test_uuid_object_passes_through(self, engine: NCEEngine) -> None:
        """_ensure_uuid(UUID) must return the same UUID object."""
        uid = uuid4()
        result = engine._ensure_uuid(uid)
        assert result == uid
        assert isinstance(result, UUID)

    def test_string_uuid_converts_to_uuid(self, engine: NCEEngine) -> None:
        """_ensure_uuid(str) must parse and return a UUID — not None.
        This is the P0 regression guard: before the fix this branch fell
        through and returned None implicitly.
        """
        uid = uuid4()
        result = engine._ensure_uuid(str(uid))
        assert isinstance(result, UUID), (
            f"_ensure_uuid returned {result!r} instead of UUID — "
            "the P0 scoping bug may have been reintroduced"
        )
        assert result == uid

    def test_string_uuid_is_not_none_literal(self, engine: NCEEngine) -> None:
        """The RLS regression guard: the result must never be the Python
        object None when a string is supplied, because scoped_session passes
        the result to set_namespace_context — None would become the string
        'None' in the Postgres session variable and silently empty every
        tenant's result set.
        """
        uid = uuid4()
        result = engine._ensure_uuid(str(uid))
        assert result is not None, (
            "_ensure_uuid returned None for a string UUID input — "
            "this would cause RLS to receive 'None' as namespace_id, "
            "breaking tenant isolation silently."
        )

    def test_invalid_string_raises_value_error(self, engine: NCEEngine) -> None:
        """_ensure_uuid with a non-UUID string must raise ValueError,
        not silently swallow the error or return None.
        """
        with pytest.raises(ValueError):
            engine._ensure_uuid("not-a-uuid")


# ---------------------------------------------------------------------------
# _warn_connect_not_called — must log and return None without NameError
# ---------------------------------------------------------------------------


class TestWarnConnectNotCalled:
    def test_returns_none(self, engine: NCEEngine) -> None:
        """_warn_connect_not_called must return None (no stray return value).
        Before the fix, it contained `return UUID(str(val))` where `val`
        was not in scope — this raised NameError on every lazy-init path.
        """
        result = engine._warn_connect_not_called("some_method")
        assert result is None

    def test_emits_warning_log(self, engine: NCEEngine, caplog: pytest.LogCaptureFixture) -> None:
        """_warn_connect_not_called must emit a WARNING-level log message."""
        with caplog.at_level(logging.WARNING, logger="nce-orchestrator"):
            engine._warn_connect_not_called("test_method")
        assert any(
            "test_method" in record.message and record.levelno == logging.WARNING
            for record in caplog.records
        ), f"No warning log found for 'test_method'. Records: {caplog.records}"

    def test_no_name_error_raised(self, engine: NCEEngine) -> None:
        """The stray `return UUID(str(val))` caused NameError before the fix.
        This test guards against regression.
        """
        try:
            engine._warn_connect_not_called("lazy_init_regression_guard")
        except NameError as exc:
            pytest.fail(
                f"_warn_connect_not_called raised NameError — "
                f"the P0 stranded-return bug may be back: {exc}"
            )
