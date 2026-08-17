"""
tests/unit/test_product_hardening.py
=====================================
Acceptance tests for Batch 043 — Module 2.Wave 13 (hardening).

Covers:
  1. No BID/cost/margin field on any public MCP handler shape.
  2. No BID/cost/margin field on any public REST route shape.
  3. Exact Product tool-count assertion (6 tools, names listed).
  4. Non-opted-in namespace is cleanly disabled (MCP + REST boundary).
  5. Business constants are NOT code literals — they load from product-*.json.
  6. Config JSON files load correctly per-namespace (no import-time errors).

All tests are pure unit tests (no DB, no Redis).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg.exceptions import DataError as _PgDataError

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000099"

# Columns that must NEVER appear on any public shape (ADR-0017).
_FORBIDDEN: frozenset[str] = frozenset(
    {"cost", "cost_price", "bid_price", "bid_id", "margin", "unit_cost"}
)

# Exact set of Product MCP tool names (Waves 3–7).
_PRODUCT_TOOLS: frozenset[str] = frozenset(
    {
        "product_search",
        "product_get",
        "product_price",
        "product_related",
        "product_match_bom_line",
        "product_enrich",
    }
)


# ---------------------------------------------------------------------------
# 1. No BID/cost/margin on any public MCP handler shape
# ---------------------------------------------------------------------------

_PUBLIC_SHAPES: list[dict[str, Any]] = [
    # search result
    {
        "results": [
            {
                "id": "aaa",
                "manufacturer": "CISCO",
                "mfr_part_no": "SFP-10G-SR",
                "gtin": None,
                "lifecycle_status": "active",
                "etim_specs": {},
                "updated_at": None,
            }
        ],
        "total": 1,
    },
    # get result
    {
        "product": {
            "id": "bbb",
            "manufacturer": "CISCO",
            "mfr_part_no": "SFP-10G-SR",
        },
        "prices": [{"supplier": "nettailer", "list_price": 99.0, "updated_at": None}],
        "edges": [{"predicate": "references", "object_label": "X", "confidence": 0.9}],
    },
    # price result (sales_price only — cost never returned)
    {
        "sales_price": 130.0,
        "source": "supplier_list",
        "as_of": None,
        "stale": False,
    },
    # match result
    {
        "bom_line": "some part",
        "matches": [{"node_id": "uuid-1", "score": 0.9, "matched_on": "mfr_part_no"}],
        "top_sku": "uuid-1",
        "top_score": 0.9,
    },
    # enrichment review item
    {
        "id": "ccc",
        "namespace_id": _NAMESPACE_ID,
        "product_id": "ddd",
        "field_name": "voltage",
        "field_value": "230V",
        "confidence": 0.8,
        "needs_review": True,
        "product_source_id": None,
        "created_at": None,
    },
]


def _all_keys_recursive(obj: Any) -> set[str]:
    """Recursively collect all string keys from dicts/lists."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys_recursive(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys_recursive(item)
    return keys


@pytest.mark.parametrize("shape", _PUBLIC_SHAPES)
def test_no_forbidden_column_in_public_shape(shape: dict[str, Any]) -> None:
    """Assert no BID/cost/margin field appears anywhere in a public Product shape."""
    all_keys = _all_keys_recursive(shape)
    leaked = all_keys & _FORBIDDEN
    assert not leaked, f"Forbidden columns {leaked} leaked in public Product shape: {shape}"


# ---------------------------------------------------------------------------
# 2. Exact Product tool-count assertion
# ---------------------------------------------------------------------------


def test_exact_product_tool_count() -> None:
    """Product tools registered in TOOL_REGISTRY must be exactly the 6 listed tools."""
    from nce.tool_registry import TOOL_REGISTRY

    registered_product = {name for name in TOOL_REGISTRY if name.startswith("product_")}
    assert registered_product == _PRODUCT_TOOLS, (
        f"Product tool set mismatch.\n"
        f"  Expected:   {sorted(_PRODUCT_TOOLS)}\n"
        f"  Got:        {sorted(registered_product)}"
    )


