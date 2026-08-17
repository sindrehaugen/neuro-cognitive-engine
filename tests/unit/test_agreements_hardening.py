"""
tests/unit/test_agreements_hardening.py
========================================
Acceptance tests for Batch 115 — Module 3.Wave 11 (hardening). Final wave of
Module 3 (Agreements).

Covers:
  1. No raw legal-term / reviewer-identity leak on the external-facing MCP
     shape (``handle_agreements_lookup_terms`` / ``_serialize_lookup_row`` /
     ``_unwrap_terms``), and on the underlying SQL SELECT clause.
  2. Exact Agreements tool-count assertion (``TOOL_REGISTRY``, ``agreements_``
     prefix) + tool classification pin (cacheable/mutation/admin_only/migration).
  3. Namespace opt-in gate (``metadata.agreements.enabled``) — a non-opted-in
     namespace is cleanly disabled at the MCP handler boundary.
  4. Internal-only invariant — ``agreement_review_queue`` /
     ``agreement_extraction_runs`` carry ONLY ``tenant_isolation_policy``
     (namespace-scoped RLS), never an ``external_scope_id`` column /
     ``external_isolation_policy``. Pins that Agreements data cannot reach a
     C3 external principal via RLS (schema-level; see repo report for the
     separate, out-of-scope A2A skill-dispatch-allowlist finding).
  5. ``_expiry_review_flags`` / ``_leakage_flags`` (coverage.py) structurally
     cannot leak raw extracted term values into their "detail" strings, even
     when fed an agreement whose ``extracted`` blob carries rich contract data.
  6. ``_dispatch_agreements_coverage_alerts`` (cron.py) stays within the
     Batch 109 ACCEPTED bound: capped sample count, GL amounts + pseudonymized
     vendor-node UUIDs only — this is a conscious deviation, not a bug, and
     this suite pins the bound rather than asserting the alert's absence.

All tests are pure unit tests (no DB, no Redis) — mocking conventions follow
``tests/unit/test_product_hardening.py`` and
``tests/unit/test_agreements_coverage_surface.py``.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg.exceptions import DataError as _PgDataError

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000099"
_AGREEMENT_ID = "00000000-0000-4000-8000-0000000000aa"
_VENDOR_NODE_ID = "00000000-0000-4000-8000-0000000000bb"

# Sentinel markers — unique strings embedded in "raw legal term" fixture data.
# Using unique sentinels (rather than plausible-looking numbers) means a
# match in any assertion below is unambiguous evidence of a real leak, never
# a coincidental collision with a legitimate amount/date/status token.
_SENTINEL_RESTRICTED_CLAUSE = "SENTINEL-RESTRICTED-CLAUSE-9f3a-DO-NOT-LEAK"
_SENTINEL_KICKBACK_PCT = "SENTINEL-KICKBACK-PCT-7f21-DO-NOT-LEAK"

# Reviewer-identity columns that exist on agreement_review_queue but must
# NEVER be selected/serialized by the read-only Advisor tool, plus generic
# secret-shaped keys as a defense-in-depth net.
_FORBIDDEN_SHAPE_KEYS: frozenset[str] = frozenset(
    {
        "reviewed_by",
        "reviewed_at",
        "nce_master_key",
        "master_key",
        "signing_key",
        "api_key",
        "password",
    }
)

_EXTRACTED_RICH_TERMS: dict[str, Any] = {
    "supplierId": {
        "value": "912345678",
        "extractionConfidence": 95.0,
        "reviewStatus": "auto_green",
    },
    "paymentTermsDays": {
        "value": 30,
        "extractionConfidence": 72.0,
        "reviewStatus": "needs_review_yellow",
    },
    "kickbackTiers": {
        "value": [{"minSpend": 100000, "pct": _SENTINEL_KICKBACK_PCT}],
        "extractionConfidence": 88.0,
        "reviewStatus": "auto_green",
    },
    "volumeCommitment": {
        "value": 250000.0,
        "extractionConfidence": 91.0,
        "reviewStatus": "auto_green",
    },
    "restrictedClause": {
        "value": _SENTINEL_RESTRICTED_CLAUSE,
        "extractionConfidence": 60.0,
        "reviewStatus": "needs_review_yellow",
    },
}


def _all_keys_recursive(obj: Any) -> set[str]:
    """Recursively collect all string keys from dicts/lists (mirrors test_product_hardening)."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys_recursive(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys_recursive(item)
    return keys


