"""
tests/test_economy_peppol_close.py
====================================
Acceptance test for Batch 128 — Module 8.Wave 13 ("peppol-close"), the final
wave of Module 8.

Two disciplines under test:

  1. KID generation (MOD10/Luhn) — pure logic, plain unit tests (rule 9: no
     DB dependency, so these are NOT ``@pytest.mark.integration``).
  2. Outbound EHF is PEPPOL-flag-gated (pure logic + monkeypatching — also
     plain unit tests) and the close-narrative is C9a-grounded (requires a
     live Postgres with the ``kg_nodes``/``kg_edges`` schema applied — these
     ARE ``@pytest.mark.integration`` and skip locally when no DSN is
     configured, per ``tests/conftest.py``'s ``pg_pool`` fixture).

This file is wired into the "Integration — M8 Economy" step of
``.github/workflows/ci.yml`` (the CI integration-coverage ratchet in
``tests/test_ci_integration_coverage.py`` requires every file carrying an
``@pytest.mark.integration`` marker to be accounted for there).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.economy import close_narrative, peppol

# ---------------------------------------------------------------------------
# Discipline 1 — KID (MOD10 / Luhn). Plain unit tests: no DB.
# ---------------------------------------------------------------------------


def test_kid_generate_matches_external_luhn_reference() -> None:
    """ "79927398713" is Wikipedia's canonical worked Luhn/MOD10 example — an
    external reference vector, not just self-consistency."""
    result = peppol.do_generate_kid("7992739871")
    assert result["kid"] == "79927398713"
    assert result["check_digit"] == "3"
    assert result["variant"] == "MOD10"


def test_kid_generate_preserves_leading_zeros() -> None:
    """A ``str`` base number keeps leading zeros — the reason ``int`` input
    is refused (see test_kid_generate_rejects_int_input)."""
    result = peppol.do_generate_kid("00012345")
    assert result["base_number"] == "00012345"
    assert result["kid"].startswith("00012345")


@pytest.mark.parametrize("base", ["1234567", "123456789012", "555", "1", "9070512345671"])
def test_kid_round_trip_generate_then_validate(base: str) -> None:
    """A KID this module generates is always valid under its own validator."""
    generated = peppol.do_generate_kid(base)
    validated = peppol.do_validate_kid(generated["kid"])
    assert validated["valid"] is True
    assert validated["variant"] == "MOD10"


def test_kid_validate_known_bad_rejected() -> None:
    """A KID with a corrupted (off-by-one) check digit is refused — known-bad
    example, pinned against the same base as the external reference vector."""
    good = peppol.do_generate_kid("7992739871")
    bad_check = str((int(good["check_digit"]) + 1) % 10)
    bad_kid = good["base_number"] + bad_check
    result = peppol.do_validate_kid(bad_kid)
    assert result["valid"] is False


@pytest.mark.parametrize(
    "bad_base",
    [
        1234567,  # int — leading zeros would silently be lost
        True,  # bool — isinstance(True, int) is True; must still be refused
        "",  # empty
        "12a45",  # non-digit character
        "1" * 25,  # exceeds the 24-digit base ceiling
        None,
    ],
)
def test_kid_generate_refuses_ambiguous_input(bad_base: Any) -> None:
    with pytest.raises(ValueError):
        peppol.do_generate_kid(bad_base)


def test_kid_validate_refuses_too_short() -> None:
    with pytest.raises(ValueError):
        peppol.do_validate_kid("5")


# ---------------------------------------------------------------------------
# Discipline 1b — the ``variant`` seam (round-2 fix): MOD10 default, MOD11
# honestly unavailable, unknown variants rejected outright.
# ---------------------------------------------------------------------------


def test_kid_generate_explicit_mod10_matches_default() -> None:
    """An explicit variant="MOD10" must produce exactly the same result as
    leaving the parameter unset."""
    default_result = peppol.do_generate_kid("7992739871")
    explicit_result = peppol.do_generate_kid("7992739871", variant="MOD10")
    assert explicit_result == default_result
    assert explicit_result["variant"] == "MOD10"


def test_kid_validate_explicit_mod10_matches_default() -> None:
    default_result = peppol.do_validate_kid("79927398713")
    explicit_result = peppol.do_validate_kid("79927398713", variant="MOD10")
    assert explicit_result == default_result
    assert explicit_result["variant"] == "MOD10"


def test_kid_generate_mod11_raises_not_implemented_naming_pending_decision() -> None:
    """MOD11 is specified (docs/vertical_engines/08-economy-engine.md) but not
    built -- it must be honestly unavailable, not silently narrowed to
    MOD10."""
    with pytest.raises(NotImplementedError) as exc_info:
        peppol.do_generate_kid("7992739871", variant="MOD11")
    message = str(exc_info.value)
    assert "MOD11" in message
    assert "bank-arrangement" in message or "pending" in message


def test_kid_validate_mod11_raises_not_implemented_naming_pending_decision() -> None:
    with pytest.raises(NotImplementedError) as exc_info:
        peppol.do_validate_kid("79927398713", variant="MOD11")
    message = str(exc_info.value)
    assert "MOD11" in message
    assert "bank-arrangement" in message or "pending" in message


@pytest.mark.parametrize("bad_variant", ["MOD97", "", None, 10, "mod10", "Mod10"])
def test_kid_generate_rejects_unknown_variant(bad_variant: Any) -> None:
    """An unrecognised variant is refused outright -- never silently
    defaulted to MOD10 (that would be the exact "fails toward looseness"
    regression this fix closes)."""
    with pytest.raises(ValueError):
        peppol.do_generate_kid("7992739871", variant=bad_variant)


@pytest.mark.parametrize("bad_variant", ["MOD97", "", None, 10, "mod10", "Mod10"])
def test_kid_validate_rejects_unknown_variant(bad_variant: Any) -> None:
    with pytest.raises(ValueError):
        peppol.do_validate_kid("79927398713", variant=bad_variant)


# ---------------------------------------------------------------------------
# Discipline 2a — outbound EHF is PEPPOL-flag-gated. Plain unit tests: pure
# logic + monkeypatching, no DB.
# ---------------------------------------------------------------------------

_SAMPLE_INVOICE: dict[str, Any] = {
    "invoice_id": "INV-2026-0042",
    "issue_date": "2026-08-01",
    "currency_code": "NOK",
    "payable_amount": 1234.5,
    "supplier_peppol_id": "0192:987654321",
    "buyer_peppol_id": "0192:123456789",
    "kid": "79927398713",
}


class _RaisingTransport(peppol.PeppolTransport):
    """Test double: raises if it is ever touched. Used to prove the flag
    gate short-circuits BEFORE any transport is constructed/called."""

    async def send_document(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "PeppolTransport.send_document was called while NCE_ECONOMY_PEPPOL_ENABLED "
            "is false — the safety interlock in do_generate_ehf was bypassed"
        )


class _FakeTransport(peppol.PeppolTransport):
    """Test double: records the call and returns a canned success payload."""

    def __init__(self) -> None:
        self.called = False
        self.kwargs: dict[str, Any] = {}

    async def send_document(self, **kwargs: Any) -> dict[str, Any]:
        self.called = True
        self.kwargs = kwargs
        return {"status": "accepted", "message_id": "fake-123"}


@pytest.mark.asyncio
async def test_ehf_disabled_never_touches_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE gate-removal regression test: with NCE_ECONOMY_PEPPOL_ENABLED at
    its default (false), do_generate_ehf must return without ever calling
    ``_creds`` or constructing a transport. If the ``if not
    cfg.NCE_ECONOMY_PEPPOL_ENABLED: return ...`` gate is deleted from
    ``peppol.py``, this test fails — the patched stand-ins below raise the
    instant they are touched.
    """
    monkeypatch.setattr(cfg, "NCE_ECONOMY_PEPPOL_ENABLED", False)

    def _creds_must_not_be_called() -> tuple[str, str] | None:
        raise AssertionError(
            "_creds() was called while NCE_ECONOMY_PEPPOL_ENABLED is false — "
            "the safety interlock in do_generate_ehf was bypassed"
        )

    monkeypatch.setattr(peppol, "_creds", _creds_must_not_be_called)
    monkeypatch.setattr(peppol, "StubPeppolTransport", _RaisingTransport)

    result = await peppol.do_generate_ehf(
        dict(_SAMPLE_INVOICE),
        namespace_id=str(uuid4()),
        idempotency_key="test-key-1",
    )

    assert result["sent"] is False
    assert result["peppol_enabled"] is False
    assert "ehf_xml" in result and "<cbc:ID>INV-2026-0042</cbc:ID>" in result["ehf_xml"]