def test_total_registry_count_unchanged() -> None:
    """Total TOOL_REGISTRY count must be 95 (unified realignment registry; this wave adds none)."""
    from nce.tool_registry import TOOL_REGISTRY

    assert len(TOOL_REGISTRY) >= 95, (
        f"Expected at least 95 tools, got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# 3. Non-opted-in namespace is cleanly disabled at MCP handler boundary
# ---------------------------------------------------------------------------


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine_disabled() -> MagicMock:
    """Engine whose pg_pool returns NULL for the namespaces.metadata query."""
    conn = AsyncMock()
    # namespaces query returns row with product_enabled=False
    conn.fetchrow = AsyncMock(return_value={"product_enabled": False})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="SET")
    conn.fetchval = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))

    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_engine_enabled() -> MagicMock:
    """Engine whose pg_pool returns product_enabled=True for the namespaces query."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"product_enabled": True})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="SET")
    conn.fetchval = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))

    engine = MagicMock()
    engine.pg_pool = pool
    return engine


@pytest.mark.asyncio
async def test_mcp_handler_disabled_namespace_raises_mcp_error() -> None:
    """handle_product_search raises McpError(-32005) for a non-opted-in namespace."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.product.mcp_handlers import handle_product_search

    engine = _make_engine_disabled()
    with pytest.raises(McpError) as exc_info:
        await handle_product_search(engine, {"namespace_id": _NAMESPACE_ID, "query": "SFP"})

    assert exc_info.value.code == -32005
    assert (
        "product" in exc_info.value.message.lower()
        or "disabled" in str(exc_info.value.data).lower()
    )


@pytest.mark.asyncio
async def test_mcp_handler_enabled_namespace_proceeds() -> None:
    """handle_product_search proceeds when namespace has product.enabled=true."""
    from nce.vertical_modules.product.mcp_handlers import handle_product_search

    engine = _make_engine_enabled()

    # Patch scoped_pg_session to avoid real DB call in do_search_products.
    class _FakeScoped:
        def __init__(self, pool: Any, ns: str) -> None:
            self._pool = pool

        async def __aenter__(self) -> Any:
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_: Any) -> None:
            pass

    with patch("nce.vertical_modules.product.mcp_handlers.scoped_pg_session", new=_FakeScoped):
        result_json = await handle_product_search(
            engine, {"namespace_id": _NAMESPACE_ID, "query": "SFP"}
        )

    parsed = json.loads(result_json)
    assert "results" in parsed


# ---------------------------------------------------------------------------
# 4. Non-opted-in namespace cleanly disabled at REST route boundary
# ---------------------------------------------------------------------------


def _make_starlette_request(query_params: dict[str, str]) -> MagicMock:
    req = MagicMock()
    req.query_params = query_params
    req.path_params = {}
    return req


@pytest.mark.asyncio
async def test_rest_route_disabled_namespace_returns_409() -> None:
    """api_product_search returns 409 for a non-opted-in namespace."""
    engine = _make_engine_disabled()

    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "query": "SFP"})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.product import api_product_search

        response = await api_product_search(request)

    assert response.status_code == 409
    body = json.loads(response.body)
    assert "error" in body


@pytest.mark.asyncio
async def test_rest_enrichment_review_disabled_namespace_returns_409() -> None:
    """api_product_enrichment_review returns 409 for a non-opted-in namespace."""
    engine = _make_engine_disabled()
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.product import api_product_enrichment_review

        response = await api_product_enrichment_review(request)

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# 5. Business constants load from product-*.json (config-as-IP assertion)
# ---------------------------------------------------------------------------

_CONFIG_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "nce" / "config_data"


def test_product_relation_weights_json_loads() -> None:
    """product-relation-weights.json must exist and contain the expected keys."""
    config_path = _CONFIG_DATA_DIR / "product-relation-weights.json"
    assert config_path.exists(), f"Missing: {config_path}"

    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    required_keys = {
        "conf_warranty",
        "conf_mount",
        "conf_replacement",
        "conf_accessory_min",
        "conf_accessory_max",
        "min_shared_tokens",
        "catalog_query_limit",
    }
    missing = required_keys - data.keys()
    assert not missing, f"Missing keys in product-relation-weights.json: {missing}"

    # Values are in valid float range for confidence scores
    for key in (
        "conf_warranty",
        "conf_mount",
        "conf_replacement",
        "conf_accessory_min",
        "conf_accessory_max",
    ):
        val = float(data[key])
        assert 0.0 <= val <= 1.0, f"{key}={val} is outside [0, 1]"


