"""D34a — the D365 publisher prefix is validated configuration, not a source literal.

Covers (a) both field-name shapes, (b) the injection guard — a malformed prefix is
rejected BEFORE any SQL is built, and (c) the unset case (validate-on-use, not on
import).
"""

from __future__ import annotations

import asyncio
import importlib
import re
from typing import Any

import pytest

from nce.config import DeploymentConfigurationError, cfg
from nce.vertical_modules.sales import read_model
from nce.vertical_modules.sales.source_adapters import d365

# Deliberately NOT the old hardcoded prefix: a test asserting the old value would
# pass whether or not the seam exists (§6.4 positive control).
TEST_PREFIX = "zzq"

MALFORMED = [
    "a' OR '1'='1",
    "a'--",
    "a;DROP TABLE sales_read_model",
    "two words",
    "Contoso",
    "has-dash",
    "9leading",
    "dollar$",
    "x" * 33,
]


@pytest.fixture
def prefix(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", TEST_PREFIX)
    return TEST_PREFIX


class _RecordingConn:
    """Records every query text handed to it, so we can prove none was."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.queries.append(query)
        return []

    async def fetchrow(self, query: str, *args: Any) -> None:
        self.queries.append(query)
        return None


# ── (a) both shapes ──────────────────────────────────────────────────────────
def test_prepended_shape(prefix: str) -> None:
    assert d365.prefixed_field("industry") == "zzq_industry"
    assert d365.prefixed_field("customerneeds") == "zzq_customerneeds"


def test_infixed_lookup_shape(prefix: str) -> None:
    built = d365.prefixed_field("opportunityid_value", lookup=True)
    assert built == "_zzq_opportunityid_value"
    # What a naive prepend-only builder produces. It errors nowhere and returns no
    # rows, which is precisely the defect D34a fixes.
    assert built != "zzq__opportunityid_value"


def test_select_fields_use_the_configured_prefix(prefix: str) -> None:
    fields = d365._select_fields()
    assert "zzq_industry" in fields["accounts"]
    assert "_zzq_opportunityid_value" in fields["incidents"]
    assert "zzq_estrecurringmonthly" in fields["opportunities"]
    assert "zzq_subject@OData.Community.Display.V1.FormattedValue" in fields["opportunities"]
    flat = [f for fs in fields.values() for f in fs]
    # Positive form: the publisher-scoped fields in the whole $select map are
    # exactly these seven, every one of them built from the configured prefix.
    assert {f for f in flat if TEST_PREFIX in f} == {
        f"{TEST_PREFIX}_industry",
        f"{TEST_PREFIX}_estrecurringmonthly",
        f"{TEST_PREFIX}_estrecurringmonthly_base",
        f"{TEST_PREFIX}_customerneeds",
        f"{TEST_PREFIX}_jobdescription",
        f"{TEST_PREFIX}_subject@OData.Community.Display.V1.FormattedValue",
        f"_{TEST_PREFIX}_opportunityid_value",
    }


def test_sql_expressions_use_the_configured_prefix(prefix: str) -> None:
    sql = read_model._rec_num_sql(prefix)
    assert "source_json->>'zzq_estrecurringmonthly'" in sql
    assert "source_json->>'zzq_estrecurringmonthly_base'" in sql
    # Positive form: every JSON key the expression reaches into is prefixed.
    refs = re.findall(r"source_json->>'([^']*)'", sql)
    assert set(refs) == {
        f"{TEST_PREFIX}_estrecurringmonthly",
        f"{TEST_PREFIX}_estrecurringmonthly_base",
    }, refs
    assert read_model._subject_fv(prefix).startswith("zzq_subject@OData")


def test_classification_reads_the_configured_field_names(prefix: str) -> None:
    assert read_model.classify_it_av({read_model._subject_fv(prefix): "it"}) == "it"
    assert read_model.classify_it_av({"zzq_customerneeds": "Videobar til moterom"}) == "av"
    # the branded "<prefix> 365" IT term follows the prefix too
    assert read_model.classify_it_av({"zzq_jobdescription": "zzq 365 migrering"}) == "it"


# ── (b) THE INJECTION GUARD ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", MALFORMED)
def test_malformed_prefix_is_rejected_by_every_builder(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", bad)
    with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX"):
        d365.publisher_prefix()
    with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX"):
        d365.prefixed_field("industry")
    with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX"):
        d365._select_fields()
    # Every SQL-text builder raises instead of returning a string, so no query text
    # containing the hostile value is ever constructed.
    for build in (read_model._rec_sql, read_model._rec_num_sql, read_model._subject_fv):
        with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX"):
            build(bad)


@pytest.mark.parametrize("bad", ["a' OR '1'='1", "a;DROP TABLE sales_read_model", "two words"])
def test_no_sql_reaches_the_connection_for_a_malformed_prefix(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """The rejection happens BEFORE any SQL is built, let alone executed."""
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", bad)
    ns = "00000000-0000-0000-0000-000000000000"
    for helper in (read_model.manager_dashboard_helper, read_model.stats_dashboard_helper):
        conn = _RecordingConn()
        with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX"):
            asyncio.run(helper(conn, ns))
        assert conn.queries == [], f"{helper.__name__} built SQL from {bad!r}: {conn.queries}"


def test_the_guard_does_not_sanitise_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", "ac'me")
    with pytest.raises(DeploymentConfigurationError) as exc:
        d365.publisher_prefix()
    assert "NCE_D365_PUBLISHER_PREFIX" in str(exc.value)
    # D49b: and it is NOT a ValueError — that is the whole point. A ValueError
    # here would keep mapping to -32602/422, i.e. "you sent something wrong",
    # for a key only the operator can set.
    assert not isinstance(exc.value, ValueError)
    assert exc.value.config_key == "NCE_D365_PUBLISHER_PREFIX"
    # no repaired value is offered anywhere in the failure path
    assert d365._validate_prefix("acme") == "acme"


# ── (c) the unset case: validate on use, never at import ─────────────────────
def test_unset_prefix_raises_on_use_naming_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", "")
    with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX is not set"):
        d365.publisher_prefix()
    with pytest.raises(DeploymentConfigurationError, match="NCE_D365_PUBLISHER_PREFIX is not set"):
        read_model.classify_it_av({"name": "anything"})


def test_unset_prefix_does_not_break_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with no D365 integration must import the sales module cleanly."""
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", "")
    assert importlib.import_module("nce.vertical_modules.sales.read_model") is read_model
    assert importlib.import_module("nce.vertical_modules.sales.source_mode") is not None
