"""
tests/unit/test_system_design_lucid.py
=======================================
Unit tests for the System Design Lucid export adapter (Wave 11, Phase 1b).

All HTTP is mocked — no real network calls.
No DB required (``_read_design_nodes`` is mocked; pure unit test).

Tested behaviours:
  1. ``do_publish_design_docs`` builds the correct Lucid payload from a
     seeded design and returns a ``lucid_url``.
  2. Clean no-op (``lucid_url: None``) when credentials are unset.
  3. No import path exists in the module (EXPORT ONLY — spec correction W11).
  4. Credentials never appear in log output.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_DESIGN_ID = "DESIGN-TEST-001"
_FAKE_API_KEY = "FAKE_LUCID_KEY_NEVER_IN_LOGS"
_FAKE_BASE_URL = "https://api.lucid.test"
_FAKE_LUCID_URL = "https://lucid.app/diagrams/abc123/edit"

# Seeded node data that ``_read_design_nodes`` would return.
_SEEDED_NODES: dict[str, Any] = {
    "design_label": f"DESIGN:{_DESIGN_ID.upper()}",
    "design_lines": [
        f"DESIGN_LINE:{_DESIGN_ID.upper()}:LINE-001",
        f"DESIGN_LINE:{_DESIGN_ID.upper()}:LINE-002",
    ],
    "functional_locations": [
        "FL:TESTNS:SITE-A",
        "FL:TESTNS:SITE-A:BUILDING-1",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_http_response(status_code: int, body: Any) -> MagicMock:
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=body)
    return resp


def _make_engine() -> MagicMock:
    """Return a mock NCEEngine with a pg_pool."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# Tests: export builds correct payload and returns lucid_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_design_docs_returns_lucid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """do_publish_design_docs should POST the correct payload and return lucid_url."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_LUCID_API_KEY", _FAKE_API_KEY)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_LUCID_BASE_URL", _FAKE_BASE_URL)

    http_response = _make_mock_http_response(200, {"editUrl": _FAKE_LUCID_URL})
    engine = _make_engine()

    with (
        patch(
            "nce.vertical_modules.system_design.lucid._read_design_nodes",
            new=AsyncMock(return_value=_SEEDED_NODES),
        ),
        patch(
            "nce.vertical_modules.system_design.lucid.request_with_retry",
            new=AsyncMock(return_value=http_response),
        ) as mock_req,
    ):
        from nce.vertical_modules.system_design.lucid import do_publish_design_docs

        result = await do_publish_design_docs(
            engine,
            {"namespace_id": _NAMESPACE_ID, "design_id": _DESIGN_ID},
        )

    assert result["lucid_url"] == _FAKE_LUCID_URL

    # Verify the POST was called once with a JSON body
    mock_req.assert_awaited_once()
    call_args = mock_req.call_args
    assert call_args.args[1] == "POST"
    posted_json = (
        call_args.kwargs.get("json") or call_args.args[3]
        if len(call_args.args) > 3
        else call_args.kwargs.get("json")
    )
    assert posted_json is not None
    assert posted_json["title"] == f"Design: {_DESIGN_ID}"
    assert any(i["category"] == "DESIGN_LINE" for i in posted_json["items"])
    assert any(i["category"] == "FUNCTIONAL_LOCATION" for i in posted_json["items"])