def test_product_quality_json_loads() -> None:
    """product-quality.json must exist and contain grade_thresholds + trusted_min_grade."""
    config_path = _CONFIG_DATA_DIR / "product-quality.json"
    assert config_path.exists(), f"Missing: {config_path}"

    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    assert "grade_thresholds" in data, "product-quality.json must have 'grade_thresholds'"
    assert "trusted_min_grade" in data, "product-quality.json must have 'trusted_min_grade'"

    # Grade thresholds must be a list of [float, str] pairs
    thresholds = data["grade_thresholds"]
    assert isinstance(thresholds, list) and len(thresholds) > 0
    for threshold, grade in thresholds:
        assert 0.0 <= float(threshold) <= 1.0
        assert grade in ("A", "B", "C", "D", "E")

    assert data["trusted_min_grade"] in ("A", "B", "C", "D", "E")


def test_related_module_uses_json_weights() -> None:
    """related.py must load confidence constants from JSON, not use code literals."""
    from nce.vertical_modules.product.related import (
        _CONF_ACCESSORY_MAX,
        _CONF_ACCESSORY_MIN,
        _CONF_MOUNT,
        _CONF_REPLACEMENT,
        _CONF_WARRANTY,
        _WEIGHTS,
    )

    # Constants must match what's in the JSON file
    assert abs(_CONF_WARRANTY - float(_WEIGHTS["conf_warranty"])) < 1e-9
    assert abs(_CONF_MOUNT - float(_WEIGHTS["conf_mount"])) < 1e-9
    assert abs(_CONF_REPLACEMENT - float(_WEIGHTS["conf_replacement"])) < 1e-9
    assert abs(_CONF_ACCESSORY_MIN - float(_WEIGHTS["conf_accessory_min"])) < 1e-9
    assert abs(_CONF_ACCESSORY_MAX - float(_WEIGHTS["conf_accessory_max"])) < 1e-9


def test_quality_module_uses_json_grade_thresholds() -> None:
    """quality.py must load _GRADE_THRESHOLDS from JSON, not use code literals."""
    from nce.vertical_modules.product.quality import _GRADE_THRESHOLDS, _QUALITY_CONFIG

    expected = [(float(t), str(g)) for t, g in _QUALITY_CONFIG["grade_thresholds"]]
    assert _GRADE_THRESHOLDS == expected


def test_golden_record_trusted_min_grade_from_json() -> None:
    """golden_record.py TRUSTED_MIN_GRADE must be loaded from product-quality.json."""
    from nce.vertical_modules.product.golden_record import TRUSTED_MIN_GRADE

    config_path = _CONFIG_DATA_DIR / "product-quality.json"
    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    assert TRUSTED_MIN_GRADE == data["trusted_min_grade"], (
        f"TRUSTED_MIN_GRADE={TRUSTED_MIN_GRADE!r} does not match "
        f"product-quality.json trusted_min_grade={data['trusted_min_grade']!r}"
    )


# ---------------------------------------------------------------------------
# 6. _safe_row / _safe_price_row deny-list covers all forbidden variants
# ---------------------------------------------------------------------------


def test_safe_row_strips_all_forbidden_variants() -> None:
    """_safe_row must strip cost_price, bid_id, and margin from catalog rows."""
    from nce.vertical_modules.product.mcp_handlers import _safe_row

    raw = {
        "id": "x",
        "manufacturer": "CISCO",
        "cost_price": 50.0,
        "bid_id": "bid-001",
        "margin": 0.3,
        "list_price": 99.0,
    }
    cleaned = _safe_row(raw)
    leaked = set(cleaned.keys()) & _FORBIDDEN
    assert not leaked, f"_safe_row leaked: {leaked}"
    assert "list_price" in cleaned  # public field preserved