def _make_row(**overrides: Any) -> dict[str, Any]:
    """A raw ``agreement_review_queue`` row shape.

    Includes ``reviewed_by``/``reviewed_at`` even though the production SQL
    (``_LOOKUP_BASE_SQL``) never selects them: this proves ``_serialize_lookup_row``
    itself is safe (named-key construction, not a passthrough/merge) even if
    a future regression widened the SELECT to include them.
    """
    row: dict[str, Any] = {
        "agreement_id": uuid.UUID(_AGREEMENT_ID),
        "source_doc_ref": "doc-001",
        "review_status": "needs_review_yellow",
        "extraction_confidence": 82.5,
        "extracted": json.dumps(_EXTRACTED_RICH_TERMS),
        "flagged_at": "2026-07-01T00:00:00+00:00",
        "reviewed_by": "alice@example.com",
        "reviewed_at": "2026-07-02T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _make_conn(rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _patch_scoped_session(conn: MagicMock):
    @asynccontextmanager
    async def _fake_session(pool: Any, namespace_id: Any):
        yield conn

    return patch(
        "nce.vertical_modules.agreements.mcp_handlers.scoped_pg_session",
        _fake_session,
    )


def _patch_guard_ok():
    return patch(
        "nce.vertical_modules.agreements.mcp_handlers.require_agreements_enabled",
        new=AsyncMock(return_value=None),
    )


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine_disabled() -> MagicMock:
    """Engine whose ``namespaces`` query returns ``agreements_enabled=False``."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"agreements_enabled": False})
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


# ---------------------------------------------------------------------------
# 1. No raw legal-term / reviewer-identity leak on the MCP shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_terms_shape_has_exactly_the_expected_top_level_keys() -> None:
    """Each agreement entry in the response has EXACTLY the documented key set.

    Discriminates against both an accidental field drop (breaks the § 9.3
    contract) and an accidental field addition (a leak vector) — an exact
    set comparison catches either direction.
    """
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([_make_row()])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        result = await handle_agreements_lookup_terms(MagicMock(), {"namespace_id": _NAMESPACE_ID})

    parsed = json.loads(result)
    ag = parsed["agreements"][0]
    assert set(ag.keys()) == {
        "agreement_id",
        "source_doc_ref",
        "review_status",
        "extraction_confidence",
        "flagged_at",
        "terms",
    }


@pytest.mark.asyncio
async def test_lookup_terms_shape_never_leaks_reviewer_identity_or_secret_keys() -> None:
    """Recursively scan the full parsed response for forbidden keys.

    ``reviewed_by``/``reviewed_at`` are real PII-bearing columns on
    ``agreement_review_queue`` that this Advisor tool must never surface
    (reviewer identity is not part of the §9.3 trust contract this tool
    exposes — only ``review_status`` + confidence are).
    """
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([_make_row()])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        result = await handle_agreements_lookup_terms(MagicMock(), {"namespace_id": _NAMESPACE_ID})

    parsed = json.loads(result)
    leaked = _all_keys_recursive(parsed) & _FORBIDDEN_SHAPE_KEYS
    assert not leaked, f"Forbidden keys leaked in agreements_lookup_terms shape: {leaked}"


def test_lookup_sql_never_selects_reviewer_or_secret_columns() -> None:
    """``_LOOKUP_BASE_SQL`` must not SELECT reviewer-identity columns.

    Static text check on the query template itself — catches a future
    ``SELECT *`` or an added ``reviewed_by`` column at the SQL layer, before
    it ever reaches ``_serialize_lookup_row``.
    """
    from nce.vertical_modules.agreements.mcp_handlers import _LOOKUP_BASE_SQL

    assert "reviewed_by" not in _LOOKUP_BASE_SQL
    assert "reviewed_at" not in _LOOKUP_BASE_SQL
    assert "SELECT *" not in _LOOKUP_BASE_SQL.upper().replace("\n", " ")


# ---------------------------------------------------------------------------
# 2. Exact Agreements tool-count assertion
# ---------------------------------------------------------------------------

_AGREEMENTS_TOOLS: frozenset[str] = frozenset({"agreements_lookup_terms"})


def test_exact_agreements_tool_count() -> None:
    """Agreements tools registered in TOOL_REGISTRY must be EXACTLY this set.

    Scoped to the ``agreements_`` prefix only — does NOT duplicate or edit
    ``tests/test_tool_registry.py``'s repo-wide ``_EXPECTED_TOTAL``. Fails
    loudly if a future wave registers a second agreements tool without
    updating this test.
    """
    from nce.tool_registry import TOOL_REGISTRY

    registered_agreements = {name for name in TOOL_REGISTRY if name.startswith("agreements_")}
    assert registered_agreements == _AGREEMENTS_TOOLS, (
        f"Agreements tool set mismatch.\n"
        f"  Expected:   {sorted(_AGREEMENTS_TOOLS)}\n"
        f"  Got:        {sorted(registered_agreements)}"
    )


def test_agreements_lookup_terms_classification_pinned() -> None:
    """``agreements_lookup_terms`` must stay a read-only, non-admin, cacheable Advisor tool."""
    from nce.tool_registry import CACHEABLE_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY

    spec = TOOL_REGISTRY["agreements_lookup_terms"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False
    assert "agreements_lookup_terms" in CACHEABLE_TOOLS
    assert "agreements_lookup_terms" not in MUTATION_TOOLS


# ---------------------------------------------------------------------------
# 3. Namespace opt-in gate — non-opted-in namespace is cleanly disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handler_disabled_namespace_raises_mcp_scope_forbidden() -> None:
    """handle_agreements_lookup_terms raises McpError(-32005) for a non-opted-in namespace.

    Exercises the REAL guard chain end-to-end: ``_check_agreements_enabled``
    → ``require_agreements_enabled`` → the ``namespaces.metadata`` query — only
    the DB connection is mocked, not the guard function itself.
    """
    from nce.mcp_errors import McpError
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    engine = _make_engine_disabled()
    with pytest.raises(McpError) as exc_info:
        await handle_agreements_lookup_terms(engine, {"namespace_id": _NAMESPACE_ID})

    assert exc_info.value.code == -32005  # MCP_SCOPE_FORBIDDEN
    assert exc_info.value.data["reason"] == "agreements_disabled"


@pytest.mark.asyncio
async def test_mcp_handler_enabled_namespace_proceeds() -> None:
    """handle_agreements_lookup_terms proceeds to the DB read when opted in."""
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        result = await handle_agreements_lookup_terms(MagicMock(), {"namespace_id": _NAMESPACE_ID})

    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["count"] == 0


# ---------------------------------------------------------------------------
# 4. Internal-only invariant — Agreements never reaches a C3 external principal
#    via RLS (schema-level; static text scan, no DB).
# ---------------------------------------------------------------------------

_SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent.parent / "nce" / "schema.sql"


def _extract_create_table_block(schema_text: str, table_name: str) -> str:
    """Return the ``CREATE TABLE ... table_name ( ... );`` block's raw text."""
    marker = f"CREATE TABLE IF NOT EXISTS {table_name} ("
    start = schema_text.index(marker)
    end = schema_text.index(");", start)
    return schema_text[start : end + 2]


def test_agreement_tables_have_no_external_scope_column() -> None:
    """``agreement_review_queue`` / ``agreement_extraction_runs`` carry NO
    ``external_scope_id`` column.

    Pins the "internal-only by construction" invariant the wave brief asked
    for: without an ``external_scope_id`` column, these tables can never
    carry an ``external_isolation_policy`` (that policy requires the column
    to exist — see ``nce/migrations/029_c3_external_scope_rls.sql``). A later
    wave that widens Agreements to an external surface must add the column
    deliberately, which will break this test and force a conscious review.
    """
    schema_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    for table in ("agreement_review_queue", "agreement_extraction_runs"):
        block = _extract_create_table_block(schema_text, table)
        assert "external_scope_id" not in block, (
            f"{table} unexpectedly carries an external_scope_id column:\n{block}"
        )


def test_agreement_tables_use_tenant_isolation_only_never_external() -> None:
    """Both Agreements tables are in the plain ``tenant_isolation_policy`` list,
    and neither table name is ever paired with ``external_isolation_policy``
    anywhere in ``schema.sql``.
    """
    schema_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    # Present in the namespace-only tenant_tables RLS array (confirmed manually
    # at the array literal preceding the FOREACH .. tenant_isolation_policy loop).
    assert "'agreement_review_queue'" in schema_text
    assert "'agreement_extraction_runs'" in schema_text

    # external_isolation_policy is a distinct RLS primitive (C3, migration 029)
    # applied only to tables that serve external principals. Agreements tables
    # must never appear in that policy's vicinity.
    for table in ("agreement_review_queue", "agreement_extraction_runs"):
        idx = 0
        while True:
            idx = schema_text.find(table, idx)
            if idx == -1:
                break
            window = schema_text[max(0, idx - 500) : idx + 500]
            assert "external_isolation_policy" not in window, (
                f"{table} appears near external_isolation_policy — internal-only invariant broken."
            )
            idx += len(table)


# ---------------------------------------------------------------------------
# 5. coverage.py flag-builders cannot leak raw extracted term values
# ---------------------------------------------------------------------------


def test_expiry_review_flags_never_leak_extracted_term_values() -> None:
    """``_expiry_review_flags`` must not surface any raw contract-term content.

    Feeds an agreement dict whose ``extracted`` blob carries sentinel-marked
    rebate/restricted-clause content (rich enough to leak if the function
    were changed to interpolate it) alongside the ``valid_to``/``review_status``
    fields the function is actually documented to read.
    """
    from nce.vertical_modules.agreements.coverage import _expiry_review_flags

    agreements = [
        {
            "agreement_id": uuid.UUID(_AGREEMENT_ID),
            "valid_to": "2020-01-01",  # in the past -> expiry flag
            "review_status": "needs_review_yellow",  # -> also a review flag
            "supplier_id": "912345678",
            "volume_commitment": 250000.0,
            "extracted": _EXTRACTED_RICH_TERMS,  # rich raw contract data, unused by this fn
        }
    ]
    agreement_vendor_nodes = {_AGREEMENT_ID: uuid.UUID(_VENDOR_NODE_ID)}

    flags = _expiry_review_flags(agreements, agreement_vendor_nodes)

    assert len(flags) == 2  # one expiry + one review
    serialized = json.dumps(flags, default=str)
    assert _SENTINEL_RESTRICTED_CLAUSE not in serialized
    assert _SENTINEL_KICKBACK_PCT not in serialized
    assert "912345678" not in serialized  # supplier identifier is not echoed either


@pytest.mark.asyncio
async def test_leakage_flags_never_leak_extracted_term_values() -> None:
    """``_leakage_flags`` must not surface any raw contract-term content.

    Same sentinel-marker technique as the expiry/review test above, applied
    to the leakage path (which cross-joins real GL spend against agreement
    caps). Only the GL amount, date, and pseudonymized vendor-node UUID may
    appear in the flag's ``detail`` text.
    """
    from nce.entity_resolution.resolver import Match
    from nce.vertical_modules.agreements.coverage import _leakage_flags

    vendor_node_id = uuid.UUID(_VENDOR_NODE_ID)
    ns_uuid = uuid.UUID(_NAMESPACE_ID)

    agreements = [
        {
            "agreement_id": uuid.UUID(_AGREEMENT_ID),
            "volume_commitment": 100000.0,  # cap
            "extracted": _EXTRACTED_RICH_TERMS,  # rich raw contract data, unused by this fn
            "supplier_id": "912345678",
        }
    ]
    agreement_vendor_nodes = {_AGREEMENT_ID: vendor_node_id}
    gl_rows = [{"supplier_id": "912345678", "amount_nok": 250000.0, "gl_date": "2026-06-01"}]

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"label": "Vendor:912345678"})

    with patch(
        "nce.vertical_modules.agreements.coverage.resolve",
        new=AsyncMock(return_value=[Match(node_id=vendor_node_id, score=1.0)]),
    ):
        flags = await _leakage_flags(conn, ns_uuid, agreements, agreement_vendor_nodes, gl_rows)

    assert len(flags) == 1
    assert flags[0]["flag_type"] == "leakage"
    serialized = json.dumps(flags, default=str)
    assert _SENTINEL_RESTRICTED_CLAUSE not in serialized
    assert _SENTINEL_KICKBACK_PCT not in serialized
    # The vendor node id (pseudonymized UUID) is the accepted identifier —
    # the raw supplier org-number string must NOT also be echoed verbatim.
    assert "912345678" not in serialized
    assert str(vendor_node_id) in serialized


