"""
tests/test_assets_sla.py
=========================
Pure-unit acceptance tests for Batch 147 — Module 9.Wave 7 (``sla-attach``).

>>> ORCH NOTE (2026-08-18): this file is a PURE UNIT test on purpose — no
>>> ``@pytest.mark.integration`` marker, no live Postgres. The A2A read
>>> (``agreements.sla.get_sla_coverage``) and the DB write seam
>>> (``assets.sla.scoped_pg_session``) are both mocked at the module boundary,
>>> per the wave's ORCH NOTE ("mock the dependency at the seam instead; that is
>>> the intended shape here"). This mirrors the established precedent in
>>> ``tests/unit/test_product_enrich_a2a.py`` (patch ``scoped_pg_session`` where
>>> it is looked up, in the module under test's own namespace).

What each test proves (and what it does NOT prove):
  - The A2A READ: ``do_attach_sla`` calls ``get_sla_coverage`` with the caller's
    own ``(pool, namespace_id, agreement_id)`` — proven via call-arg capture on
    a mock, not against a real Agreements table.
  - The per-ROOM WRITE: exactly one ``kg_edges`` upsert is issued, with the
    ``covered_by`` predicate, ``FL:<slug>:<fl_id>`` as subject and
    ``Agreement:<id>`` as object — proven via SQL text + bound-parameter
    capture on a fake connection, not against a real database. No RLS is
    exercised here (there is no database); the ``namespace_id`` assertions
    below prove this module's OWN explicit predicate/parameter, matching the
    repo-wide rule that RLS is never the only guard.
  - "No terms authored": proven by patching the two Agreements WRITE symbols
    (``do_set_sla_coverage``, ``upsert_agreement_term_node``) and asserting
    zero calls — not by reading source code.
  - "No clock/breach written": ``nce/vertical_modules/assets/sla.py`` imports
    no Support module and defines no clock/breach write path at all, so there
    is nothing to call or mock; this is a structural fact checked by grepping
    the module's imports in ``test_module_imports_no_support_or_terms_writer``
    below, not a runtime mutation target (there is no mechanism to remove).
  - The RETURN VALUE: all five keys of ``do_attach_sla``'s return dict are
    asserted, not just ``covered_by_edge``/``sla_terms_read`` — ``status``,
    ``agreement_id`` and ``functional_location_id`` are checked against
    fixture values that differ in both content and shape, so a swap between
    ``agreement_id``/``functional_location_id`` (or a partial-failure path
    that still hardcodes ``status: "ok"``) fails the test.
  - The A2A-READ-FAILS path: when ``get_sla_coverage`` raises, the exception
    must propagate (not be swallowed into a fabricated result) and no write
    may be attempted — pinned as a contract, not left as an accident of "no
    try/except exists today".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_OTHER_NAMESPACE_ID = "00000000-0000-4000-8000-000000000002"
_AGREEMENT_ID = "aaaaaaaa-0000-4000-8000-000000000001"
_FL_ID = "site-a:room-101"
_NAMESPACE_SLUG = "acme"

_SLA_TERMS = [
    f"AgreementTerm:{_AGREEMENT_ID}:sla_responseHours",
    f"AgreementTerm:{_AGREEMENT_ID}:sla_coverageWindow",
]


# ---------------------------------------------------------------------------
# Fakes — mock the dependency AT THE SEAM (no real asyncpg, no real Postgres)
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal fake connection: only ``execute`` is used by ``do_attach_sla``."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "INSERT 0 1"


class _FakeScoped:
    """Replacement for ``scoped_pg_session`` — yields a ``_FakeConn`` directly.

    Bypasses ``pool.acquire`` / ``conn.transaction`` / ``set_namespace_context``
    entirely, matching ``tests/unit/test_product_enrich_a2a.py``'s
    ``_fake_scoped_session_factory`` pattern: the seam under test is the DB
    boundary, not asyncpg's own transaction machinery.
    """

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, pool: Any, namespace_id: Any):  # noqa: ANN401 - test double
        self.calls.append((pool, namespace_id))
        return self

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _fake_engine() -> Any:
    class _Engine:
        pg_pool = object()  # never dereferenced directly — scoped_pg_session is patched

    return _Engine()


def _base_params(**overrides: Any) -> dict[str, Any]:
    params = {
        "namespace_id": _NAMESPACE_ID,
        "agreement_id": _AGREEMENT_ID,
        "functional_location_id": _FL_ID,
        "namespace_slug": _NAMESPACE_SLUG,  # bypass the `namespaces` DB lookup
    }
    params.update(overrides)
    return params


def _patched(sla_terms: list[str] | None = None):
    """Return the (get_sla_coverage mock, scoped-session fake, conn) triple."""
    conn = _FakeConn()
    scoped = _FakeScoped(conn)
    coverage_mock = AsyncMock(
        return_value={
            "agreement_id": _AGREEMENT_ID,
            "covers": [],
            "sla_terms": list(_SLA_TERMS if sla_terms is None else sla_terms),
        }
    )
    return coverage_mock, scoped, conn


# ---------------------------------------------------------------------------
# 1. The A2A read: terms come FROM Agreements, with the caller's own identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_sla_terms_from_agreements_via_a2a() -> None:
    """do_attach_sla must call agreements.sla.get_sla_coverage with the caller's
    own (pool, namespace_id, agreement_id) — the A2A read."""
    from nce.vertical_modules.assets import sla as assets_sla

    coverage_mock, scoped, _conn = _patched()
    engine = _fake_engine()

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
    ):
        result = await assets_sla.do_attach_sla(engine, _base_params())

    coverage_mock.assert_awaited_once()
    call_args = coverage_mock.await_args
    assert call_args is not None
    pool_arg, ns_arg, agreement_arg = call_args.args
    assert pool_arg is engine.pg_pool
    assert str(ns_arg) == _NAMESPACE_ID
    assert str(agreement_arg) == _AGREEMENT_ID
    assert result["sla_terms_read"] == _SLA_TERMS


# ---------------------------------------------------------------------------
# 2. The per-ROOM write: exactly one covered_by edge, correct labels + ns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_exactly_one_covered_by_edge_per_room() -> None:
    """do_attach_sla must write exactly one kg_edges row: FL -[covered_by]->
    Agreement, scoped to the caller's own namespace_id."""
    from nce.vertical_modules.assets import sla as assets_sla

    coverage_mock, scoped, conn = _patched()
    engine = _fake_engine()

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
    ):
        result = await assets_sla.do_attach_sla(engine, _base_params())

    assert len(conn.execute_calls) == 1, "must write exactly one edge, nothing more"
    query, args = conn.execute_calls[0]
    assert "kg_edges" in query
    assert "ON CONFLICT" in query

    fl_label, predicate, agreement_label, confidence, namespace_id, change_origin = args
    assert fl_label == f"FL:{_NAMESPACE_SLUG.upper()}:{_FL_ID.upper()}"
    assert predicate == "covered_by"
    assert agreement_label == f"Agreement:{_AGREEMENT_ID}"
    assert confidence == 1.0
    assert namespace_id == _NAMESPACE_ID
    assert change_origin == "agent"

    assert result["covered_by_edge"] == {
        "subject": fl_label,
        "predicate": "covered_by",
        "object": agreement_label,
    }


