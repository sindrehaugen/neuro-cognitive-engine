"""
tests/unit/test_degradation_register.py
=======================================
Wave I-5: Comprehensive unit test suite and standing positive controls (U18)
for the Degradation Register and GET /api/health/degradations endpoint.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from nce.admin_handlers.health import get_degradations
from nce.degradation import (
    DegradationRecord,
    DegradationRegister,
    get_degradation_register,
    record_degradation,
)


@pytest.fixture(autouse=True)
def clean_degradation_register():
    """Ensure every test begins and ends with an empty degradation register."""
    reg = get_degradation_register()
    reg.clear()
    yield
    reg.clear()


class TestDegradationRegisterUnit:
    """Core domain logic tests for DegradationRegister and DegradationRecord."""

    def test_degradation_record_structure(self) -> None:
        rec = DegradationRecord(
            namespace_id="ns-test-01",
            engine="project",
            code="sales_baseline_unavailable",
            count=3,
            first_seen_at="2026-09-06T12:00:00Z",
            last_seen_at="2026-09-06T12:05:00Z",
            detail="Sales baseline not found",
            onboarding_hint="Connect Sales signed quote freeze",
        )
        d = rec.to_dict()
        assert d["namespace_id"] == "ns-test-01"
        assert d["engine"] == "project"
        assert d["code"] == "sales_baseline_unavailable"
        assert d["count"] == 3
        assert d["detail"] == "Sales baseline not found"
        assert d["onboarding_hint"] == "Connect Sales signed quote freeze"

    def test_record_creates_and_increments_counter(self) -> None:
        reg = DegradationRegister()
        ns = str(uuid.uuid4())

        r1 = reg.record(ns, "project", "code_a", detail="detail 1", onboarding_hint="hint 1")
        assert r1.count == 1
        assert r1.detail == "detail 1"
        assert r1.onboarding_hint == "hint 1"
        assert reg.total_count(ns) == 1

        r2 = reg.record(ns, "project", "code_a", detail="detail 2", onboarding_hint="hint 2")
        assert r2.count == 2
        assert r2.detail == "detail 2"
        assert r2.onboarding_hint == "hint 2"
        assert reg.total_count(ns) == 2

    def test_namespace_isolation(self) -> None:
        reg = DegradationRegister()
        ns1 = str(uuid.uuid4())
        ns2 = str(uuid.uuid4())

        reg.record(ns1, "project", "code_a")
        reg.record(ns1, "project", "code_b")
        reg.record(ns2, "agreements", "gl_unavailable")

        assert reg.total_count(ns1) == 2
        assert reg.total_count(ns2) == 1
        assert reg.total_count() == 3

        degs_ns1 = reg.get_degradations(ns1)
        assert len(degs_ns1) == 2
        assert {d["code"] for d in degs_ns1} == {"code_a", "code_b"}

        degs_ns2 = reg.get_degradations(ns2)
        assert len(degs_ns2) == 1
        assert degs_ns2[0]["code"] == "gl_unavailable"

    def test_summary_aggregation(self) -> None:
        reg = DegradationRegister()
        ns1 = "ns-alpha"
        ns2 = "ns-beta"

        reg.record(ns1, "project", "code_1")
        reg.record(ns1, "project", "code_1")
        reg.record(ns2, "agreements", "code_2")

        summary = reg.get_summary()
        assert summary["total_events"] == 3
        assert summary["unique_degradations"] == 2
        assert summary["by_engine"] == {"project": 2, "agreements": 1}
        assert summary["by_namespace"] == {"ns-alpha": 2, "ns-beta": 1}

    def test_clear_all_and_by_namespace(self) -> None:
        reg = DegradationRegister()
        ns1 = "ns-1"
        ns2 = "ns-2"

        reg.record(ns1, "eng", "code_1")
        reg.record(ns2, "eng", "code_2")

        reg.clear(ns1)
        assert reg.total_count(ns1) == 0
        assert reg.total_count(ns2) == 1

        reg.clear()
        assert reg.total_count() == 0


class TestDegradationEndpoint:
    """Tests for GET /api/health/degradations route and handler."""

    @pytest.mark.asyncio
    async def test_get_degradations_empty(self) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/health/degradations",
            "query_string": b"",
        }
        req = Request(scope)
        resp: JSONResponse = await get_degradations(req)

        import json

        body = json.loads(resp.body.decode("utf-8"))
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert body["total_degradations"] == 0
        assert body["degradations"] == []
        assert body["summary"]["total_events"] == 0

    @pytest.mark.asyncio
    async def test_get_degradations_filtered_by_namespace(self) -> None:
        ns1 = str(uuid.uuid4())
        ns2 = str(uuid.uuid4())

        record_degradation(ns1, "project", "sales_baseline_unavailable", detail="test 1")
        record_degradation(ns2, "agreements", "gl_unavailable", detail="test 2")

        # Query with ns1
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/health/degradations",
            "query_string": f"namespace_id={ns1}".encode(),
        }
        req = Request(scope)
        resp: JSONResponse = await get_degradations(req)

        import json

        body = json.loads(resp.body.decode("utf-8"))
        assert resp.status_code == 200
        assert body["total_degradations"] == 1
        assert len(body["degradations"]) == 1
        assert body["degradations"][0]["namespace_id"] == ns1
        assert body["degradations"][0]["code"] == "sales_baseline_unavailable"

    def test_route_mounted_on_admin_app(self) -> None:
        from nce.admin_app import app

        matched = [
            r
            for r in app.routes
            if getattr(r, "path", None) == "/api/health/degradations"
            and "GET" in getattr(r, "methods", set())
        ]
        assert len(matched) == 1, "/api/health/degradations must be mounted with GET method"


class TestDegradationPathInstrumentation:
    """Verify that vertical module grace-degradation paths record into the register."""

    @pytest.mark.asyncio
    async def test_project_convert_records_sales_baseline_degradation(self) -> None:
        from nce.vertical_modules.project.convert import do_convert_signed_quote

        ns = uuid.uuid4()
        mock_engine = MagicMock()
        mock_engine.pg_pool = MagicMock()

        # Mock database session
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=None)

        tx_ctx = MagicMock()
        tx_ctx.__aenter__ = AsyncMock(return_value=None)
        tx_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=tx_ctx)

        pool_ctx = AsyncMock()
        pool_ctx.__aenter__.return_value = mock_conn
        pool_ctx.__aexit__.return_value = None

        with patch("nce.vertical_modules.project.convert.scoped_pg_session", return_value=pool_ctx):
            with patch(
                "nce.vertical_modules.project.convert.assert_owner",
                new=AsyncMock(return_value=None),
            ):
                with patch(
                    "nce.vertical_modules.project.convert._read_signed_baseline",
                    side_effect=NotImplementedError("Sales not built"),
                ):
                    result = await do_convert_signed_quote(
                        mock_engine,
                        {
                            "namespace_id": str(ns),
                            "quote_id": "Q-TEST-99",
                            "signed_by": "Test User",
                        },
                    )

        assert result["degraded"] is True
        assert "sales_baseline_unavailable" in result["degraded_reasons"]

        reg = get_degradation_register()
        records = reg.get_degradations(str(ns))
        codes = {r["code"] for r in records}
        assert "sales_baseline_unavailable" in codes
        assert "no_bom_lines_in_graph" in codes

    @pytest.mark.asyncio
    async def test_agreements_coverage_records_gl_unavailable_degradation(self) -> None:
        from nce.vertical_modules.agreements.coverage import do_coverage_matrix

        ns = uuid.uuid4()
        mock_engine = MagicMock()
        mock_engine.pg_pool = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])

        pool_ctx = AsyncMock()
        pool_ctx.__aenter__.return_value = mock_conn
        pool_ctx.__aexit__.return_value = None

        with patch(
            "nce.vertical_modules.agreements.coverage.scoped_pg_session", return_value=pool_ctx
        ):
            # _read_economy_gl_rows raises NotImplementedError by default
            result = await do_coverage_matrix(mock_engine, {"namespace_id": str(ns)})

        assert result["status"] == "gl_unavailable"
        reg = get_degradation_register()
        records = reg.get_degradations(str(ns))
        assert any(r["code"] == "gl_unavailable" and r["engine"] == "agreements" for r in records)

    @pytest.mark.asyncio
    async def test_resources_planner_records_unobserved_outcomes(self) -> None:
        from nce.vertical_modules.resources.planner import do_plan_allocation

        ns = uuid.uuid4()
        mock_engine = MagicMock()
        mock_engine.pg_pool = MagicMock()

        mock_conn = AsyncMock()
        # 1 candidate found
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "kind": "employee",
                    "ref_id": "E100",
                    "display_name": "Test Tech",
                    "attrs": {"skills": ["av_install"], "base_location": "Oslo"},
                }
            ]
        )
        # no conflict, 0 current load
        mock_conn.fetchval = AsyncMock(return_value=None)
        # 0 historical jobs in cognitive ledger
        mock_conn.fetchrow = AsyncMock(
            return_value={"total_jobs": 0, "avg_rating": None, "avg_quality": None}
        )

        pool_ctx = AsyncMock()
        pool_ctx.__aenter__.return_value = mock_conn
        pool_ctx.__aexit__.return_value = None

        with patch(
            "nce.vertical_modules.resources.planner.scoped_pg_session", return_value=pool_ctx
        ):
            await do_plan_allocation(
                mock_engine,
                {
                    "namespace_id": str(ns),
                    "namespace_metadata": {"resources": {"enabled": True}},
                    "demand_kind": "project",
                    "starts_at": "2026-09-10T08:00:00Z",
                    "ends_at": "2026-09-10T16:00:00Z",
                    "required_skills": ["av_install"],
                },
            )

        reg = get_degradation_register()
        records = reg.get_degradations(str(ns))
        hist_records = [r for r in records if r["code"] == "unobserved_outcome_history"]
        assert len(hist_records) == 1
        assert "0 attributed outcomes; record 5 to unlock" in hist_records[0]["onboarding_hint"]


class TestDegradationPositiveControls:
    """Standing positive controls (U18: a positive-control test beats a manual RED)."""

    def test_positive_control_ratchet_detects_non_empty_register(self) -> None:
        """Prove that if a degradation is recorded, the empty-register check fails loudly."""
        reg = get_degradation_register()
        assert reg.total_count() == 0

        # Inject synthetic degradation
        reg.record("ns-synthetic", "test_engine", "synthetic_degradation")
        assert reg.total_count() == 1

        # Assert contract failure
        with pytest.raises(AssertionError, match="degradation register non-empty"):
            total = reg.total_count()
            if total > 0:
                raise AssertionError(
                    f"break-degradations: degradation register non-empty: total={total}"
                )

        # Restore
        reg.clear()
        assert reg.total_count() == 0

    @pytest.mark.asyncio
    async def test_positive_control_endpoint_reflects_mutations(self) -> None:
        """Prove that the HTTP endpoint reflects live mutations to the register."""
        reg = get_degradation_register()
        assert reg.total_count() == 0

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/health/degradations",
            "query_string": b"",
        }
        req = Request(scope)

        import json

        resp = await get_degradations(req)
        assert json.loads(resp.body)["total_degradations"] == 0

        # Mutate
        reg.record("ns-pos", "engine_x", "code_x")
        resp2 = await get_degradations(req)
        body2 = json.loads(resp2.body)
        assert body2["total_degradations"] == 1
        assert body2["degradations"][0]["code"] == "code_x"

        # Restore
        reg.clear()