@pytest.mark.asyncio
async def test_ehf_enabled_reaches_injected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag on and credentials present, do_generate_ehf DOES reach
    the transport (proving the gate lets code through when explicitly
    enabled, the mirror image of the disabled-path test above)."""
    monkeypatch.setattr(cfg, "NCE_ECONOMY_PEPPOL_ENABLED", True)
    monkeypatch.setattr(peppol, "_creds", lambda: ("fake-api-key", "https://sandbox.example"))

    fake_transport = _FakeTransport()
    result = await peppol.do_generate_ehf(
        dict(_SAMPLE_INVOICE),
        namespace_id=str(uuid4()),
        idempotency_key="test-key-2",
        transport=fake_transport,
    )

    assert fake_transport.called is True
    assert result["sent"] is True
    assert result["peppol_enabled"] is True
    assert result["transport_result"] == {"status": "accepted", "message_id": "fake-123"}


@pytest.mark.asyncio
async def test_ehf_enabled_without_credentials_is_clean_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on but no API key configured -> no transport call, structured
    'not configured' response (mirrors system_design/lucid.py's clean no-op
    convention for missing credentials)."""
    monkeypatch.setattr(cfg, "NCE_ECONOMY_PEPPOL_ENABLED", True)
    monkeypatch.setattr(peppol, "_creds", lambda: None)
    monkeypatch.setattr(peppol, "StubPeppolTransport", _RaisingTransport)

    result = await peppol.do_generate_ehf(
        dict(_SAMPLE_INVOICE),
        namespace_id=str(uuid4()),
        idempotency_key="test-key-3",
    )

    assert result["sent"] is False
    assert result["peppol_enabled"] is True
    assert "NCE_ECONOMY_PEPPOL_API_KEY" in result["reason"]