# ---------------------------------------------------------------------------
# 2b. The return-value contract: status / agreement_id / functional_location_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_return_value_carries_status_agreement_id_and_fl_id() -> None:
    """do_attach_sla's return dict must carry status == "ok" and must echo
    back the CALLER's own agreement_id / functional_location_id — not just a
    non-null value. _AGREEMENT_ID and _FL_ID are deliberately different in
    both content and shape (a UUID string vs. a colon-joined slug), so a
    swap between the two return keys fails this test, not just a missing or
    blank one."""
    from nce.vertical_modules.assets import sla as assets_sla

    coverage_mock, scoped, _conn = _patched()
    engine = _fake_engine()

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
    ):
        result = await assets_sla.do_attach_sla(engine, _base_params())

    assert result["status"] == "ok"
    assert result["agreement_id"] == _AGREEMENT_ID
    assert result["functional_location_id"] == _FL_ID


@pytest.mark.asyncio
async def test_write_uses_the_callers_own_namespace_id_not_a_fixed_one() -> None:
    """The namespace_id bound into the write must track the CALLER's argument,
    not a value baked into the module — proven with two different namespaces
    (not the same fixture value each time, per the ORCH NOTE's discriminator
    trap)."""
    from nce.vertical_modules.assets import sla as assets_sla

    for ns in (_NAMESPACE_ID, _OTHER_NAMESPACE_ID):
        coverage_mock, scoped, conn = _patched()
        engine = _fake_engine()
        with (
            patch.object(assets_sla, "get_sla_coverage", coverage_mock),
            patch.object(assets_sla, "scoped_pg_session", scoped),
        ):
            await assets_sla.do_attach_sla(engine, _base_params(namespace_id=ns))

        _, args = conn.execute_calls[0]
        bound_namespace_id = args[4]
        assert bound_namespace_id == ns


