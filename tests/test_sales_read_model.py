"""Integration tests for nce/vertical_modules/sales/read_model.py.

Verifies:
  1. do_list_customers, do_customer_profile, do_sales_overview
  2. do_seller_detail, do_sales_dashboard, do_sales_stats, do_sales_manager
  3. do_list_agreements, do_agreement_detail, do_quote_detail
  4. do_get_targets, do_set_target
  5. strict RLS tenant isolation (verifying no cross-namespace bleed)
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.config import cfg
from nce.vertical_modules.sales.read_model import (
    classify_it_av,
    do_agreement_detail,
    do_customer_profile,
    do_get_targets,
    do_list_agreements,
    do_list_customers,
    do_quote_detail,
    do_sales_dashboard,
    do_sales_manager,
    do_sales_overview,
    do_sales_stats,
    do_seller_detail,
    do_set_target,
    segment_of,
)

# ---------------------------------------------------------------------------
# Unit tests for classification helpers
# ---------------------------------------------------------------------------


def test_classify_it_av() -> None:
    # Test via subject display formatted value
    sj1 = {f"{_TEST_PREFIX}_subject@OData.Community.Display.V1.FormattedValue": "av; smartbygg"}
    assert classify_it_av(sj1) == "av"

    sj2 = {f"{_TEST_PREFIX}_subject@OData.Community.Display.V1.FormattedValue": "it"}
    assert classify_it_av(sj2) == "it"

    sj3 = {f"{_TEST_PREFIX}_subject@OData.Community.Display.V1.FormattedValue": "av; it"}
    assert classify_it_av(sj3) == "begge"

    # Test via owner title
    assert classify_it_av({}, "Lead IT Architect") == "it"

    # Test via regex matching in text fields
    sj4 = {"name": "Levering av Neat videobar til auditorium"}
    assert classify_it_av(sj4) == "av"

    sj5 = {"description": "Azure migration licenses and firewall installation"}
    assert classify_it_av(sj5) == "it"

    sj6 = {f"{_TEST_PREFIX}_customerneeds": "Microsoft 365 setup and neat meeting screen"}
    assert classify_it_av(sj6) == "begge"

    sj7 = {"name": "Custom setup"}
    assert classify_it_av(sj7) == "ukjent"


def test_segment_of() -> None:
    assert segment_of("NACE (55) Hotell og restaurant") == "hospitality"
    assert segment_of("NACE (85) Grunnskole") == "education"
    assert segment_of("Some university client", "Oslo Universitet") == "education"
    assert segment_of("NACE (62) IT-tjenester") == "workplace"
    assert segment_of(None, "Standard AS") == "ukjent"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:
    class _EngineStub:
        pg_pool: asyncpg.Pool

    stub = _EngineStub()
    stub.pg_pool = pg_pool
    return stub


async def _insert_sales_record(
    conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
    entity: str,
    source_id: str,
    name: str,
    source_json: dict[str, Any],
    manual: dict[str, Any] | None = None,
    is_deleted: bool = False,
    modifiedon: datetime.datetime | None = None,
) -> None:
    if modifiedon is None:
        modifiedon = datetime.datetime.now(datetime.timezone.utc)
    if "name" not in source_json:
        source_json = dict(source_json)
        source_json["name"] = name
    manual_json = manual or {}
    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, source_json, manual, is_deleted, modifiedon, synced_at)
        VALUES
            ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, now())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            source_json = EXCLUDED.source_json,
            manual = EXCLUDED.manual,
            is_deleted = EXCLUDED.is_deleted,
            modifiedon = EXCLUDED.modifiedon,
            updated_at = now()
        """,
        namespace_id,
        entity,
        source_id,
        name,
        json.dumps(source_json),
        json.dumps(manual_json),
        is_deleted,
        modifiedon,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_read_model_crud_and_rls(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    make_namespace: Any,
) -> None:
    engine = _make_engine_stub(pg_pool)
    other_ns = await make_namespace()

    # 1. Seed some accounts (customers) in both namespaces
    acc_id_1 = str(uuid.uuid4())
    acc_id_2 = str(uuid.uuid4())
    acc_id_other = str(uuid.uuid4())

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            # Seed under primary namespace context
            await set_namespace_context(conn, namespace_id)
            await _insert_sales_record(
                conn,
                namespace_id,
                "accounts",
                acc_id_1,
                "Acme Corp",
                {
                    "accountid": acc_id_1,
                    "address1_city": "Oslo",
                    f"{_TEST_PREFIX}_industry": "NACE (62)",
                },
            )
            await _insert_sales_record(
                conn,
                namespace_id,
                "accounts",
                acc_id_2,
                "Cyberdyne Systems",
                {
                    "accountid": acc_id_2,
                    "address1_city": "Trondheim",
                    f"{_TEST_PREFIX}_industry": "NACE (55)",
                },
            )

            # Seed under other namespace context
            await set_namespace_context(conn, other_ns)
            await _insert_sales_record(
                conn,
                other_ns,
                "accounts",
                acc_id_other,
                "Other Corp",
                {
                    "accountid": acc_id_other,
                    "address1_city": "Bergen",
                    f"{_TEST_PREFIX}_industry": "NACE (85)",
                },
            )

    # 2. Test do_list_customers shape and RLS isolation
    res = await do_list_customers(engine, {"namespace_id": str(namespace_id)})
    assert res["entity"] == "accounts"
    assert res["total"] == 2
    assert len(res["items"]) == 2
    # Check that other_ns customer is not leaked
    names = {item.get("name") for item in res["items"]}
    assert "Acme Corp" in names
    assert "Cyberdyne Systems" in names
    assert "Other Corp" not in names

    # Test search query
    res_search = await do_list_customers(engine, {"namespace_id": str(namespace_id), "q": "cyber"})
    assert res_search["total"] == 1
    assert res_search["items"][0]["name"] == "Cyberdyne Systems"

    # Test do_list_customers for other_ns
    res_other = await do_list_customers(engine, {"namespace_id": str(other_ns)})
    assert res_other["total"] == 1
    assert res_other["items"][0]["name"] == "Other Corp"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_customer_profile_resolution(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    acc_id = str(uuid.uuid4())
    contact_id = str(uuid.uuid4())
    opp_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    fl_id = str(uuid.uuid4())

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)

            # Seed account
            await _insert_sales_record(
                conn,
                namespace_id,
                "accounts",
                acc_id,
                "Acme Corp",
                {"accountid": acc_id, "address1_city": "Oslo"},
            )
            # Seed contact linked to account
            await _insert_sales_record(
                conn,
                namespace_id,
                "contacts",
                contact_id,
                "John Doe",
                {
                    "contactid": contact_id,
                    "_parentcustomerid_value": acc_id,
                    "fullname": "John Doe",
                },
            )
            # Seed opportunity linked to account
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_id,
                "Big Deal",
                {
                    "opportunityid": opp_id,
                    "_customerid_value": acc_id,
                    "statecode": "0",
                    "estimatedvalue": "500000",
                },
            )
            # Seed case (incident) linked to account
            await _insert_sales_record(
                conn,
                namespace_id,
                "incidents",
                case_id,
                "Broken screen",
                {
                    "incidentid": case_id,
                    "_customerid_value": acc_id,
                    "statecode": "0",
                    "title": "Broken screen",
                },
            )
            # Seed functional location
            await _insert_sales_record(
                conn,
                namespace_id,
                "functionallocations",
                fl_id,
                "Oslo Headquarters",
                {
                    "msdyn_functionallocationid": fl_id,
                    "msdyn_name": "Oslo HQ",
                    "msdyn_address1": "Storgata 1",
                    "msdyn_city": "Oslo",
                },
            )
            # Seed customer asset linking functional location and account
            await _insert_sales_record(
                conn,
                namespace_id,
                "customerassets",
                asset_id,
                "Main Screen",
                {
                    "msdyn_customerassetid": asset_id,
                    "_msdyn_account_value": acc_id,
                    "_msdyn_functionallocation_value": fl_id,
                },
            )

    # Resolve customer profile
    profile = await do_customer_profile(
        engine, {"namespace_id": str(namespace_id), "accountid": acc_id}
    )
    assert "company" in profile
    assert profile["company"]["name"] == "Acme Corp"

    assert len(profile["contacts"]) == 1
    assert profile["contacts"][0]["fullname"] == "John Doe"

    assert len(profile["opportunities"]) == 1
    assert profile["opportunities"][0]["name"] == "Big Deal"

    assert len(profile["cases"]) == 1
    assert profile["cases"][0]["title"] == "Broken screen"

    assert len(profile["locations"]) == 1
    assert profile["locations"][0]["name"] == "Oslo HQ"
    assert profile["locations"][0]["address"] == "Storgata 1, Oslo"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_dashboard_and_overview(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    ref_date = datetime.date(2026, 6, 23)

    user_guid = str(uuid.uuid4())
    opp_open_id = str(uuid.uuid4())
    opp_won_id = str(uuid.uuid4())
    opp_lost_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    appt_id = str(uuid.uuid4())

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)

            # Seed system user
            await _insert_sales_record(
                conn,
                namespace_id,
                "systemusers",
                user_guid,
                "Sarah Connor",
                {
                    "systemuserid": user_guid,
                    "fullname": "Sarah Connor",
                    "isdisabled": "False",
                    "title": "Account Manager",
                },
            )

            # Seed opportunities
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_open_id,
                "Terminator Prevention",
                {
                    "opportunityid": opp_open_id,
                    "statecode": "0",
                    "estimatedvalue": "900000",
                    "salesstagecode": "1-Kvalifisere",
                    "estimatedclosedate": "2026-07-15",
                    "_ownerid_value": user_guid,
                    "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Sarah Connor",
                },
            )
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_won_id,
                "Won Upgrade Project",
                {
                    "opportunityid": opp_won_id,
                    "statecode": "1",
                    "estimatedvalue": "400000",
                    "_ownerid_value": user_guid,
                    "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Sarah Connor",
                },
            )
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_lost_id,
                "Lost Project",
                {
                    "opportunityid": opp_lost_id,
                    "statecode": "2",
                    "estimatedvalue": "150000",
                    "_ownerid_value": user_guid,
                    "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Sarah Connor",
                },
            )

            # Seed incident
            await _insert_sales_record(
                conn,
                namespace_id,
                "incidents",
                case_id,
                "T-800 unit malfunction",
                {
                    "incidentid": case_id,
                    "statecode": "0",
                    "title": "T-800 unit malfunction",
                    "prioritycode": "1",
                    "ticketnumber": "CAS-998",
                    "createdon": "2026-06-22T12:00:00Z",
                    "_ownerid_value": user_guid,
                },
            )

            # Seed appointment
            await _insert_sales_record(
                conn,
                namespace_id,
                "appointments",
                appt_id,
                "Resistance briefing",
                {
                    "activityid": appt_id,
                    "statecode": "1",  # completed
                    "scheduledstart": "2026-06-23T09:00:00Z",
                    "_ownerid_value": user_guid,
                },
            )

    # 1. Test sales overview (pipeline aggregates by stage)
    overview = await do_sales_overview(engine, {"namespace_id": str(namespace_id)})
    assert len(overview["stages"]) == 1
    assert overview["stages"][0]["stage"] == "1-Kvalifisere"
    assert overview["stages"][0]["count"] == 1
    assert overview["stages"][0]["value"] == 900000.0

    # 2. Test seller detail
    seller = await do_seller_detail(
        engine, {"namespace_id": str(namespace_id), "user": "sarah-connor"}
    )
    assert seller["user"] == "sarah-connor"
    assert seller["owner"] == user_guid
    assert len(seller["pipeline"]) == 1
    assert seller["pipeline"][0]["value"] == 900000.0
    assert seller["wonCount"] == 1
    assert seller["wonValue"] == 400000.0

    # 3. Test dashboard - mine
    dash_mine = await do_sales_dashboard(
        engine,
        {
            "namespace_id": str(namespace_id),
            "user": "sarah-connor",
            "today": ref_date.isoformat(),
        },
    )
    assert dash_mine["scope"] == "mine"
    assert dash_mine["user"] == "sarah-connor"
    assert dash_mine["pipeline"]["openCount"] == 1
    assert dash_mine["pipeline"]["openValue"] == 900000.0
    assert dash_mine["pipeline"]["wonCount"] == 1
    assert dash_mine["pipeline"]["wonValue"] == 400000.0
    assert len(dash_mine["closingSoon"]) == 1
    assert dash_mine["closingSoon"][0]["name"] == "Terminator Prevention"
    assert len(dash_mine["openCases"]["items"]) == 1

    # 4. Test dashboard - team
    dash_team = await do_sales_dashboard(
        engine,
        {
            "namespace_id": str(namespace_id),
            "user": "admin",
            "today": ref_date.isoformat(),
        },
    )
    assert dash_team["scope"] == "team"
    assert dash_team["user"] == "admin"
    assert dash_team["pipeline"]["openCount"] == 1
    assert dash_team["pipeline"]["openValue"] == 900000.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_manager_targets_and_risk(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    ref_date = datetime.date(2026, 6, 23)
    user_guid = str(uuid.uuid4())
    opp_stale_id = str(uuid.uuid4())

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)

            # Seed system user
            await _insert_sales_record(
                conn,
                namespace_id,
                "systemusers",
                user_guid,
                "John Connor",
                {
                    "systemuserid": user_guid,
                    "fullname": "John Connor",
                    "isdisabled": "False",
                    "title": "Salgskonsulent",
                },
            )

            # Seed a stale open opportunity (modified > 60 days ago)
            stale_modified = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                days=70
            )
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_stale_id,
                "Stale Opportunity",
                {
                    "opportunityid": opp_stale_id,
                    "statecode": "0",
                    "estimatedvalue": "300000",
                    "salesstagecode": "2-Utvikle",
                    "estimatedclosedate": "2026-08-01",
                    "_ownerid_value": user_guid,
                    "_ownerid_value@OData.Community.Display.V1.FormattedValue": "John Connor",
                },
                modifiedon=stale_modified,
            )

    # 1. Set won target for John Connor
    res_set = await do_set_target(
        engine,
        {
            "namespace_id": str(namespace_id),
            "owner_slug": "john-connor",
            "metric": "won_monthly",
            "value": 500000.0,
        },
    )
    assert res_set["ok"] is True

    # 2. Get targets
    targets = await do_get_targets(engine, {"namespace_id": str(namespace_id)})
    assert "john-connor" in targets
    assert targets["john-connor"]["won_monthly"] == 500000.0

    # 3. Test manager dashboard
    m_dash = await do_sales_manager(
        engine,
        {
            "namespace_id": str(namespace_id),
            "period": "month",
            "today": ref_date.isoformat(),
        },
    )
    assert len(m_dash["byAm"]) == 1
    assert m_dash["byAm"][0]["name"] == "John Connor"
    assert m_dash["byAm"][0]["targets"]["won"] == 500000.0
    assert m_dash["team"]["openProjectValue"] == 300000.0

    # Risk metrics validation
    assert m_dash["risk"]["count"] == 1
    assert m_dash["risk"]["value"] == 300000.0
    assert m_dash["risk"]["stale"]["count"] == 1
    assert len(m_dash["risk"]["items"]) == 1
    assert m_dash["risk"]["items"][0]["name"] == "Stale Opportunity"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_stats_trend_and_classification(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    ref_date = datetime.date(2026, 6, 23)
    user_guid = str(uuid.uuid4())
    opp_av_id = str(uuid.uuid4())
    opp_it_id = str(uuid.uuid4())

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)

            # Seed system user
            await _insert_sales_record(
                conn,
                namespace_id,
                "systemusers",
                user_guid,
                "Sarah Connor",
                {
                    "systemuserid": user_guid,
                    "fullname": "Sarah Connor",
                    "isdisabled": "False",
                    "title": "Account Manager",
                },
            )

            # AV Opportunity
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_av_id,
                "AV installation in classroom",
                {
                    "opportunityid": opp_av_id,
                    "statecode": "0",
                    "estimatedvalue": "200000",
                    f"{_TEST_PREFIX}_customerneeds": "Videobar neatly installed in room",
                    "_ownerid_value": user_guid,
                },
            )

            # IT Opportunity
            await _insert_sales_record(
                conn,
                namespace_id,
                "opportunities",
                opp_it_id,
                "Office 365 licensing migration",
                {
                    "opportunityid": opp_it_id,
                    "statecode": "0",
                    "estimatedvalue": "150000",
                    "description": "Microsoft 365 cloud environment migration",
                    "_ownerid_value": user_guid,
                },
            )

    stats = await do_sales_stats(
        engine,
        {
            "namespace_id": str(namespace_id),
            "period": "month",
            "today": ref_date.isoformat(),
        },
    )
    assert stats["byItAv"]["av"]["openCount"] == 1
    assert stats["byItAv"]["av"]["openValue"] == 200000.0
    assert stats["byItAv"]["it"]["openCount"] == 1
    assert stats["byItAv"]["it"]["openValue"] == 150000.0
    assert stats["coverage"]["n"] == 2
    assert stats["coverage"]["itav"] == 100  # Both classified


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sales_agreements_and_quotes(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _make_engine_stub(pg_pool)
    agr_id = str(uuid.uuid4())
    quote_id = str(uuid.uuid4())

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)

            # Seed agreement
            await _insert_sales_record(
                conn,
                namespace_id,
                "agreements",
                agr_id,
                "Standard SLA 2026",
                {
                    "msdyn_agreementid": agr_id,
                    "msdyn_name": "Standard SLA 2026",
                },
            )

            # Seed quote
            await _insert_sales_record(
                conn,
                namespace_id,
                "quotes",
                quote_id,
                "Quote for AV system design",
                {
                    "quoteid": quote_id,
                    "name": "Quote for AV system design",
                },
            )

    # 1. Test do_list_agreements
    agrs = await do_list_agreements(engine, {"namespace_id": str(namespace_id)})
    assert agrs["total"] == 1
    assert agrs["items"][0]["msdyn_name"] == "Standard SLA 2026"

    # 2. Test do_agreement_detail
    agr_detail = await do_agreement_detail(
        engine, {"namespace_id": str(namespace_id), "agreementid": agr_id}
    )
    assert agr_detail["msdyn_name"] == "Standard SLA 2026"

    # 3. Test do_quote_detail
    quote_detail = await do_quote_detail(
        engine, {"namespace_id": str(namespace_id), "quoteid": quote_id}
    )
    assert quote_detail["name"] == "Quote for AV system design"


# ── D34a: the D365 publisher prefix is configuration, not a source literal ──
# Deliberately NOT the old hardcoded value, so these fixtures fail if the seam
# is reverted to a literal (§6.4 positive control).
_TEST_PREFIX = "zzq"


@pytest.fixture(autouse=True)
def _d34a_publisher_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", _TEST_PREFIX)