@pytest.mark.asyncio
async def test_ehf_default_stub_transport_has_zero_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the flag fully enabled and credentials present, the
    DEFAULT (non-injected) transport is StubPeppolTransport, which always
    raises NotImplementedError -- the PEPPOL provider is not yet selected,
    so real sending is impossible by construction (mirrors
    procurement/transports.py's NetsetPoTransport / Batch 54)."""
    monkeypatch.setattr(cfg, "NCE_ECONOMY_PEPPOL_ENABLED", True)
    monkeypatch.setattr(peppol, "_creds", lambda: ("fake-api-key", "https://sandbox.example"))

    with pytest.raises(NotImplementedError):
        await peppol.do_generate_ehf(
            dict(_SAMPLE_INVOICE),
            namespace_id=str(uuid4()),
            idempotency_key="test-key-4",
        )


@pytest.mark.asyncio
async def test_generate_ehf_builds_valid_document_regardless_of_flag() -> None:
    """The XML build itself is always safe/pure -- independent of the flag."""
    result = await peppol.do_generate_ehf(
        dict(_SAMPLE_INVOICE),
        namespace_id=str(uuid4()),
        idempotency_key="test-key-5",
    )
    xml = result["ehf_xml"]
    assert "<cbc:ID>INV-2026-0042</cbc:ID>" in xml
    assert "<cbc:DocumentCurrencyCode>NOK</cbc:DocumentCurrencyCode>" in xml
    assert "1234.50" in xml  # payable_amount coerced via Decimal, never float()
    assert "<cbc:PaymentID>79927398713</cbc:PaymentID>" in xml


@pytest.mark.asyncio
async def test_generate_ehf_rejects_missing_required_field() -> None:
    bad_invoice = dict(_SAMPLE_INVOICE)
    del bad_invoice["currency_code"]
    with pytest.raises(ValueError):
        await peppol.do_generate_ehf(
            bad_invoice, namespace_id=str(uuid4()), idempotency_key="test-key-6"
        )


# ---------------------------------------------------------------------------
# Discipline 2b — close-narrative is C9a-grounded. Integration tests: needs a
# live Postgres with kg_nodes/kg_edges (skips locally per conftest.pg_pool).
# ---------------------------------------------------------------------------


async def _insert_kg_node(pool: Any, namespace_id: Any, label: str, entity_type: str) -> Any:
    async with pool.acquire() as conn:
        node_id = await conn.fetchval(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3, 'agent')
            RETURNING id
            """,
            label,
            entity_type,
            namespace_id,
        )
    assert node_id is not None
    return node_id


async def _insert_kg_edge(
    pool: Any, namespace_id: Any, subject_label: str, predicate: str, object_label: str
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id, change_origin)
            VALUES ($1, $2, $3, $4, 'agent')
            """,
            subject_label,
            predicate,
            object_label,
            namespace_id,
        )


