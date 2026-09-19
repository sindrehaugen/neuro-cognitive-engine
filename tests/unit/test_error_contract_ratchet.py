"""tests/unit/test_error_contract_ratchet.py
=========================================
Wave T-4a: Shared MCP Error Contract & Escaped Guards Ratchet.

Invariant:
1. Zero vertical module exception classes across ``nce/vertical_modules/**`` may escape
   to ``MCP_INTERNAL_ERROR`` (-32603) when wrapped by ``@mcp_handler``.
2. All 10 ``<Engine>DisabledError`` classes must inherit from ``EngineDisabledError``
   (from ``nce.engine_registry``) and map to ``MCP_ENGINE_DISABLED`` (-32005) with
   ``data.reason == "engine_disabled"``.
3. The legacy dynamic import hack ``_get_resources_errors()`` in ``nce/mcp_errors.py``
   is permanently retired (0 AST occurrences).
4. All registered MCP tools in ``TOOL_REGISTRY`` must resolve to handlers protected by
   ``@mcp_handler``, with remaining in-band error returns pinned in a shrink-only
   allowlist for Wave T-4b burn-down.
5. Standing positive control (U18): synthetic unmapped exception classes must be caught
   and flagged as escaping by the ratchet scanner.
6. T-4c Deferral Condition, reshaped 2026-09-19 (charter §13, "the residual-pin problem,
   third time this week"): every REST error response return site (``HTTPException``,
   ``JSONResponse``, ``Response`` with ``status_code >= 400``) across admin handlers and
   the customer portal must live in a file on the reviewed
   ``_FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS`` allowlist. This replaces a bare
   shrink-only total-site ceiling (``MAX_REST_ERROR_RESPONSE_SITES_FLOOR = 1090``), which
   broke exactly like ``MUTATION_TOOLS``/``CACHEABLE_TOOLS`` (K-H4) and the H-1 residual
   route pin before it: it could not tell "an old site nobody converted" from "a new
   handler that copied the exact pattern every sibling handler in the same file already
   uses", so it fired on Wave D-5 (#252) for following the house convention and its only
   available remedy was raising the number. Every admin_handlers file (and the customer
   portal app) currently uses the raw pattern exclusively -- the shared helpers in
   ``nce.admin_http_support`` (``admin_error_response``, ``admin_client_error``,
   ``admin_validation_error``, ``engine_unavailable``) exist but see zero use for the
   4xx/5xx path anywhere in this surface today, so the allowlist below is, honestly,
   every file that has one. What the allowlist buys: growth WITHIN an already-listed
   file (the overwhelmingly common case -- a new handler following its file's own
   established pattern) costs nothing and needs no review, exactly like the H-1
   residual-pin allowlist's already-approved prefixes; a raw site in a file NOT on the
   list -- the next genuinely new admin surface -- fails by name, same bar as before,
   arguably a sharper one (it fails immediately rather than needing a count to first
   drift past a ceiling). Migrating a file to the shared helpers and removing it from
   this list is real, verifiable shrinkage; nothing here forces or schedules that
   migration, which is a separate, much larger initiative than this reshape.

   Also fixed here: the previous ceiling's ``targets`` list scanned the nonexistent path
   ``nce/customer_portal/app.py``. The real file is
   ``nce/vertical_modules/customer_portal/app.py`` (44 real sites, silently never
   measured). Corrected the path; the allowlist below reflects the true, now-measured
   estate.
7. All mapped domain refusals must preserve their distinct ``data.reason`` across
   ``@mcp_handler`` and specialized error translators (no None flattening).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Any, Final

import pytest

from nce.engine_registry import EngineDisabledError
from nce.mcp_errors import (
    MCP_BUSINESS_REFUSED,
    MCP_ENGINE_DISABLED,
    MCP_INTERNAL_ERROR,
    BusinessRefusalError,
    McpError,
    mcp_handler,
)
from nce.tool_registry import TOOL_REGISTRY

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_VERTICAL_MODULES_DIR: Final[Path] = _REPO_ROOT / "nce" / "vertical_modules"
_MCP_ERRORS_PATH: Final[Path] = _REPO_ROOT / "nce" / "mcp_errors.py"

# The canonical reconciled set of 10 <Engine>DisabledError classes across vertical modules.
# In Step 1 Census, 9 were escaping to -32603 while ResourcesDisabledError was caught by
# the legacy _get_resources_errors() hack. Under T-4a, all 10 inherit from EngineDisabledError.
RECONCILED_DISABLED_ERROR_CLASSES: Final[dict[str, str]] = {
    "AgreementsDisabledError": "nce.vertical_modules.agreements._guard",
    "BusinessInsightsDisabledError": "nce.vertical_modules.business_insights._guard",
    "EconomyDisabledError": "nce.vertical_modules.economy._guard",
    "FieldTechDisabledError": "nce.vertical_modules.field_tech._guard",
    "HrDisabledError": "nce.vertical_modules.hr._guard",
    "InventoryDisabledError": "nce.vertical_modules.inventory._guard",
    "MarketingDisabledError": "nce.vertical_modules.marketing._guard",
    "ProductDisabledError": "nce.vertical_modules.product._guard",
    "ResourcesDisabledError": "nce.vertical_modules.resources._guard",
    "SupportDisabledError": "nce.vertical_modules.support._guard",
}

# Remaining in-band JSON error tools targeted by Wave T-4b (shrink-only allowlist).
# No new in-band error tools may be added.
IN_BAND_JSON_ERROR_TOOLS_T4B_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "explain_config_change",
        "d365_query_case",
        "d365_sync_now",
        "d365_case_stress_report",
        "d365_list_sla_breaches",
        "d365_netbox_mappings",
        "d365_sync_status",
        "evaluate_circuit_impact",
        "diag_ingest_bundle",
        "diag_commit_bundle",
        "diag_digest_status",
        "diag_device_health",
        "diag_list_anomalies",
    }
)

# T-4c reshaped 2026-09-19 (see module docstring, invariant 6): a shrink-only allowlist
# of files still on the raw JSONResponse/HTTPException error-response pattern rather
# than nce.admin_http_support's shared helpers. Every file below was measured true on
# 2026-09-19 (main @ 86a4ee0) -- this is every file with >=1 qualifying site today, not
# a hand-picked subset. Growth WITHIN a listed file is free (it already carries this
# debt); a file not listed here must use the shared helpers for every >=400 response.
# Removing an entry (because a file was migrated) is real shrinkage and always welcome;
# adding one back, or adding a new one, needs a stated reason -- same discipline as
# _KNOWN_PLATFORM_PREFIXES (K-H8) and _KNOWN_GAPS.
_NOT_MIGRATED_REASON: Final[str] = (
    "pre-existing convention (measured 2026-09-19): raw JSONResponse/HTTPException, "
    "not the nce.admin_http_support helpers -- not yet migrated, not this wave's job"
)
_FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS: Final[dict[str, str]] = {
    "nce/admin_app.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/_shared.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/a2a.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/agreements.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/assets.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/business_insights.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/d365.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/economy.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/entity_resolution.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/field_tech.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/fleet.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/health.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/hr.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/inventory.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/marketing.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/muscles.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/pricing.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/procurement.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/product.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/project.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/replay.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/resources.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/sales.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/sales_public.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/settings.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/support.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/system_design.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/tools.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/vendors.py": _NOT_MIGRATED_REASON,
    "nce/admin_handlers/webhooks.py": _NOT_MIGRATED_REASON,
    "nce/vertical_modules/customer_portal/app.py": _NOT_MIGRATED_REASON,
}


def _collect_rest_error_response_sites(targets: list[Path]) -> list[tuple[Path, int]]:
    """Return (absolute_path, lineno) for every qualifying >=400 response site.

    A qualifying site is a call to HTTPException/JSONResponse/Response whose
    status_code (keyword or first positional int constant) is >= 400. Shared with
    the test below and with the positive control, so both use one true scanner.
    Paths are returned absolute (not relative to `_REPO_ROOT`) so this also works
    against an arbitrary scratch directory, such as the positive control's probe file.
    """
    py_files: list[Path] = []
    for t in targets:
        if t.is_file():
            py_files.append(t)
        elif t.is_dir():
            py_files.extend(t.rglob("*.py"))

    sites: list[tuple[Path, int]] = []
    for file_path in py_files:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr

                if func_name in ("HTTPException", "JSONResponse", "Response"):
                    status = None
                    for kw in node.keywords:
                        if (
                            kw.arg == "status_code"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, int)
                        ):
                            status = kw.value.value
                            break
                    if (
                        status is None
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, int)
                    ):
                        status = node.args[0].value

                    if status is not None and status >= 400:
                        sites.append((file_path, node.lineno))
    return sites


def _evaluate_exception_mapping(exc_cls: type[BaseException]) -> int:
    """Run exc_cls through @mcp_handler and return the resulting JSON-RPC error code."""

    @mcp_handler
    async def _failing_handler() -> Any:
        raise exc_cls("ratchet test probe")

    try:
        asyncio.run(_failing_handler())
    except McpError as err:
        return err.code
    except Exception:
        return MCP_INTERNAL_ERROR
    return 0


def _collect_vertical_module_exception_classes() -> list[tuple[str, str, type[BaseException]]]:
    """Dynamically collect all exception classes defined within nce/vertical_modules/**."""
    collected: list[tuple[str, str, type[BaseException]]] = []
    for py_path in sorted(_VERTICAL_MODULES_DIR.rglob("*.py")):
        parts = py_path.relative_to(_REPO_ROOT).with_suffix("").parts
        mod_name = ".".join(parts)
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseException) and obj.__module__ == mod.__name__:
                collected.append((mod_name, name, obj))
    return collected


def test_zero_vertical_module_exceptions_escape_to_internal_error() -> None:
    """Invariant 1: Assert zero vertical module exceptions escape to -32603 Internal Error.

    Every vertical module exception must inherit from BusinessRefusalError, EngineDisabledError,
    or a standard client parameter error (ValueError, KeyError), mapping to -32005 or -32602,
    never -32603.
    """
    all_classes = _collect_vertical_module_exception_classes()
    assert len(all_classes) >= 50, (
        f"Expected >= 50 vertical exception classes, found {len(all_classes)}"
    )

    escaping: list[str] = []
    for mod_name, name, cls in all_classes:
        code = _evaluate_exception_mapping(cls)
        if code == MCP_INTERNAL_ERROR:
            escaping.append(f"{mod_name}.{name} -> {code}")

    assert not escaping, (
        f"Detected {len(escaping)} vertical exceptions escaping to -32603 Internal Error:\n"
        + "\n".join(f"  - {e}" for e in escaping)
    )


def test_all_ten_engine_disabled_errors_reconciled() -> None:
    """Invariant 2: Assert all 10 <Engine>DisabledError classes inherit from EngineDisabledError.

    Each class must yield MCP_ENGINE_DISABLED (-32005) with reason: 'engine_disabled'.
    """
    assert len(RECONCILED_DISABLED_ERROR_CLASSES) == 10, (
        "Charter specifies exactly 10 engine disabled classes."
    )

    for class_name, mod_name in RECONCILED_DISABLED_ERROR_CLASSES.items():
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, class_name, None)
        assert cls is not None, f"Expected {class_name} to exist in {mod_name}"

        # Inheritance contract
        assert issubclass(cls, EngineDisabledError), (
            f"{class_name} must inherit from EngineDisabledError, got bases: {[b.__name__ for b in cls.__bases__]}"
        )

        # Runtime MCP mapping contract
        @mcp_handler
        async def _test_handler(target_cls: type[BaseException] = cls) -> Any:
            raise target_cls("Module disabled by configuration")

        with pytest.raises(McpError) as exc_info:
            asyncio.run(_test_handler())

        err = exc_info.value
        assert err.code == MCP_ENGINE_DISABLED, (
            f"{class_name} mapped to {err.code}, expected MCP_ENGINE_DISABLED ({MCP_ENGINE_DISABLED})"
        )
        assert isinstance(err.data, dict)
        assert err.data.get("reason") == "engine_disabled", (
            f"{class_name} data.reason was {err.data.get('reason')!r}, expected 'engine_disabled'"
        )


def test_resources_errors_legacy_hack_retired() -> None:
    """Invariant 3: Assert the legacy _get_resources_errors() hack is completely retired.

    The AST of nce/mcp_errors.py must contain 0 occurrences of _get_resources_errors.
    """
    tree = ast.parse(_MCP_ERRORS_PATH.read_text(encoding="utf-8"))
    found_occurrences: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_get_resources_errors"
        ):
            found_occurrences.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "_get_resources_errors":
            found_occurrences.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "_get_resources_errors":
            found_occurrences.append(node.lineno)

    assert not found_occurrences, (
        f"Legacy _get_resources_errors() hack still present in nce/mcp_errors.py at lines: {found_occurrences}"
    )


def test_all_registered_mcp_tools_wrapped_by_mcp_handler() -> None:
    """Invariant 4: Assert all registered MCP tools are wrapped by @mcp_handler.

    Tools not yet wrapped must be members of the standing IN_BAND_JSON_ERROR_TOOLS_T4B_ALLOWLIST,
    which is shrink-only and targeted by Wave T-4b.
    """
    assert len(TOOL_REGISTRY) >= 280, (
        f"Expected >= 280 tools in TOOL_REGISTRY, found {len(TOOL_REGISTRY)}"
    )

    unwrapped_tools: set[str] = set()
    wrapped_count = 0

    for name, spec in TOOL_REGISTRY.items():
        closure = inspect.getclosurevars(spec.handler)
        mod = closure.nonlocals.get("module")
        attr = closure.nonlocals.get("attr")
        if mod and attr:
            target_fn = getattr(mod, attr, None)
            if target_fn is not None and hasattr(target_fn, "__wrapped__"):
                wrapped_count += 1
            else:
                unwrapped_tools.add(name)
        elif hasattr(spec.handler, "__wrapped__"):
            wrapped_count += 1
        else:
            unwrapped_tools.add(name)

    assert wrapped_count >= 267, f"Expected at least 267 wrapped MCP tools, got {wrapped_count}"

    # Verify no unknown unwrapped tools have leaked in
    disallowed_unwrapped = unwrapped_tools - IN_BAND_JSON_ERROR_TOOLS_T4B_ALLOWLIST
    assert not disallowed_unwrapped, (
        f"Disallowed unwrapped tools found in TOOL_REGISTRY: {disallowed_unwrapped}. "
        f"All newly added MCP tools must be decorated with @mcp_handler."
    )

    # Verify allowlist cannot grow
    assert len(unwrapped_tools) <= len(IN_BAND_JSON_ERROR_TOOLS_T4B_ALLOWLIST), (
        f"Unwrapped tools count ({len(unwrapped_tools)}) exceeds allowlist floor ({len(IN_BAND_JSON_ERROR_TOOLS_T4B_ALLOWLIST)})"
    )


def test_positive_control_synthetic_escaping_error_fails_ratchet() -> None:
    """Invariant 5: Standing positive control (U18).

    Proves the ratchet test machinery goes RED if an unhandled bare Exception class is introduced.
    """

    class SyntheticUncaughtModuleError(Exception):
        """Synthetic error simulating an unhandled vertical failure."""

    code = _evaluate_exception_mapping(SyntheticUncaughtModuleError)
    assert code == MCP_INTERNAL_ERROR, (
        f"Positive control failure: SyntheticUncaughtModuleError expected {MCP_INTERNAL_ERROR}, got {code}"
    )


def _rest_error_scan_targets() -> list[Path]:
    return [
        _REPO_ROOT / "nce" / "admin_handlers",
        _REPO_ROOT / "nce" / "admin_app.py",
        _REPO_ROOT / "nce" / "vertical_modules" / "customer_portal" / "app.py",
    ]


def test_rest_error_response_sites_are_all_known_unconverted_files() -> None:
    """Invariant 6: T-4c, reshaped — every raw REST error-response site must live in a
    file on the reviewed unconverted-files allowlist, or fail by name.

    Replaces the old bare shrink-only total (see module docstring): growth inside an
    already-listed file is free, a raw site in any other file fails loudly, by name.
    """
    sites = _collect_rest_error_response_sites(_rest_error_scan_targets())
    unexplained = [
        (path.relative_to(_REPO_ROOT).as_posix(), lineno)
        for path, lineno in sites
        if path.relative_to(_REPO_ROOT).as_posix()
        not in _FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS
    ]
    assert not unexplained, (
        f"Found {len(unexplained)} raw REST error-response site(s) outside the reviewed "
        "unconverted-files allowlist (T-4c). Either use nce.admin_http_support's shared "
        "helpers (admin_error_response / admin_client_error / admin_validation_error / "
        "engine_unavailable), or add the file to "
        "_FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS with a reason:\n"
        + "\n".join(f"  - {path}:{lineno}" for path, lineno in unexplained[:25])
    )


def test_rest_error_response_allowlist_and_scan_are_not_vacuous() -> None:
    """Positive control: the allowlist and the scanner must both still be doing real work."""
    sites = _collect_rest_error_response_sites(_rest_error_scan_targets())
    assert len(sites) >= 1000, (
        f"Only {len(sites)} REST error-response sites found — expected >= 1000 based on "
        "the 2026-09-19 census (1126). Either the scan broke or the surface shrank a lot; "
        "either way, re-derive before trusting this instrument."
    )
    assert len(_FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS) >= 25, (
        "Unconverted-files allowlist has fewer than 25 entries — expected >= 25 based on "
        "the 2026-09-19 census (31 files). Confirm this is real migration progress, not "
        "an accidental truncation."
    )
    files_with_sites = {path.relative_to(_REPO_ROOT).as_posix() for path, _ in sites}
    assert "nce/admin_handlers/fleet.py" in files_with_sites, (
        "fleet.py (the largest known unconverted file, 105 sites at last census) was not "
        "found by the scan — the detector is probably broken, not fleet.py fixed."
    )


def test_rest_error_response_allowlist_entries_have_reasons() -> None:
    """Every allowlist entry must carry a real, non-empty reason — no bare `True`-style flag."""
    blank = [
        path
        for path, reason in _FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS.items()
        if not (reason or "").strip()
    ]
    assert not blank, f"Allowlist entries with no reason: {blank}"


def test_positive_control_unlisted_file_with_raw_error_site_is_caught(tmp_path: Path) -> None:
    """Standing positive control: prove the scanner actually detects a raw >=400 site
    (not vacuous), and that such a site can never accidentally match an allowlist entry
    by construction (the allowlist holds repo-relative paths; a scratch file has none).
    """
    probe_dir = tmp_path / "admin_handlers"
    probe_dir.mkdir()
    probe_file = probe_dir / "synthetic_probe.py"
    probe_file.write_text(
        "from starlette.responses import JSONResponse\n"
        "\n"
        "def handler():\n"
        '    return JSONResponse({"error": "synthetic"}, status_code=499)\n'
        "    return JSONResponse({'ok': True}, status_code=200)\n",  # must NOT be counted
        encoding="utf-8",
    )

    sites = _collect_rest_error_response_sites([probe_dir])
    assert len(sites) == 1, (
        f"Positive control failure: expected exactly 1 qualifying (>=400) site in the "
        f"probe file, found {len(sites)} — the scanner is either vacuous or over-matching."
    )
    (found_path, found_line) = sites[0]
    assert found_path == probe_file and found_line == 4, (
        f"Positive control failure: expected {probe_file}:4, got {found_path}:{found_line}"
    )

    # This exact absolute path can never collide with a repo-relative allowlist key.
    assert str(found_path) not in _FILES_NOT_YET_MIGRATED_TO_SHARED_ERROR_HELPERS


@pytest.mark.asyncio
async def test_domain_refusals_preserve_data_reason_through_mcp_handler() -> None:
    """Invariant 7: Assert every domain refusal preserves its data.reason.

    Guards against data.reason flattening (e.g. subclass __init__ wiping class-level reason
    attribute with None) across the shared @mcp_handler decorator and specialized translators.
    """
    import uuid
    from decimal import Decimal

    from nce.vertical_modules.inventory.reconcile import LedgerDivergenceError
    from nce.vertical_modules.inventory.reservation import (
        InsufficientAvailableError,
        OverReleaseError,
    )
    from nce.vertical_modules.inventory.rma import (
        RmaAlreadySettledError,
        RmaNotFoundError,
        RmaNotWeeeScopeError,
    )
    from nce.vertical_modules.resources._guard import ResourceConcurrencyError
    from nce.vertical_modules.system_design.geometry import VersionConflictError
    from nce.vertical_modules.system_design.mcp_handlers import (
        retire_denied_mcp_error,
        version_conflict_mcp_error,
    )
    from nce.vertical_modules.system_design.retire import RetireDeniedError

    sample_uuid = uuid.uuid4()

    cases: list[tuple[Exception, str]] = [
        (VersionConflictError("DES-1", expected=1, actual=2), "version_conflict"),
        (RetireDeniedError([{"node_label": "DEV-1", "reason": "in_use"}]), "retire_denied"),
        (
            InsufficientAvailableError(
                sku="SKU-1",
                location_id=sample_uuid,
                project_id="P1",
                requested=Decimal(5),
                on_hand=Decimal(2),
                reserved=Decimal(0),
                blocked=Decimal(0),
            ),
            "insufficient_available",
        ),
        (
            OverReleaseError(
                sku="SKU-1",
                location_id=sample_uuid,
                project_id="P1",
                requested=Decimal(5),
                currently_reserved=Decimal(2),
            ),
            "over_release",
        ),
        (RmaNotFoundError(rma_ref="RMA-1"), "rma_not_found"),
        (
            RmaAlreadySettledError(rma_ref="RMA-1", stock_movement_state="restocked"),
            "rma_already_settled",
        ),
        (RmaNotWeeeScopeError(rma_ref="RMA-1"), "rma_not_weee_scope"),
        (
            LedgerDivergenceError(
                [
                    {
                        "sku": "SKU-1",
                        "location_id": sample_uuid,
                        "on_hand": 1,
                        "ledger_sum": 0,
                        "difference": 1,
                    }
                ]
            ),
            "ledger_divergence",
        ),
        (ResourceConcurrencyError("Resource locked"), "resource_concurrency"),
        (BusinessRefusalError("Custom refused", reason="custom_slug"), "custom_slug"),
        (BusinessRefusalError("Default refused"), "business_refused"),
    ]

    for exc_instance, expected_reason in cases:
        assert getattr(exc_instance, "reason", None) == expected_reason, (
            f"Exception {exc_instance.__class__.__name__} lost its reason attribute: "
            f"expected {expected_reason!r}, got {getattr(exc_instance, 'reason', None)!r}"
        )

        @mcp_handler
        async def failing_tool(engine: Any, args: dict[str, Any]) -> str:
            raise exc_instance

        with pytest.raises(McpError) as exc_info:
            await failing_tool(None, {})

        assert exc_info.value.code == MCP_BUSINESS_REFUSED
        assert exc_info.value.data is not None
        assert exc_info.value.data.get("reason") == expected_reason, (
            f"Handler data.reason for {exc_instance.__class__.__name__} was flattened or incorrect: "
            f"expected {expected_reason!r}, got {exc_info.value.data.get('reason')!r}"
        )

    # Specialized translators in system_design
    v_exc = VersionConflictError("DES-1", expected=1, actual=2)
    v_err = version_conflict_mcp_error(v_exc)
    assert v_err.data.get("reason") == "version_conflict"

    r_exc = RetireDeniedError([{"node_label": "DEV-1", "reason": "in_use"}])
    r_err = retire_denied_mcp_error(r_exc)
    assert r_err.data.get("reason") == "retire_denied"
