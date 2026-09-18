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
6. T-4c Deferral Floor: REST error response return sites across admin handlers and customer
   portal are constrained by a shrink-only ceiling (<= 1090).
7. Invariant 7: All mapped domain refusals must preserve their distinct ``data.reason``
   across ``@mcp_handler`` and specialized error translators (no None flattening).
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

# T-4c shrink-only count floor committed in charter §13.
MAX_REST_ERROR_RESPONSE_SITES_FLOOR: Final[int] = 1090


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


def test_rest_error_response_sites_shrink_only_floor() -> None:
    """Invariant 6: T-4c Deferral Condition — REST error response return sites floor.

    Counts REST error response return sites (HTTPException, JSONResponse, Response with
    status >= 400) across nce/admin_handlers/, nce/admin_app.py, and nce/customer_portal/app.py.
    Must never exceed the committed floor of 1090.
    """
    targets = [
        _REPO_ROOT / "nce" / "admin_handlers",
        _REPO_ROOT / "nce" / "admin_app.py",
        _REPO_ROOT / "nce" / "customer_portal" / "app.py",
    ]

    py_files: list[Path] = []
    for t in targets:
        if t.is_file():
            py_files.append(t)
        elif t.is_dir():
            py_files.extend(t.rglob("*.py"))

    measured_sites = 0
    for file_path in py_files:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except Exception:
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
                        measured_sites += 1

    assert measured_sites <= MAX_REST_ERROR_RESPONSE_SITES_FLOOR, (
        f"REST error response return sites ({measured_sites}) breached shrink-only floor "
        f"({MAX_REST_ERROR_RESPONSE_SITES_FLOOR}). T-4c deferral condition violated per Charter §13."
    )


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