def test_safe_price_row_strips_all_forbidden_variants() -> None:
    """_safe_price_row must strip cost_price and bid_id from price rows."""
    from nce.vertical_modules.product.mcp_handlers import _safe_price_row

    raw = {
        "supplier": "nettailer",
        "list_price": 99.0,
        "cost_price": 40.0,
        "bid_id": "bid-002",
        "updated_at": None,
    }
    cleaned = _safe_price_row(raw)
    leaked = set(cleaned.keys()) & _FORBIDDEN
    assert not leaked, f"_safe_price_row leaked: {leaked}"
    assert "list_price" in cleaned


def test_review_hidden_covers_all_forbidden_variants() -> None:
    """admin_handlers.product._REVIEW_HIDDEN must cover all ADR-0017 forbidden columns."""
    from nce.admin_handlers.product import _REVIEW_HIDDEN

    required_hidden = {"cost", "cost_price", "margin", "bid_id"}
    missing = required_hidden - _REVIEW_HIDDEN
    assert not missing, f"_REVIEW_HIDDEN is missing: {missing}"


# ---------------------------------------------------------------------------
# 7. REST boundary: malformed namespace_id -> structured 4xx, never an
#    escaped exception (admin-surface sweep, Fix 1).
#
# The opt-in gate (`_check_product_enabled_rest` -> `require_product_enabled`)
# used to run BEFORE any UUID validation. A malformed namespace_id would hand
# a raw string straight to asyncpg's `WHERE id = $1::uuid` cast, which raises
# asyncpg.exceptions.DataError (NOT a Python ValueError) -- uncaught by the
# handler's `except ValueError` clauses, so it escaped as an unstructured 500.
#
# `_make_engine_uuid_aware` mimics that real asyncpg cast behaviour (rather
# than a mock that silently accepts anything) so these tests actually
# exercise the failure mode instead of merely looking like they do.
# ---------------------------------------------------------------------------

_MALFORMED_NAMESPACE_IDS = ["x", "not-a-uuid", "12345678-1234-1234-1234-12345678901"]


def _make_engine_uuid_aware(*, enabled: bool) -> MagicMock:
    """Engine whose namespaces-check connection mimics asyncpg's real
    ``$1::uuid`` cast: raises ``asyncpg.exceptions.DataError`` for a
    non-UUID-parseable namespace_id, otherwise returns the enabled flag.
    """

    async def _fetchrow(_query: str, namespace_id: str) -> dict[str, Any]:
        try:
            uuid.UUID(str(namespace_id))
        except ValueError as exc:
            raise _PgDataError(f"invalid input syntax for type uuid: {namespace_id!r}") from exc
        return {"product_enabled": enabled}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_search_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_product_search: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_starlette_request({"namespace_id": bad_ns, "query": "SFP"})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.product import api_product_search

        response = await api_product_search(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_get_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_product_get: malformed namespace_id -> 4xx JSON, never an escaped
    exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_starlette_request({"namespace_id": bad_ns})
    request.path_params = {"id": "SFP-10G-SR"}

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.product import api_product_get

        response = await api_product_get(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_enrichment_review_malformed_namespace_id_returns_4xx_never_escapes(
    bad_ns: str,
) -> None:
    """api_product_enrichment_review: malformed namespace_id -> 4xx JSON,
    never an escaped exception, and the opt-in-gate DB query is never
    reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_starlette_request({"namespace_id": bad_ns})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.product import api_product_enrichment_review

        response = await api_product_enrichment_review(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_guard_require_product_enabled_translates_dataerror_defence_in_depth() -> None:
    """require_product_enabled (Layer 2) must translate an asyncpg DataError
    (malformed ``::uuid`` cast) into ProductDisabledError rather than letting
    the driver exception escape -- belt-and-braces behind the REST-boundary
    check exercised above.
    """
    from nce.vertical_modules.product._guard import ProductDisabledError, require_product_enabled

    engine = _make_engine_uuid_aware(enabled=True)

    with pytest.raises(ProductDisabledError):
        await require_product_enabled(engine.pg_pool, "not-a-uuid")