# ---------------------------------------------------------------------------
# 6. Coverage-watcher alert content stays within the Batch 109 accepted bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_alert_leakage_detail_capped_and_pseudonymized_only() -> None:
    """``_dispatch_agreements_coverage_alerts`` caps embedded detail at 5 and
    never carries anything beyond GL amounts / dates / pseudonymized node ids.

    This PINS the Batch 109 accepted deviation (capped, pseudonymized, no raw
    term text / no customer names) rather than asserting the alert's absence
    — that alert firing is intentional, watcher-role behaviour.
    """
    from nce.cron import _dispatch_agreements_coverage_alerts

    ns_id = uuid.uuid4()
    # 7 leakage flags — one more than the cap of 5 — each carrying only the
    # production shape's fields (amount/date/vendor-node-id), never raw terms.
    flags = [
        {
            "agreement_id": None,
            "flag_type": "leakage",
            "detail": (
                f"GL spend {1000.0 + i} NOK on 2026-06-0{(i % 9) + 1} has no covering "
                f"agreement for resolved vendor node {uuid.uuid4()}"
            ),
            "gl_supplier_node_id": str(uuid.uuid4()),
            "agreement_supplier_node_id": None,
        }
        for i in range(7)
    ]

    with patch("nce.cron._dispatch_throttled_alert", new=AsyncMock()) as mock_alert:
        await _dispatch_agreements_coverage_alerts(ns_id, flags)

    mock_alert.assert_awaited_once()
    call = mock_alert.await_args
    key, _title, message = call.args
    assert key == f"agreements_coverage.{ns_id}.leakage"
    assert "7 leakage flag(s)" in message
    assert "First 5:" in message
    # Exactly 5 samples embedded, never all 7 (alert-storm / payload-size guard).
    assert message.count("GL spend") == 5


