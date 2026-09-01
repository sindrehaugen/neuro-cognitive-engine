"""
tests/unit/test_auth_file_secret_resolution.py
==============================================
The ``*_FILE`` secret indirection did not reach the LIVE auth path.

``nce/config.py`` declares both API keys with ``secret_env(...)``, which
resolves ``NCE_X_FILE`` first (fail-closed) so the secret never has to sit in
``/proc/<pid>/environ``. But the *live* accessors the auth path actually calls
-- ``live_mcp_api_key()`` and ``live_admin_api_key()`` -- went through
``live_env_str()`` -> ``os.getenv``, which knows nothing about ``*_FILE``.

**The severity is an availability trap, not a bypass.** With
``NCE_MCP_API_KEY_FILE`` mounted and ``NCE_MCP_API_KEY`` unset,
``enforce_mcp_tool_auth`` read ``""`` -> falsy -> no injection ->
``_validate_scope("tenant", ...)`` ran with no key and **refused**. So it fails
CLOSED. The symptom is *"MCP tenant tools stop answering the moment you mount
secrets the way the docs tell you to"*, with no obvious cause. It is not a
breach, and it must not be written up as one.

**The plan for this fix said it was "exactly ONE site", ``auth.py``'s direct
``os.environ.get``. That was true only for DIRECT reads.** The live accessors
are the indirect consumers, and they are what the auth path calls -- so the
plan's own transferable lesson ("a ``*_FILE``-aware declaration does not mean
the consumer reads it -- check the consumer") had to be applied one level
deeper than the plan applied it. Fixing the accessors fixes every consumer at
once; fixing only the one call site would have left ``live_admin_api_key()``
still blind.

**D30 is NOT re-opened here.** The key injection in ``enforce_mcp_tool_auth``
is deliberate: PR #137 (B67P) closed D30 by decision, documenting MCP stdio as
a trusted local pipe. The goal is to make ``*_FILE`` *work*, not to remove the
injection. ``test_the_d30_injection_is_still_present`` is the ratchet that
fails if a later change deletes it.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

import pytest

from nce import auth as nce_auth
from nce.config import live_admin_api_key, live_mcp_api_key

_SECRET = "s3cret-from-a-mounted-file"
_PLAIN = "plain-value-from-the-environment"


def _write(tmp_path: Any, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 1. The live accessors must resolve *_FILE -- RED before the fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("accessor", "var"),
    [
        (live_mcp_api_key, "NCE_MCP_API_KEY"),
        (live_admin_api_key, "NCE_ADMIN_API_KEY"),
    ],
    ids=["mcp", "admin"],
)
def test_live_accessor_reads_the_file_when_only_file_var_is_set(
    accessor: Any, var: str, tmp_path: Any, monkeypatch: Any
) -> None:
    """This is the whole defect: only ``*_FILE`` is mounted, as the docs say."""
    monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(f"{var}_FILE", _write(tmp_path, f"{var}.secret", _SECRET))
    assert accessor() == _SECRET


@pytest.mark.parametrize(
    ("accessor", "var"),
    [
        (live_mcp_api_key, "NCE_MCP_API_KEY"),
        (live_admin_api_key, "NCE_ADMIN_API_KEY"),
    ],
    ids=["mcp", "admin"],
)
def test_file_var_wins_over_the_plain_var(
    accessor: Any, var: str, tmp_path: Any, monkeypatch: Any
) -> None:
    """Precedence must match ``secret_env``: ``*_FILE`` always wins."""
    monkeypatch.setenv(var, _PLAIN)
    monkeypatch.setenv(f"{var}_FILE", _write(tmp_path, f"{var}.secret", _SECRET))
    assert accessor() == _SECRET


@pytest.mark.parametrize(
    ("accessor", "var"),
    [
        (live_mcp_api_key, "NCE_MCP_API_KEY"),
        (live_admin_api_key, "NCE_ADMIN_API_KEY"),
    ],
    ids=["mcp", "admin"],
)
def test_trailing_newline_is_stripped(
    accessor: Any, var: str, tmp_path: Any, monkeypatch: Any
) -> None:
    """A mounted secret almost always ends in a newline from the editor."""
    monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(f"{var}_FILE", _write(tmp_path, f"{var}.secret", _SECRET + "\n"))
    assert accessor() == _SECRET


@pytest.mark.parametrize(
    ("accessor", "var"),
    [
        (live_mcp_api_key, "NCE_MCP_API_KEY"),
        (live_admin_api_key, "NCE_ADMIN_API_KEY"),
    ],
    ids=["mcp", "admin"],
)
def test_unreadable_file_fails_closed_and_never_leaks_the_path_contents(
    accessor: Any, var: str, tmp_path: Any, monkeypatch: Any
) -> None:
    """Fail-closed, matching ``secret_env``: a missing mount is not "no key"."""
    monkeypatch.setenv(var, _PLAIN)
    monkeypatch.setenv(f"{var}_FILE", str(tmp_path / "definitely-absent.secret"))
    with pytest.raises(RuntimeError) as excinfo:
        accessor()
    # Names the var, never the secret it was meant to hold.
    assert var in str(excinfo.value)
    assert _PLAIN not in str(excinfo.value)


@pytest.mark.parametrize(
    ("accessor", "var"),
    [
        (live_mcp_api_key, "NCE_MCP_API_KEY"),
        (live_admin_api_key, "NCE_ADMIN_API_KEY"),
    ],
    ids=["mcp", "admin"],
)
def test_plain_var_still_works_and_stays_live(accessor: Any, var: str, monkeypatch: Any) -> None:
    """Backward compatible, and still reflects runtime env changes.

    ``live_env_str`` exists precisely so ``monkeypatch.setenv``/``delenv``
    behave; the fix must not trade that for a cached read.
    """
    monkeypatch.delenv(f"{var}_FILE", raising=False)
    monkeypatch.setenv(var, _PLAIN)
    assert accessor() == _PLAIN
    monkeypatch.setenv(var, _PLAIN + "-rotated")
    assert accessor() == _PLAIN + "-rotated"
    monkeypatch.delenv(var, raising=False)
    assert accessor() == ""


# ---------------------------------------------------------------------------
# 2. The live auth path -- the availability trap itself
# ---------------------------------------------------------------------------


@pytest.mark.real_mcp_auth
def test_tenant_injection_resolves_a_file_mounted_key(tmp_path: Any, monkeypatch: Any) -> None:
    """The symptom: tenant tools stop answering when only ``*_FILE`` is mounted.

    ``enforce_mcp_tool_auth`` injects the server's own key when the caller
    supplied none. Before the fix that read ``os.environ`` directly, so a
    file-mounted key resolved to ``""``, nothing was injected, and the tenant
    scope check refused.
    """
    monkeypatch.delenv("NCE_MCP_API_KEY", raising=False)
    monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", "")
    monkeypatch.setenv("NCE_MCP_API_KEY_FILE", _write(tmp_path, "mcp.secret", _SECRET))

    seen: dict[str, Any] = {}

    def _capture(scope: str, arguments: dict[str, Any]) -> None:
        # Capture INSIDE the call: the caller's ``finally`` pops the key.
        seen["scope"] = scope
        seen["mcp_api_key"] = arguments.get("mcp_api_key")

    arguments: dict[str, Any] = {}
    with patch.object(nce_auth, "_validate_scope", new=_capture):
        nce_auth.enforce_mcp_tool_auth("a_tool_that_is_not_admin_only", arguments)

    assert seen["scope"] == "tenant"
    assert seen["mcp_api_key"] == _SECRET


@pytest.mark.real_mcp_auth
def test_a_caller_supplied_key_is_never_overwritten(tmp_path: Any, monkeypatch: Any) -> None:
    """The injection is a fallback, not an override."""
    monkeypatch.delenv("NCE_MCP_API_KEY", raising=False)
    monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", "")
    monkeypatch.setenv("NCE_MCP_API_KEY_FILE", _write(tmp_path, "mcp.secret", _SECRET))

    seen: dict[str, Any] = {}

    def _capture(scope: str, arguments: dict[str, Any]) -> None:
        seen["mcp_api_key"] = arguments.get("mcp_api_key")

    with patch.object(nce_auth, "_validate_scope", new=_capture):
        nce_auth.enforce_mcp_tool_auth(
            "a_tool_that_is_not_admin_only", {"mcp_api_key": "callers-own-key"}
        )

    assert seen["mcp_api_key"] == "callers-own-key"


@pytest.mark.real_mcp_auth
def test_the_key_is_always_popped_even_when_the_scope_check_raises(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The injected key must not survive back into the caller's argument bag."""
    monkeypatch.delenv("NCE_MCP_API_KEY", raising=False)
    monkeypatch.setenv("NCE_MCP_NAMESPACE_ID", "")
    monkeypatch.setenv("NCE_MCP_API_KEY_FILE", _write(tmp_path, "mcp.secret", _SECRET))

    def _boom(scope: str, arguments: dict[str, Any]) -> None:
        raise PermissionError("refused")

    arguments: dict[str, Any] = {}
    with patch.object(nce_auth, "_validate_scope", new=_boom):
        with pytest.raises(PermissionError):
            nce_auth.enforce_mcp_tool_auth("a_tool_that_is_not_admin_only", arguments)

    assert "mcp_api_key" not in arguments
    assert "admin_api_key" not in arguments


