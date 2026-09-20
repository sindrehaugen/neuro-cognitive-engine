"""Regression guard: a developer's ambient ``NCE_MCP_NAMESPACE_ID`` must never
leak into a test.

``nce.config.live_mcp_namespace_id()`` reads ``os.environ`` directly on every
call (deliberately, so a test's own ``monkeypatch.setenv`` still works), not
through ``cfg``. When a developer's shell has this var set -- pinning the
server to one tenant namespace, a normal thing to do for local manual
testing -- every test exercising ``enforce_mcp_tool_auth``'s tenant path gets
its own random ``namespace_id`` silently rejected by
``_bind_mcp_tenant_namespace`` with ``MCP_SCOPE_FORBIDDEN``, before the
handler under test ever runs. This bit ``tests/test_system_design_author_surface.py``
and ``tests/test_mcp_cache.py`` independently the same day, diagnosed each
time as a suspected regression rather than an inherited shell var.

``tests/conftest.py``'s autouse ``_reset_nce_cfg_singleton_after_test`` now
clears it via ``monkeypatch.delenv`` for the duration of every test. Two
directions, both required -- an over-aggressive clear that nothing can
override would silently break the tests that legitimately set this var
themselves (test_a2a_hardening.py, test_auth.py,
test_config_prod_hardening.py, test_secrets_provider_seam.py,
test_rest_cache_invalidation.py, unit/test_auth_file_secret_resolution.py):

  1. Simulating an ambient shell value, a test that does nothing else must
     see it cleared.
  2. A test that explicitly sets the var itself must still see its own value,
     proving the autouse clear does not shadow a same-test override.
"""

from __future__ import annotations

import os

import pytest

from nce.config import live_mcp_namespace_id

_AMBIENT_NAMESPACE = "00000000-0000-4000-8000-000000000001"


def test_ambient_shell_namespace_id_is_cleared_before_this_test_runs() -> None:
    """No test should ever observe a value it did not set itself.

    This does not simulate the ambient var being set -- the autouse fixture
    has already run and cleared it by the time this test body executes.
    Asserting it is unset here is the actual regression guard: if the
    autouse ``monkeypatch.delenv`` call in conftest.py is ever removed or
    reordered past this test's own setup, this fails immediately, with no
    database and no live Postgres required.
    """
    assert os.environ.get("NCE_MCP_NAMESPACE_ID", "") == ""
    assert live_mcp_namespace_id() == ""


def test_explicit_setenv_in_a_test_still_wins_over_the_autouse_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that legitimately wants the var set must still be able to.

    Proves the autouse clear is not over-aggressive: the same monkeypatch
    fixture instance the autouse fixture used to delenv is the one this test
    body calls setenv on, and pytest's monkeypatch stacks changes rather than
    reverting between fixtures within one test -- so this must observe the
    value this test set, not the cleared state from setup.
    """
    monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", _AMBIENT_NAMESPACE)
    assert live_mcp_namespace_id() == _AMBIENT_NAMESPACE


def test_clearing_does_not_leak_across_tests() -> None:
    """Run after the setenv test above (module order) to prove no leak.

    If monkeypatch's automatic per-test reversal were broken, this would
    inherit the previous test's explicitly-set value instead of seeing the
    autouse clear apply fresh for this test.
    """
    assert os.environ.get("NCE_MCP_NAMESPACE_ID", "") == ""
    assert live_mcp_namespace_id() == ""