# ---------------------------------------------------------------------------
# 3. "No terms authored" — the Agreements WRITE symbols are never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_authors_agreement_terms_or_calls_the_terms_writer() -> None:
    """do_attach_sla must never call agreements.sla.do_set_sla_coverage (the
    terms WRITER) or agreements.graph.upsert_agreement_term_node — Assets only
    reads terms, it never authors them."""
    from nce.vertical_modules.agreements import graph as agreements_graph
    from nce.vertical_modules.agreements import sla as agreements_sla
    from nce.vertical_modules.assets import sla as assets_sla

    coverage_mock, scoped, _conn = _patched()
    engine = _fake_engine()

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
        patch.object(agreements_sla, "do_set_sla_coverage", AsyncMock()) as terms_writer_mock,
        patch.object(agreements_graph, "upsert_agreement_term_node", AsyncMock()) as term_node_mock,
    ):
        await assets_sla.do_attach_sla(engine, _base_params())

    terms_writer_mock.assert_not_awaited()
    term_node_mock.assert_not_awaited()


def test_module_imports_no_support_or_terms_writer_symbol() -> None:
    """Structural check: assets/sla.py's own namespace carries no reference to
    a Support clock/breach writer or to Agreements' terms-authoring function —
    there is no mechanism to accidentally call. (Not a mutation target: there
    is nothing here to break and re-green: the guard IS the absence.)"""
    from nce.vertical_modules.assets import sla as assets_sla

    module_names = set(vars(assets_sla).keys())
    assert "do_set_sla_coverage" not in module_names
    assert "upsert_agreement_term_node" not in module_names
    assert not any("support" in name.lower() for name in module_names)


# ---------------------------------------------------------------------------
# 4. Fails loud when Agreements has no terms on record (no fabricated coverage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raises_when_agreements_has_no_sla_terms() -> None:
    """No terms on record in Agreements => do_attach_sla refuses to write a
    coverage link (it would otherwise fabricate coverage for nothing)."""
    from nce.vertical_modules.assets import sla as assets_sla

    coverage_mock, scoped, conn = _patched(sla_terms=[])
    engine = _fake_engine()

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
        pytest.raises(ValueError, match="no SLA terms"),
    ):
        await assets_sla.do_attach_sla(engine, _base_params())

    assert conn.execute_calls == [], "must not write any edge when terms are absent"


# ---------------------------------------------------------------------------
# 5. Required-parameter validation — fails before any A2A call or DB write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["namespace_id", "agreement_id", "functional_location_id"],
)
async def test_missing_required_param_raises_before_any_call(missing_field: str) -> None:
    from nce.vertical_modules.assets import sla as assets_sla

    coverage_mock, scoped, conn = _patched()
    engine = _fake_engine()
    params = _base_params()
    params.pop(missing_field)

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
        pytest.raises(ValueError),
    ):
        await assets_sla.do_attach_sla(engine, params)

    coverage_mock.assert_not_awaited()
    assert conn.execute_calls == []


# ---------------------------------------------------------------------------
# 6. The A2A read raises — the exception propagates, nothing is written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_read_error_propagates_and_no_write_is_attempted() -> None:
    """When agreements.sla.get_sla_coverage raises, do_attach_sla must NOT
    swallow the exception into a fabricated success result, and must not
    attempt the coverage-link write. This is pinned as a contract, not left
    to the accident of "no try/except exists today"."""
    from nce.vertical_modules.assets import sla as assets_sla

    conn = _FakeConn()
    scoped = _FakeScoped(conn)
    coverage_mock = AsyncMock(side_effect=RuntimeError("agreements lookup failed"))
    engine = _fake_engine()

    with (
        patch.object(assets_sla, "get_sla_coverage", coverage_mock),
        patch.object(assets_sla, "scoped_pg_session", scoped),
        pytest.raises(RuntimeError, match="agreements lookup failed"),
    ):
        await assets_sla.do_attach_sla(engine, _base_params())

    assert conn.execute_calls == [], "must not write any edge when the A2A read fails"