async def _cleanup(pool: Any, namespace_id: Any, period_label: str, node_ids: list[Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM kg_edges WHERE namespace_id = $1 AND object_label = $2",
            namespace_id,
            period_label,
        )
        for node_id in node_ids:
            await conn.execute("DELETE FROM kg_nodes WHERE id = $1", node_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_narrative_all_claims_grounded(pg_pool: Any, make_namespace: Any) -> None:
    """Every citation the narrative carries traces to a real kg_nodes row;
    prose is built only from DB-retrieved labels (C9a)."""
    ns_id = await make_namespace()
    period_id = f"2026-08-{uuid4().hex[:8]}"
    period_label = f"PERIOD:{period_id.upper()}"
    invoice_label = f"INVOICE:INV-{uuid4().hex[:8]}".upper()

    period_node_id = await _insert_kg_node(pg_pool, ns_id, period_label, "PERIOD")
    invoice_node_id = await _insert_kg_node(pg_pool, ns_id, invoice_label, "INVOICE")
    await _insert_kg_edge(pg_pool, ns_id, invoice_label, "recognized_in", period_label)

    try:
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            result = await close_narrative.do_generate_close_narrative(
                conn, namespace_id=ns_id, period_id=period_id
            )

        assert result["period_id"] == period_id
        assert result["dropped"] == []
        cited_ids = {c["node_id"] for c in result["citations"]}
        assert str(period_node_id) in cited_ids
        assert str(invoice_node_id) in cited_ids
        assert len(result["citations"]) == 2

        prose = result["prose"]
        assert period_label in prose
        assert invoice_label in prose
        assert prose.startswith(f"Period {period_id.upper()} close:")
    finally:
        await _cleanup(pg_pool, ns_id, period_label, [period_node_id, invoice_node_id])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_narrative_unlinkable_claim_is_refused(
    pg_pool: Any, make_namespace: Any
) -> None:
    """A recognized_in edge whose subject label has NO live kg_nodes row
    (a dangling edge -- kg_edges carries no FK to kg_nodes, per graph.py's
    module docstring) contributes NO claim and NEVER appears in the
    narrative -- proving an unlinkable/uncited claim is refused, not
    fabricated."""
    ns_id = await make_namespace()
    period_id = f"2026-09-{uuid4().hex[:8]}"
    period_label = f"PERIOD:{period_id.upper()}"
    invoice_label = f"INVOICE:INV-{uuid4().hex[:8]}".upper()
    ghost_label = f"INVOICE:GHOST-{uuid4().hex[:8]}".upper()

    period_node_id = await _insert_kg_node(pg_pool, ns_id, period_label, "PERIOD")
    invoice_node_id = await _insert_kg_node(pg_pool, ns_id, invoice_label, "INVOICE")
    # Real edge (resolves) + a dangling edge whose subject was never created
    # as a kg_nodes row (legitimate: kg_edges has no FK to kg_nodes).
    await _insert_kg_edge(pg_pool, ns_id, invoice_label, "recognized_in", period_label)
    await _insert_kg_edge(pg_pool, ns_id, ghost_label, "recognized_in", period_label)

    try:
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            result = await close_narrative.do_generate_close_narrative(
                conn, namespace_id=ns_id, period_id=period_id
            )

        # Only the two REAL nodes (PERIOD + the one real INVOICE) ever became
        # claims -- the ghost edge never produced a claim to drop in the
        # first place, and none of its text reaches the prose.
        assert len(result["citations"]) == 2
        assert result["dropped"] == []
        cited_ids = {c["node_id"] for c in result["citations"]}
        assert str(period_node_id) in cited_ids
        assert str(invoice_node_id) in cited_ids

        assert "GHOST" not in result["prose"]
        assert ghost_label not in result["prose"]
    finally:
        await _cleanup(pg_pool, ns_id, period_label, [period_node_id, invoice_node_id])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_narrative_no_facts_yields_empty_prose(
    pg_pool: Any, make_namespace: Any
) -> None:
    """A period with no PERIOD node and no recognized_in facts at all ->
    empty prose, no citations, nothing fabricated out of thin air."""
    ns_id = await make_namespace()
    period_id = f"never-existed-{uuid4().hex}"

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        result = await close_narrative.do_generate_close_narrative(
            conn, namespace_id=ns_id, period_id=period_id
        )

    assert result["prose"] == ""
    assert result["citations"] == []
    assert result["dropped"] == []


@pytest.mark.asyncio
async def test_close_narrative_rejects_empty_period_id() -> None:
    """Pure validation -- runs before any DB access, so no pg_pool needed."""
    with pytest.raises(ValueError):
        await close_narrative.do_generate_close_narrative(
            None, namespace_id=str(uuid4()), period_id="   "
        )