# ---------------------------------------------------------------------------
# 3. Ratchets
# ---------------------------------------------------------------------------


def test_the_d30_injection_is_still_present() -> None:
    """D30 was closed BY DECISION -- this fix must not silently reverse it.

    PR #137 (B67P) documented MCP stdio as a trusted local pipe and ratcheted
    that assumption. A change that deletes the injection is re-litigating D30,
    not fixing a ``*_FILE`` bug.
    """
    # The MODULE source, not the attribute: tests/conftest.py monkeypatches
    # nce.auth.enforce_mcp_tool_auth with a wrapper, so inspecting the
    # attribute would inspect the harness instead of the code under test.
    src = inspect.getsource(nce_auth)
    assert 'arguments["mcp_api_key"] = ' in src
    assert 'not arguments.get("mcp_api_key")' in src
    assert 'not arguments.get("admin_api_key")' in src


def test_no_direct_env_read_of_either_key_remains_in_the_auth_path() -> None:
    """The defect shape, ratcheted: the auth path must go through the accessor.

    A direct ``os.environ``/``os.getenv`` read of either key bypasses the
    ``*_FILE`` resolution again, which is exactly how this bug arrived.
    """
    src = inspect.getsource(nce_auth)
    for var in ("NCE_MCP_API_KEY", "NCE_ADMIN_API_KEY"):
        for pattern in (
            f'os.environ.get("{var}"',
            f'os.getenv("{var}"',
            f'os.environ["{var}"]',
        ):
            assert pattern not in src, f"{pattern} reintroduces the bug"


def test_live_accessors_do_not_reimplement_the_file_resolution() -> None:
    """One resolution, shared -- not a second copy that can drift from it."""
    from nce import config as nce_config

    for fn in (nce_config.live_mcp_api_key, nce_config.live_admin_api_key):
        # Strip the docstring: it legitimately *describes* the *_FILE mount.
        # The invariant is about the CODE -- delegate, never re-derive.
        src = inspect.getsource(fn)
        body = src.replace(fn.__doc__ or "", "")
        assert "secret_env(" in body, (
            f"{fn.__name__} must delegate to secret_env so the *_FILE"
            " resolution has exactly one implementation"
        )
        for banned in ("os.getenv(", "os.environ", "live_env_str("):
            assert banned not in body, (
                f"{fn.__name__} uses {banned} -- that is the bug this closed:"
                " it reads past the *_FILE indirection"
            )