@pytest.mark.asyncio
async def test_publish_design_docs_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Payload must contain one item per DESIGN_LINE and FUNCTIONAL_LOCATION."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_LUCID_API_KEY", _FAKE_API_KEY)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_LUCID_BASE_URL", _FAKE_BASE_URL)

    http_response = _make_mock_http_response(200, {"editUrl": _FAKE_LUCID_URL})
    engine = _make_engine()

    captured_payload: dict[str, Any] = {}

    async def _capture_request(client: Any, method: str, url: str, **kwargs: Any) -> Any:
        captured_payload.update(kwargs.get("json", {}))
        return http_response

    with (
        patch(
            "nce.vertical_modules.system_design.lucid._read_design_nodes",
            new=AsyncMock(return_value=_SEEDED_NODES),
        ),
        patch(
            "nce.vertical_modules.system_design.lucid.request_with_retry",
            new=_capture_request,
        ),
    ):
        from nce.vertical_modules.system_design.lucid import do_publish_design_docs

        await do_publish_design_docs(
            engine,
            {"namespace_id": _NAMESPACE_ID, "design_id": _DESIGN_ID},
        )

    items = captured_payload.get("items", [])
    dl_items = [i for i in items if i["category"] == "DESIGN_LINE"]
    fl_items = [i for i in items if i["category"] == "FUNCTIONAL_LOCATION"]

    assert len(dl_items) == len(_SEEDED_NODES["design_lines"])
    assert len(fl_items) == len(_SEEDED_NODES["functional_locations"])

    dl_texts = {i["text"] for i in dl_items}
    assert dl_texts == set(_SEEDED_NODES["design_lines"])

    fl_texts = {i["text"] for i in fl_items}
    assert fl_texts == set(_SEEDED_NODES["functional_locations"])


# ---------------------------------------------------------------------------
# Tests: clean no-op without credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_design_docs_noop_when_api_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """do_publish_design_docs returns lucid_url=None when API key is unset."""
    monkeypatch.delenv("NCE_SYSTEM_DESIGN_LUCID_API_KEY", raising=False)

    engine = _make_engine()

    with patch(
        "nce.vertical_modules.system_design.lucid.request_with_retry",
        new=AsyncMock(),
    ) as mock_req:
        from nce.vertical_modules.system_design.lucid import do_publish_design_docs

        result = await do_publish_design_docs(
            engine,
            {"namespace_id": _NAMESPACE_ID, "design_id": _DESIGN_ID},
        )

    assert result == {"lucid_url": None}
    mock_req.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: no import path exists (EXPORT ONLY — spec correction W11)
# ---------------------------------------------------------------------------


def test_no_import_function_in_lucid_module() -> None:
    """The lucid module must not contain any import function/path.

    Lucid import is CUT per spec correction (Wave 11 brief).
    This test explicitly asserts no import-related callable exists.
    """
    import nce.vertical_modules.system_design.lucid as lucid_mod

    # Check module-level functions and classes
    public_names = [
        name
        for name, obj in inspect.getmembers(lucid_mod)
        if callable(obj) and not name.startswith("__")
    ]

    import_like = [name for name in public_names if "import" in name.lower()]

    assert not import_like, (
        f"lucid.py must not contain import functions (spec correction W11). Found: {import_like}"
    )


def test_lucid_module_source_has_no_import_function() -> None:
    """Source-level check: no function definition with 'import' in name."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("nce.vertical_modules.system_design.lucid")
    assert spec is not None and spec.origin is not None

    with open(spec.origin, encoding="utf-8") as fh:
        source = fh.read()

    tree = ast.parse(source)
    func_names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    import_funcs = [n for n in func_names if "import" in n.lower()]
    assert not import_funcs, (
        f"lucid.py source must not define any import function (spec correction W11). "
        f"Found: {import_funcs}"
    )


# ---------------------------------------------------------------------------
# Tests: credentials never appear in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_credential_in_log_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The API key value must never appear in any log record."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_LUCID_API_KEY", _FAKE_API_KEY)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_LUCID_BASE_URL", _FAKE_BASE_URL)

    http_response = _make_mock_http_response(200, {"editUrl": _FAKE_LUCID_URL})
    engine = _make_engine()

    with caplog.at_level(logging.DEBUG, logger="nce.vertical_modules.system_design.lucid"):
        with (
            patch(
                "nce.vertical_modules.system_design.lucid._read_design_nodes",
                new=AsyncMock(return_value=_SEEDED_NODES),
            ),
            patch(
                "nce.vertical_modules.system_design.lucid.request_with_retry",
                new=AsyncMock(return_value=http_response),
            ),
        ):
            from nce.vertical_modules.system_design.lucid import do_publish_design_docs

            await do_publish_design_docs(
                engine,
                {"namespace_id": _NAMESPACE_ID, "design_id": _DESIGN_ID},
            )

    full_log = "\n".join(caplog.messages)
    assert _FAKE_API_KEY not in full_log, f"Credential API key leaked into log output: {full_log!r}"