# ---------------------------------------------------------------------------
# 7. REST boundary: malformed namespace_id -> structured 4xx, never an
#    escaped exception (admin-surface sweep, Fix 1).
#
# The opt-in gate (`_check_agreements_enabled_rest` -> `require_agreements_enabled`)
# used to run BEFORE any UUID validation on all five REST routes. A malformed
# namespace_id would hand a raw string straight to asyncpg's
# `WHERE id = $1::uuid` cast, which raises asyncpg.exceptions.DataError (NOT
# a Python ValueError) -- uncaught by the handlers' `except ValueError`
# clauses, so it escaped as an unstructured 500.
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
        return {"agreements_enabled": enabled}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_agreements_request(
    query_params: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.query_params = query_params or {}
    req.path_params = path_params or {}
    req.json = AsyncMock(return_value=json_body if json_body is not None else {})
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_list_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_agreements_list: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_agreements_request(query_params={"namespace_id": bad_ns})

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.agreements import api_agreements_list

        response = await api_agreements_list(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_detail_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_agreements_detail: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_agreements_request(
        query_params={"namespace_id": bad_ns},
        path_params={"id": _AGREEMENT_ID},
    )

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.agreements import api_agreements_detail

        response = await api_agreements_detail(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_extract_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_agreements_extract: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_agreements_request(
        json_body={"namespace_id": bad_ns, "source_doc_ref": "doc-001"}
    )

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.agreements import api_agreements_extract

        response = await api_agreements_extract(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_review_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_agreements_review: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_agreements_request(
        json_body={
            "namespace_id": bad_ns,
            "agreement_id": _AGREEMENT_ID,
            "decision": "confirm",
            "reviewed_by": "alice@example.com",
        }
    )

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.agreements import api_agreements_review

        response = await api_agreements_review(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_coverage_malformed_namespace_id_returns_4xx_never_escapes(bad_ns: str) -> None:
    """api_agreements_coverage: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_agreements_request(query_params={"namespace_id": bad_ns})

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = engine

        from nce.admin_handlers.agreements import api_agreements_coverage

        response = await api_agreements_coverage(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_guard_require_agreements_enabled_translates_dataerror_defence_in_depth() -> None:
    """require_agreements_enabled (Layer 2) must translate an asyncpg DataError
    (malformed ``::uuid`` cast) into AgreementsDisabledError rather than
    letting the driver exception escape -- belt-and-braces behind the
    REST-boundary check exercised above.
    """
    from nce.vertical_modules.agreements._guard import (
        AgreementsDisabledError,
        require_agreements_enabled,
    )

    engine = _make_engine_uuid_aware(enabled=True)

    with pytest.raises(AgreementsDisabledError):
        await require_agreements_enabled(engine.pg_pool, "not-a-uuid")
