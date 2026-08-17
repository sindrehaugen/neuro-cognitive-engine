"""
nce/vertical_modules/sales/read_model.py
=========================================
Native tenant-isolated sales read-model and query aggregations.

Replaces the steps_d365 sidecar aggregations with native read functions
backed by sales_read_model and sales_targets tables. All queries are
scoped via scoped_pg_session and explicit namespace_id parameters.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.sales.read_model")

# ── IT vs AV classification patterns (copied from steps_d365/classify.py) ─────
_AV_RE = re.compile(
    r"møterom|moterom|\bmtr\b|\bneat\b|projektor|prosjektor|lerret|skjerm|signage|\bled\b|"
    r"mikrofon|\bxlr\b|\bdsp\b|crestron|yealink|høyttaler|hoyttaler|videobar|"
    r"touchpanel|auditorium|streaming|lyd|kamera|av-anlegg|av-løsning|av-losning|"
    r"belysning|lysstyring|\blampe\b|\blamper\b|audio|akustikk|akustisk|\bheadset\b|headsett|"
    r"hodesett|jabra|logitech|\blogi\b|meetup|\bpoly\b|epson|barco|shure|sennheiser|"
    r"smartbygg|smart bygg|romkontroll|interaktiv tavle|touchskjerm|konferanserom|"
    r"møtesenter|motesenter|trådløs deling|tradlos deling|bordmic|\bdante\b|kontrollrom",
    re.IGNORECASE,
)
_IT_RE = re.compile(
    r"microsoft 365|bravo 365|\bm365\b|\bo365\b|azure|brannmur|firewall|\bswitch\b|"
    r"\bserver\b|migrering|it-drift|driftsavtale|nettverk|\bvpn\b|\bwifi\b|lisens|"
    r"\bsccm\b|intune|endpoint|backup|sharepoint|\bnce\b|"
    r"microsoft|windows|office 365|\bpc\b|pc-park|scanner|skanner|docking|laptop|"
    r"asset management|datasenter|tilgangskontroll|licens|\bdator\b|datorer|datamaskin|"
    r"cisco|meraki|\bdaas\b|fortinet|\bnis ?2\b|aksesspunkt",
    re.IGNORECASE,
)
_TEXT_FIELDS = ("name", "bravo_customerneeds", "bravo_jobdescription", "description")
_SUBJECT_FV = "bravo_subject@OData.Community.Display.V1.FormattedValue"


def classify_it_av(sj: dict[str, Any], owner_title: str | None = None) -> str:
    subj = sj.get(_SUBJECT_FV)
    if subj:
        parts = {p.strip().lower() for p in str(subj).split(";")}
        av_subj = "av" in parts or "smartbygg" in parts
        it_subj = "it" in parts
        if av_subj and it_subj:
            return "begge"
        if av_subj:
            return "av"
        if it_subj:
            return "it"
    if owner_title and "lead it" in owner_title.lower():
        return "it"
    text = " ".join(str(sj.get(k) or "") for k in _TEXT_FIELDS)
    has_av = bool(_AV_RE.search(text))
    has_it = bool(_IT_RE.search(text))
    name = str(sj.get("name") or "")
    if re.search(r"\bAV\b", name):
        has_av = True
    if re.search(r"\bIT\b", name):
        has_it = True
    if has_av and has_it:
        return "begge"
    if has_av:
        return "av"
    if has_it:
        return "it"
    return "ukjent"


_NACE_RE = re.compile(r"\((\d{2})\d*\)")
_EDU_TOKENS = (
    "høyskole",
    "hoyskole",
    "universitet",
    "university",
    "education",
    "steinerskole",
    "bjørknes",
    "bjornknes",
    "akademi",
    "skole",
)


def segment_of(industry: str | None, name: str | None = None) -> str:
    ind = (industry or "").lower()
    blob = ind + " " + (name or "").lower()
    m = _NACE_RE.search(industry or "")
    nace2 = m.group(1) if m else ""
    if nace2 in ("55", "56") or any(
        w in blob for w in ("hotell", "hotel", "apartments", "hospitality", "restaurant")
    ):
        return "hospitality"
    if nace2 == "85" or any(w in blob for w in _EDU_TOKENS):
        return "education"
    if industry:
        return "workplace"
    return "ukjent"


# ── Database value helpers ───────────────────────────────────────────────────
def _nz(v: Any) -> Any:
    return v.replace("\x00", "") if isinstance(v, str) else v


def _merged(row: dict[str, Any] | asyncpg.Record) -> dict[str, Any]:
    src = row["source_json"]
    if isinstance(src, str):
        src = json.loads(src)
    man = row["manual"]
    if isinstance(man, str):
        man = json.loads(man)
    out = {**(src or {}), **(man or {})}
    out["_id"] = row["id"]
    out["_deleted"] = row["is_deleted"]
    return out


def _fold_slug(navn: str) -> str:
    s = (navn or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "-".join(s.split())


def _num(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _is_sales_title(title: str | None) -> bool:
    t = (title or "").lower()
    return any(h in t for h in ("account", "sales", "selger", "salg"))


def _first_of_next(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def _period_range(period: str, today: date, offset: int = 0) -> tuple[str, str, str]:
    if period == "quarter":
        q0 = ((today.month - 1) // 3) * 3 + 1
        start = _first_of_next(date(today.year, q0, 1), -3 * offset)
        qn = (start.month - 1) // 3 + 1
        return f"{start.year}-Q{qn}", start.isoformat(), _first_of_next(start, 3).isoformat()
    if period in ("year", "ytd"):
        y = today.year - offset
        return str(y), date(y, 1, 1).isoformat(), date(y + 1, 1, 1).isoformat()
    start = _first_of_next(date(today.year, today.month, 1), -offset)
    return (
        f"{start.year}-{start.month:02d}",
        start.isoformat(),
        _first_of_next(start, 1).isoformat(),
    )


# ── Aggregation variables ────────────────────────────────────────────────────
_VAL = r"coalesce(nullif(source_json->>'estimatedvalue',''), nullif(source_json->>'estimatedvalue_base',''))"
_VAL_NUM = rf"(CASE WHEN {_VAL} ~ '^-?[0-9]+(\.[0-9]+)?$' THEN ({_VAL})::numeric END)"
_STAGE = r"coalesce(nullif(source_json->>'stepname',''), nullif(source_json->>'salesstagecode',''), 'Uten fase')"
_ACCT_NAME = r"source_json->>'_customerid_value@OData.Community.Display.V1.FormattedValue'"
_REC = r"coalesce(nullif(source_json->>'bravo_estrecurringmonthly',''), nullif(source_json->>'bravo_estrecurringmonthly_base',''))"
_REC_NUM = rf"(CASE WHEN {_REC} ~ '^-?[0-9]+(\.[0-9]+)?$' THEN ({_REC})::numeric END)"
_OWNER_FV = "source_json->>'_ownerid_value@OData.Community.Display.V1.FormattedValue'"


# ── Query Helpers ────────────────────────────────────────────────────────────
async def fetch_records_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    entity: str,
    *,
    q: str = "",
    size: int = 100,
    page: int = 0,
    include_deleted: bool = False,
) -> dict[str, Any]:
    ns_str = str(namespace_id)
    where = ["namespace_id = $1", "entity = $2"]
    args: list[Any] = [ns_str, entity]
    if not include_deleted:
        where.append("is_deleted = false")
    for term in [t for t in q.lower().split() if t]:
        args.append(f"%{term}%")
        where.append(f"lower(coalesce(name,'')) LIKE ${len(args)}")
    wsql = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) FROM sales_read_model WHERE {wsql}", *args)

    args.append(page * size)
    offset_param = f"${len(args)}"
    args.append(size)
    limit_param = f"${len(args)}"

    rows = await conn.fetch(
        f"SELECT id, source_json, manual, is_deleted FROM sales_read_model WHERE {wsql} "
        f"ORDER BY modifiedon DESC NULLS LAST, lower(coalesce(name,'')) ASC "
        f"OFFSET {offset_param} LIMIT {limit_param}",
        *args,
    )
    items = [_merged(r) for r in rows]
    pages = (total + size - 1) // size if total and size else 0
    return {
        "entity": entity,
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": pages,
    }


async def fetch_one_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    entity: str,
    source_id: str,
) -> dict[str, Any] | None:
    if not source_id:
        return None
    ns_str = str(namespace_id)
    row = await conn.fetchrow(
        "SELECT id, source_json, manual, is_deleted, synced_at "
        "FROM sales_read_model WHERE namespace_id = $1 AND entity = $2 AND source_id = $3",
        ns_str,
        entity,
        source_id,
    )
    if row is None:
        return None
    out = _merged(row)
    out["_syncedAt"] = row["synced_at"].isoformat() if row["synced_at"] else None
    return out


async def fetch_by_lookup_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    entity: str,
    json_field: str,
    value: str,
    *,
    size: int = 200,
) -> list[dict[str, Any]]:
    if not value:
        return []
    ns_str = str(namespace_id)
    rows = await conn.fetch(
        "SELECT id, source_json, manual, is_deleted, synced_at FROM sales_read_model "
        "WHERE namespace_id = $1 AND entity = $2 AND is_deleted = false AND source_json->>$3 = $4 "
        "ORDER BY modifiedon DESC NULLS LAST LIMIT $5",
        ns_str,
        entity,
        json_field,
        value,
        size,
    )
    items = []
    for r in rows:
        m = _merged(r)
        m["_syncedAt"] = r["synced_at"].isoformat() if r["synced_at"] else None
        items.append(m)
    return items


async def fetch_by_ids_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    entity: str,
    source_ids: list[str],
) -> list[dict[str, Any]]:
    ids = [i for i in source_ids if i]
    if not ids:
        return []
    ns_str = str(namespace_id)
    rows = await conn.fetch(
        "SELECT id, source_json, manual, is_deleted FROM sales_read_model "
        "WHERE namespace_id = $1 AND entity = $2 AND is_deleted = false AND source_id = ANY($3::text[])",
        ns_str,
        entity,
        ids,
    )
    return [_merged(r) for r in rows]


async def get_targets_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
) -> dict[str, dict[str, float | None]]:
    ns_str = str(namespace_id)
    rows = await conn.fetch(
        "SELECT owner_slug, metric, value FROM sales_targets WHERE namespace_id = $1",
        ns_str,
    )
    out: dict[str, dict[str, float | None]] = {}
    for r in rows:
        out.setdefault(r["owner_slug"], {})[r["metric"]] = (
            float(r["value"]) if r["value"] is not None else None
        )
    return out


async def set_target_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    owner_slug: str,
    metric: str,
    value: float,
) -> None:
    ns_str = str(namespace_id)
    await conn.execute(
        "INSERT INTO sales_targets (namespace_id, owner_slug, metric, value, updated_at) "
        "VALUES ($1, $2, $3, $4, now()) "
        "ON CONFLICT (namespace_id, owner_slug, metric) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        ns_str,
        owner_slug,
        metric,
        value,
    )


async def owner_id_for_slug_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    slug: str,
) -> str | None:
    if not slug:
        return None
    ns_str = str(namespace_id)
    rows = await conn.fetch(
        "SELECT source_json->>'systemuserid' AS guid, source_json->>'fullname' AS navn "
        "FROM sales_read_model WHERE namespace_id = $1 AND entity = 'systemusers' AND is_deleted = false",
        ns_str,
    )
    for r in rows:
        if r["guid"] and r["navn"] and _fold_slug(r["navn"]) == slug:
            return r["guid"]
    return None


# ── Empty Dashboard Fallback ─────────────────────────────────────────────────
def _empty_dashboard() -> dict[str, Any]:
    return {
        "pipeline": {
            "openCount": 0,
            "openValue": 0.0,
            "wonCount": 0,
            "wonValue": 0.0,
            "lostCount": 0,
            "funnel": [],
        },
        "closingSoon": [],
        "myCustomers": {"count": 0, "items": []},
        "openCases": {"count": 0, "items": []},
        "agreementsExpiring": {"available": False, "items": []},
        "warnings": ["owner_unresolved"],
    }


# ── Dashboard aggregate helper (sequential query execution) ──────────────────
async def dashboard_aggregate_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    owner: str | None,
    *,
    team: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    ns_str = str(namespace_id)
    d0 = today or date.today()
    iso_today = d0.isoformat()
    iso_30 = (d0 + timedelta(days=30)).isoformat()

    # Position 1 is namespace_id. Owner condition is added as positional param if mine.
    oa: list[Any]
    if team:
        oa = [ns_str]
        oc = ""
    else:
        oa = [ns_str, owner]
        oc = " AND source_json->>'_ownerid_value' = $2"

    warnings: list[str] = []

    async def _pipeline():
        return await conn.fetchrow(
            f"""SELECT
                  count(*) FILTER (WHERE st='0') AS open_count,
                  sum(val) FILTER (WHERE st='0') AS open_value,
                  count(*) FILTER (WHERE st='1') AS won_count,
                  sum(val) FILTER (WHERE st='1') AS won_value,
                  count(*) FILTER (WHERE st='2') AS lost_count
                FROM (
                  SELECT source_json->>'statecode' AS st, {_VAL_NUM} AS val
                  FROM sales_read_model
                  WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false{oc}
                ) t""",
            *oa,
        )

    async def _funnel():
        return await conn.fetch(
            f"""SELECT {_STAGE} AS stage, count(*) AS cnt, sum({_VAL_NUM}) AS val
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='0'{oc}
                GROUP BY 1 ORDER BY 1""",
            *oa,
        )

    async def _closing():
        di = len(oa)
        return await conn.fetch(
            f"""SELECT id, name, {_VAL_NUM} AS val,
                       source_json->>'estimatedclosedate' AS close_date,
                       {_STAGE} AS stage, {_ACCT_NAME} AS account_name,
                       source_json->>'_customerid_value' AS account_id
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='0'
                  AND source_json->>'estimatedclosedate' >= ${di + 1}
                  AND source_json->>'estimatedclosedate' < ${di + 2}{oc}
                ORDER BY source_json->>'estimatedclosedate' ASC LIMIT 6""",
            *oa,
            iso_today,
            iso_30,
        )

    async def _customers():
        rows = await conn.fetch(
            f"""SELECT id, name, source_json->>'address1_city' AS city, modifiedon
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='accounts' AND is_deleted=false{oc}
                ORDER BY modifiedon DESC NULLS LAST LIMIT 8""",
            *oa,
        )
        total = await conn.fetchval(
            f"SELECT count(*) FROM sales_read_model "
            f"WHERE namespace_id = $1 AND entity='accounts' AND is_deleted=false{oc}",
            *oa,
        )
        return rows, total

    async def _cases():
        cnt = await conn.fetchval(
            f"""SELECT count(*) FROM sales_read_model
                WHERE namespace_id = $1 AND entity='incidents' AND is_deleted=false
                  AND source_json->>'statecode'='0'{oc}""",
            *oa,
        )
        rows = await conn.fetch(
            f"""SELECT id, source_json->>'title' AS title,
                       source_json->>'prioritycode' AS priority,
                       source_json->>'ticketnumber' AS ticket,
                       source_json->>'createdon' AS created_on
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='incidents' AND is_deleted=false
                  AND source_json->>'statecode'='0'{oc}
                ORDER BY modifiedon DESC NULLS LAST LIMIT 5""",
            *oa,
        )
        return cnt, rows

    async def _agreements():
        return await conn.fetchval(
            "SELECT count(*) FROM sales_read_model WHERE namespace_id = $1 AND entity='agreements' AND is_deleted=false",
            ns_str,
        )

    # Execute sequentially to prevent asyncpg concurrent operations error on single transaction connection
    try:
        pipeline_r = await _pipeline()
    except Exception as exc:
        pipeline_r = exc
    try:
        funnel_r = await _funnel()
    except Exception as exc:
        funnel_r = exc
    try:
        closing_r = await _closing()
    except Exception as exc:
        closing_r = exc
    try:
        customers_r = await _customers()
    except Exception as exc:
        customers_r = exc
    try:
        cases_r = await _cases()
    except Exception as exc:
        cases_r = exc
    try:
        agr_r = await _agreements()
    except Exception as exc:
        agr_r = exc

    # If all queries crashed, bubble up the first exception:
    all_results = [pipeline_r, funnel_r, closing_r, customers_r, cases_r, agr_r]
    if all(isinstance(r, Exception) for r in all_results):
        raise all_results[0]

    # ── pipeline ──
    pipeline: dict[str, Any]
    if isinstance(pipeline_r, Exception) or pipeline_r is None:
        pipeline = {
            "openCount": 0,
            "openValue": 0.0,
            "wonCount": 0,
            "wonValue": 0.0,
            "lostCount": 0,
        }
        if isinstance(pipeline_r, Exception):
            warnings.append("pipeline_failed")
    else:
        pipeline = {
            "openCount": pipeline_r["open_count"] or 0,
            "openValue": _num(pipeline_r["open_value"]),
            "wonCount": pipeline_r["won_count"] or 0,
            "wonValue": _num(pipeline_r["won_value"]),
            "lostCount": pipeline_r["lost_count"] or 0,
        }
    if pipeline["openCount"] and not pipeline["openValue"]:
        warnings.append("value_field_missing")

    # ── funnel ──
    if isinstance(funnel_r, Exception):
        warnings.append("funnel_failed")
        pipeline["funnel"] = []
    else:
        pipeline["funnel"] = [
            {"stage": r["stage"], "count": r["cnt"] or 0, "value": _num(r["val"])} for r in funnel_r
        ]

    # ── lukker snart ──
    if isinstance(closing_r, Exception):
        warnings.append("closing_failed")
        closing = []
    else:
        closing = [
            {
                "id": r["id"],
                "name": r["name"],
                "value": _num(r["val"]),
                "closeDate": r["close_date"],
                "stage": r["stage"],
                "accountName": r["account_name"],
                "accountId": r["account_id"],
            }
            for r in closing_r
        ]

    # ── mine kunder ──
    if isinstance(customers_r, Exception):
        warnings.append("customers_failed")
        my_customers = {"count": 0, "items": []}
    else:
        rows, total = customers_r
        my_customers = {
            "count": total or 0,
            "items": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "city": r["city"],
                    "lastActivity": r["modifiedon"].isoformat() if r["modifiedon"] else None,
                }
                for r in rows
            ],
        }

    # ── åpne saker ──
    if isinstance(cases_r, Exception):
        warnings.append("cases_failed")
        open_cases = {"count": 0, "items": []}
    else:
        cnt, rows = cases_r
        open_cases = {
            "count": cnt or 0,
            "items": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "priority": r["priority"],
                    "ticket": r["ticket"],
                    "createdOn": r["created_on"],
                }
                for r in rows
            ],
        }

    # ── avtaler ──
    agr_count = 0 if isinstance(agr_r, Exception) else (agr_r or 0)
    agreements = {"available": bool(agr_count), "items": []}
    if not agreements["available"]:
        warnings.append("agreements_unavailable")

    return {
        "pipeline": pipeline,
        "closingSoon": closing,
        "myCustomers": my_customers,
        "openCases": open_cases,
        "agreementsExpiring": agreements,
        "warnings": warnings,
    }


# ── Manager dashboard helper ─────────────────────────────────────────────────
async def manager_dashboard_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    period: str = "month",
    *,
    today: date | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    ns_str = str(namespace_id)
    d0 = today or date.today()
    key, start, end = _period_range(period, d0, offset)
    iso_today = d0.isoformat()
    warnings: list[str] = []

    async def _pipe():
        return await conn.fetch(
            f"""SELECT source_json->>'_ownerid_value' AS owner,
                       max({_OWNER_FV}) AS name,
                       coalesce(sum({_VAL_NUM}),0) AS project,
                       coalesce(sum({_REC_NUM}),0) AS recurring,
                       count(*) AS open_count
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='0'
                GROUP BY 1""",
            ns_str,
        )

    async def _wl():
        return await conn.fetch(
            f"""SELECT source_json->>'_ownerid_value' AS owner,
                       count(*) FILTER (WHERE source_json->>'statecode'='1') AS won_count,
                       coalesce(sum({_VAL_NUM}) FILTER (WHERE source_json->>'statecode'='1'),0) AS won_value,
                       count(*) FILTER (WHERE source_json->>'statecode'='2') AS lost_count,
                       coalesce(sum({_VAL_NUM}) FILTER (WHERE source_json->>'statecode'='2'),0) AS lost_value
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode' IN ('1','2')
                GROUP BY 1""",
            ns_str,
        )

    async def _appt():
        return await conn.fetch(
            """SELECT source_json->>'_ownerid_value' AS owner,
                       count(*) FILTER (WHERE source_json->>'scheduledstart' >= $2
                                          AND source_json->>'scheduledstart' < $3
                                          AND source_json->>'statecode' <> '2') AS booked,
                       count(*) FILTER (WHERE source_json->>'statecode'='1'
                                          AND source_json->>'scheduledstart' >= $2
                                          AND source_json->>'scheduledstart' < $3) AS completed,
                       count(*) FILTER (WHERE source_json->>'statecode' IN ('0','3')
                                          AND source_json->>'scheduledstart' >= $4) AS forecast
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='appointments' AND is_deleted=false
                GROUP BY 1""",
            ns_str,
            start,
            end,
            iso_today,
        )

    async def _su():
        return await conn.fetch(
            "SELECT source_json->>'systemuserid' AS guid, "
            "       source_json->>'fullname' AS navn, "
            "       source_json->>'isdisabled' AS disabled, "
            "       source_json->>'title' AS title "
            "FROM sales_read_model WHERE namespace_id = $1 AND entity='systemusers' AND is_deleted=false",
            ns_str,
        )

    # Sequential execution:
    try:
        pipe_rows = await _pipe()
    except Exception as exc:
        pipe_rows = exc
    try:
        wl_rows = await _wl()
    except Exception as exc:
        wl_rows = exc
    try:
        appt_rows = await _appt()
    except Exception as exc:
        appt_rows = exc
    try:
        su_rows = await _su()
    except Exception as exc:
        su_rows = exc

    all_results = [pipe_rows, wl_rows, appt_rows, su_rows]
    if all(isinstance(r, Exception) for r in all_results):
        raise all_results[0]

    names = ["pipeline", "wonlost", "appointments", "systemusers"]
    for r, n in zip(all_results, names):
        if isinstance(r, Exception):
            warnings.append(f"{n}_failed")

    su_rows_val = [] if isinstance(su_rows, Exception) else su_rows
    pipe_rows_val = [] if isinstance(pipe_rows, Exception) else pipe_rows
    wl_rows_val = [] if isinstance(wl_rows, Exception) else wl_rows
    appt_rows_val = [] if isinstance(appt_rows, Exception) else appt_rows

    guid_name = {r["guid"]: r["navn"] for r in su_rows_val if r["guid"]}
    sales_owners = {
        r["guid"]
        for r in su_rows_val
        if r["guid"] and str(r["disabled"]).lower() != "true" and _is_sales_title(r["title"])
    }
    targets = await get_targets_helper(conn, namespace_id)

    by: dict[str, dict[str, Any]] = {}

    def ensure(owner_id: str, name_val: str | None = None) -> dict[str, Any]:
        if owner_id not in by:
            by[owner_id] = {
                "owner": owner_id,
                "name": name_val or guid_name.get(owner_id) or "Ukjent selger",
                "openProjectValue": 0.0,
                "openRecurringMonthly": 0.0,
                "openCount": 0,
                "wonValue": 0.0,
                "wonCount": 0,
                "lostCount": 0,
                "lostValue": 0.0,
                "meetingsBooked": 0,
                "meetingsCompleted": 0,
                "meetingsForecast": 0,
                "riskValue": 0.0,
                "riskCount": 0,
            }
        return by[owner_id]

    for r in pipe_rows_val:
        if not r["owner"]:
            continue
        a = ensure(r["owner"], r["name"])
        a["openProjectValue"] = _num(r["project"])
        a["openRecurringMonthly"] = _num(r["recurring"])
        a["openCount"] = r["open_count"] or 0
    for r in wl_rows_val:
        if not r["owner"]:
            continue
        a = ensure(r["owner"])
        a["wonValue"] = _num(r["won_value"])
        a["wonCount"] = r["won_count"] or 0
        a["lostCount"] = r["lost_count"] or 0
        a["lostValue"] = _num(r["lost_value"])
    for r in appt_rows_val:
        if not r["owner"]:
            continue
        a = ensure(r["owner"])
        a["meetingsBooked"] = r["booked"] or 0
        a["meetingsCompleted"] = r["completed"] or 0
        a["meetingsForecast"] = r["forecast"] or 0

    for a in by.values():
        slug = _fold_slug(a["name"])
        a["slug"] = slug
        t = targets.get(slug, {})
        a["targets"] = {"meetings": t.get("meetings_monthly"), "won": t.get("won_monthly")}

    def _has_activity(a: dict[str, Any]) -> bool:
        return bool(
            a["openProjectValue"]
            or a["openRecurringMonthly"]
            or a["wonCount"]
            or a["lostCount"]
            or a["meetingsBooked"]
            or a["meetingsForecast"]
            or a["meetingsCompleted"]
            or a["targets"]["meetings"]
            or a["targets"]["won"]
        )

    rows = sorted(
        (a for a in by.values() if a["owner"] in sales_owners and _has_activity(a)),
        key=lambda a: a["openProjectValue"] + a["wonValue"],
        reverse=True,
    )
    keys = [
        "openProjectValue",
        "openRecurringMonthly",
        "openCount",
        "wonValue",
        "wonCount",
        "lostCount",
        "lostValue",
        "meetingsBooked",
        "meetingsCompleted",
        "meetingsForecast",
    ]
    team_summary = {k: round(sum(a[k] for a in rows), 2) for k in keys}

    # risk block:
    risk: dict[str, Any] = {
        "count": 0,
        "value": 0.0,
        "pctOfPipeline": 0,
        "overdue": {"count": 0, "value": 0.0},
        "stale": {"count": 0, "value": 0.0},
        "byAm": [],
        "ageBuckets": [],
        "items": [],
    }
    if sales_owners:
        owner_list = list(sales_owners)
        cutoff = d0 - timedelta(days=60)
        m30 = (d0 - timedelta(days=30)).isoformat()
        m90 = (d0 - timedelta(days=90)).isoformat()
        m365 = (d0 - timedelta(days=365)).isoformat()
        where_risk = (
            "namespace_id = $1 AND entity='opportunities' AND is_deleted=false AND source_json->>'statecode'='0' "
            "AND source_json->>'_ownerid_value' = ANY($2::text[]) "
            "AND (source_json->>'estimatedclosedate' < $3 OR modifiedon < $4)"
        )
        _AGE = {"m1": "< 1 mnd", "m3": "1–3 mnd", "y1": "3–12 mnd", "old": "> 1 år"}
        try:
            # Sequential gathers inside try/except block:
            rtot = await conn.fetchrow(
                f"""SELECT count(*) AS n, coalesce(sum(val),0) AS v,
                           count(*) FILTER (WHERE od) AS od_n, coalesce(sum(val) FILTER (WHERE od),0) AS od_v,
                           count(*) FILTER (WHERE st) AS st_n, coalesce(sum(val) FILTER (WHERE st),0) AS st_v
                    FROM (SELECT {_VAL_NUM} AS val,
                                 (source_json->>'estimatedclosedate' < $3) AS od,
                                 (modifiedon < $4) AS st
                          FROM sales_read_model WHERE {where_risk}) t""",
                ns_str,
                owner_list,
                iso_today,
                cutoff,
            )
            rage = await conn.fetch(
                f"""SELECT CASE
                             WHEN source_json->>'estimatedclosedate' >= $4 THEN 'm1'
                             WHEN source_json->>'estimatedclosedate' >= $5 THEN 'm3'
                             WHEN source_json->>'estimatedclosedate' >= $6 THEN 'y1'
                             ELSE 'old' END AS bucket,
                           count(*) AS n, coalesce(sum({_VAL_NUM}),0) AS v
                    FROM sales_read_model
                    WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false AND source_json->>'statecode'='0'
                      AND source_json->>'_ownerid_value' = ANY($2::text[])
                      AND source_json->>'estimatedclosedate' < $3
                    GROUP BY 1""",
                ns_str,
                owner_list,
                iso_today,
                m30,
                m90,
                m365,
            )
            rbyam = await conn.fetch(
                f"""SELECT source_json->>'_ownerid_value' AS owner, max({_OWNER_FV}) AS name,
                           count(*) AS n, coalesce(sum({_VAL_NUM}),0) AS v
                    FROM sales_read_model WHERE {where_risk}
                    GROUP BY 1 ORDER BY v DESC NULLS LAST""",
                ns_str,
                owner_list,
                iso_today,
                cutoff,
            )
            ritems = await conn.fetch(
                f"""SELECT source_json->>'opportunityid' AS oppid, name, {_VAL_NUM} AS val,
                           source_json->>'estimatedclosedate' AS close_date, modifiedon,
                           {_OWNER_FV} AS owner_name,
                           source_json->>'_customerid_value' AS account_id, {_ACCT_NAME} AS account_name
                    FROM sales_read_model WHERE {where_risk}
                    ORDER BY {_VAL_NUM} DESC NULLS LAST, modifiedon ASC NULLS LAST
                    LIMIT 12""",
                ns_str,
                owner_list,
                iso_today,
                cutoff,
            )
            tot_v = _num(rtot["v"])
            age = {r["bucket"]: r for r in rage}
            for r in rbyam:
                risk_am = by.get(r["owner"])
                if risk_am is not None:
                    risk_am["riskValue"] = _num(r["v"])
                    risk_am["riskCount"] = r["n"] or 0
            risk = {
                "count": rtot["n"] or 0,
                "value": tot_v,
                "pctOfPipeline": (
                    round(tot_v / team_summary["openProjectValue"] * 100)
                    if team_summary.get("openProjectValue")
                    else 0
                ),
                "overdue": {"count": rtot["od_n"] or 0, "value": _num(rtot["od_v"])},
                "stale": {"count": rtot["st_n"] or 0, "value": _num(rtot["st_v"])},
                "byAm": [
                    {
                        "owner": r["owner"],
                        "name": r["name"] or "Ukjent",
                        "slug": _fold_slug(r["name"] or ""),
                        "count": r["n"] or 0,
                        "value": _num(r["v"]),
                    }
                    for r in rbyam[:6]
                ],
                "ageBuckets": [
                    {
                        "key": k,
                        "label": _AGE[k],
                        "count": (age[k]["n"] if k in age else 0),
                        "value": (_num(age[k]["v"]) if k in age else 0.0),
                    }
                    for k in ("m1", "m3", "y1", "old")
                ],
                "items": [
                    {
                        "id": r["oppid"],
                        "name": r["name"],
                        "value": _num(r["val"]),
                        "closeDate": r["close_date"],
                        "modifiedon": r["modifiedon"].isoformat() if r["modifiedon"] else None,
                        "ownerName": r["owner_name"],
                        "accountId": r["account_id"],
                        "accountName": r["account_name"],
                        "overdue": bool(r["close_date"] and r["close_date"] < iso_today),
                    }
                    for r in ritems
                ],
            }
        except Exception:
            warnings.append("risk_failed")

    funnel_list: list[dict[str, Any]] = []
    won_lost: dict[str, list] = {"won": [], "lost": []}
    if sales_owners:
        owner_arr = list(sales_owners)
        try:
            frows = await conn.fetch(
                f"""SELECT stage, oppid, name, val, account_id, account_name, owner_name,
                           stage_count, stage_value
                    FROM (
                      SELECT {_STAGE} AS stage,
                             source_json->>'opportunityid' AS oppid, name, {_VAL_NUM} AS val,
                             source_json->>'_customerid_value' AS account_id,
                             {_ACCT_NAME} AS account_name, {_OWNER_FV} AS owner_name,
                             count(*) OVER (PARTITION BY {_STAGE}) AS stage_count,
                             coalesce(sum({_VAL_NUM}) OVER (PARTITION BY {_STAGE}),0) AS stage_value,
                             row_number() OVER (PARTITION BY {_STAGE} ORDER BY {_VAL_NUM} DESC NULLS LAST) AS rn
                      FROM sales_read_model
                      WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                        AND source_json->>'statecode'='0'
                        AND source_json->>'_ownerid_value' = ANY($2::text[])
                    ) t WHERE rn <= 6
                    ORDER BY nullif(substring(stage from '^[0-9]+'),'')::int NULLS LAST, stage ASC, rn ASC""",
                ns_str,
                owner_arr,
            )
            wlrows = await conn.fetch(
                f"""SELECT st, oppid, name, val, account_id, account_name, owner_name, close_date
                    FROM (
                      SELECT source_json->>'statecode' AS st,
                             source_json->>'opportunityid' AS oppid, name, {_VAL_NUM} AS val,
                             source_json->>'_customerid_value' AS account_id,
                             {_ACCT_NAME} AS account_name, {_OWNER_FV} AS owner_name,
                             source_json->>'actualclosedate' AS close_date,
                             row_number() OVER (PARTITION BY source_json->>'statecode'
                                                ORDER BY {_VAL_NUM} DESC NULLS LAST) AS rn
                      FROM sales_read_model
                      WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                        AND source_json->>'statecode' IN ('1','2')
                        AND source_json->>'_ownerid_value' = ANY($2::text[])
                    ) t WHERE rn <= 10 ORDER BY st ASC, val DESC NULLS LAST""",
                ns_str,
                owner_arr,
            )
            fmap: dict[str, dict[str, Any]] = {}
            for r in frows:
                f = fmap.get(r["stage"])
                if f is None:
                    f = fmap[r["stage"]] = {
                        "stage": r["stage"],
                        "count": r["stage_count"] or 0,
                        "value": _num(r["stage_value"]),
                        "deals": [],
                    }
                f["deals"].append(
                    {
                        "id": r["oppid"],
                        "name": r["name"],
                        "value": _num(r["val"]),
                        "accountId": r["account_id"],
                        "accountName": r["account_name"],
                        "ownerName": r["owner_name"],
                    }
                )
            funnel_list = list(fmap.values())
            for r in wlrows:
                deal = {
                    "id": r["oppid"],
                    "name": r["name"],
                    "value": _num(r["val"]),
                    "accountId": r["account_id"],
                    "accountName": r["account_name"],
                    "ownerName": r["owner_name"],
                    "closeDate": r["close_date"],
                }
                won_lost["won" if r["st"] == "1" else "lost"].append(deal)
        except Exception:
            warnings.append("funnel_failed")

    blocked: dict[str, Any] = {"count": 0, "value": 0.0}
    if sales_owners:
        owner_arr3 = list(sales_owners)
        try:
            inc_rows = await conn.fetch(
                """SELECT DISTINCT lower(source_json->>'_bravo_opportunityid_value') AS oid
                    FROM sales_read_model
                    WHERE namespace_id = $1 AND entity='incidents' AND is_deleted=false
                      AND source_json->>'statecode'='0'
                      AND nullif(source_json->>'_bravo_opportunityid_value','') IS NOT NULL""",
                ns_str,
            )
            blocker_oids = [r["oid"] for r in inc_rows if r["oid"]]
            brows = await conn.fetch(
                f"""SELECT source_json->>'opportunityid' AS oppid, {_VAL_NUM} AS val
                    FROM sales_read_model
                    WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                      AND source_json->>'statecode'='0'
                      AND source_json->>'_ownerid_value' = ANY($2::text[])
                      AND lower(source_json->>'opportunityid') = ANY($3::text[])""",
                ns_str,
                owner_arr3,
                blocker_oids,
            )
            blocked_ids = {r["oppid"] for r in brows}
            blocked = {
                "count": len(blocked_ids),
                "value": round(sum(_num(r["val"]) for r in brows), 2),
            }
            for f in funnel_list:
                for d in f["deals"]:
                    d["blocked"] = d["id"] in blocked_ids
            for it in risk.get("items", []):
                it["blocked"] = it["id"] in blocked_ids
        except Exception:
            warnings.append("blocked_failed")

    return {
        "period": {"key": key, "start": start, "end": end, "type": period, "offset": offset},
        "byAm": rows,
        "team": team_summary,
        "funnel": funnel_list,
        "wonLost": won_lost,
        "blocked": blocked,
        "risk": risk,
        "warnings": warnings,
    }


# ── Stats dashboard helper ───────────────────────────────────────────────────
async def stats_dashboard_helper(
    conn: asyncpg.Connection,
    namespace_id: str | UUID,
    period: str = "month",
    *,
    today: date | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    ns_str = str(namespace_id)
    d0 = today or date.today()
    key, start, end = _period_range(period, d0, offset)
    warnings: list[str] = []

    try:
        su_rows = await conn.fetch(
            "SELECT source_json->>'systemuserid' AS guid, source_json->>'title' AS title "
            "FROM sales_read_model WHERE namespace_id = $1 AND entity='systemusers' AND is_deleted=false",
            ns_str,
        )
        acc_rows = await conn.fetch(
            "SELECT source_json->>'accountid' AS aid, source_json->>'bravo_industry' AS ind, name "
            "FROM sales_read_model WHERE namespace_id = $1 AND entity='accounts' AND is_deleted=false",
            ns_str,
        )
    except Exception:
        su_rows, acc_rows = [], []
        warnings.append("lookups_failed")

    owner_title = {r["guid"]: r["title"] for r in su_rows if r["guid"]}
    acc_map = {r["aid"]: (r["ind"], r["name"]) for r in acc_rows if r["aid"]}

    def _bucket() -> dict[str, Any]:
        return {"openCount": 0, "openValue": 0.0, "wonCount": 0, "wonValue": 0.0, "recurring": 0.0}

    by_itav = {k: _bucket() for k in ("it", "av", "begge", "ukjent")}
    by_seg = {k: _bucket() for k in ("workplace", "education", "hospitality", "ukjent")}
    itav_known = seg_known = total = 0

    try:
        rows = await conn.fetch(
            f"""SELECT source_json->>'statecode' AS st, {_VAL_NUM} AS val, {_REC_NUM} AS rec,
                       source_json->>'_ownerid_value' AS owner,
                       source_json->>'_customerid_value' AS cust, {_ACCT_NAME} AS cust_name,
                       source_json->>'name' AS nm,
                       source_json->>'bravo_customerneeds' AS needs,
                       source_json->>'bravo_jobdescription' AS jobdesc,
                       source_json->>'description' AS descr,
                       source_json->>'bravo_subject@OData.Community.Display.V1.FormattedValue' AS subj
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode' IN ('0','1')""",
            ns_str,
        )
    except Exception:
        rows = []
        warnings.append("opps_failed")

    for r in rows:
        sj = {
            "name": r["nm"],
            "bravo_customerneeds": r["needs"],
            "bravo_jobdescription": r["jobdesc"],
            "description": r["descr"],
            "bravo_subject@OData.Community.Display.V1.FormattedValue": r["subj"],
        }
        itav = classify_it_av(sj, owner_title.get(r["owner"]))
        ind, aname = acc_map.get(r["cust"], (None, None))
        seg = segment_of(ind, aname or r["cust_name"])
        val = _num(r["val"])
        rec = _num(r["rec"])
        total += 1
        if itav != "ukjent":
            itav_known += 1
        if seg != "ukjent":
            seg_known += 1
        for bucket, kk in ((by_itav, itav), (by_seg, seg)):
            b = bucket[kk]
            if r["st"] == "0":
                b["openCount"] += 1
                b["openValue"] += val
                b["recurring"] += rec
            elif r["st"] == "1":
                b["wonCount"] += 1
                b["wonValue"] += val

    for bucket in (by_itav, by_seg):
        for b in bucket.values():
            b["openValue"] = round(b["openValue"], 2)
            b["wonValue"] = round(b["wonValue"], 2)
            b["recurring"] = round(b["recurring"], 2)

    trend: dict[str, list] = {"createdPerMonth": [], "wonByYear": []}
    try:
        t12 = _first_of_next(date(d0.year, d0.month, 1), -11).isoformat()
        cre = await conn.fetch(
            """SELECT substring(source_json->>'createdon', 1, 7) AS ym, count(*) AS n
                FROM sales_read_model WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'createdon' >= $2 GROUP BY 1 ORDER BY 1""",
            ns_str,
            t12,
        )
        won = await conn.fetch(
            f"""SELECT CASE
                       WHEN source_json->>'actualclosedate' >= '2000'
                         THEN substring(source_json->>'actualclosedate', 1, 4)
                       WHEN source_json->>'estimatedclosedate' >= '2000'
                         THEN substring(source_json->>'estimatedclosedate', 1, 4)
                       ELSE 'udatert' END AS yr,
                       count(*) AS n, coalesce(sum({_VAL_NUM}), 0) AS v
                FROM sales_read_model WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='1'
                GROUP BY 1 ORDER BY 1""",
            ns_str,
        )
        trend["createdPerMonth"] = [{"ym": r["ym"], "count": r["n"] or 0} for r in cre if r["ym"]]
        trend["wonByYear"] = [
            {"yr": r["yr"], "count": r["n"] or 0, "value": _num(r["v"])} for r in won if r["yr"]
        ]
    except Exception:
        warnings.append("trend_failed")

    return {
        "period": {"key": key, "start": start, "end": end, "type": period, "offset": offset},
        "byItAv": by_itav,
        "bySegment": by_seg,
        "trend": trend,
        "coverage": {
            "itav": round(100 * itav_known / total) if total else 0,
            "segment": round(100 * seg_known / total) if total else 0,
            "n": total,
        },
        "warnings": warnings,
    }


# ── Public Facades ───────────────────────────────────────────────────────────
async def do_list_customers(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    q = params.get("q", "")
    size = int(params.get("size", 100))
    page = int(params.get("page", 0))
    include_deleted = bool(
        params.get("includeDeleted", False) or params.get("include_deleted", False)
    )
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        return await fetch_records_helper(
            conn,
            namespace_id,
            "accounts",
            q=q,
            size=size,
            page=page,
            include_deleted=include_deleted,
        )


async def do_customer_profile(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    accountid = params.get("accountid") or params.get("account_id")
    if not accountid:
        raise ValueError("accountid is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        company = await fetch_one_helper(conn, namespace_id, "accounts", accountid)
        if company is None:
            return {"error": "unknown_company", "detail": f"Ukjent selskap: {accountid!r}"}

        contacts = await fetch_by_lookup_helper(
            conn, namespace_id, "contacts", "_parentcustomerid_value", accountid
        )
        opportunities = await fetch_by_lookup_helper(
            conn, namespace_id, "opportunities", "_customerid_value", accountid
        )
        cases = await fetch_by_lookup_helper(
            conn, namespace_id, "incidents", "_customerid_value", accountid
        )

        assets = await fetch_by_lookup_helper(
            conn, namespace_id, "customerassets", "_msdyn_account_value", accountid
        )
        fl_ids: list[str] = []
        seen: set[str] = set()
        for a in assets:
            fid = a.get("_msdyn_functionallocation_value")
            if fid and fid not in seen:
                seen.add(fid)
                fl_ids.append(fid)

        locations = []
        if fl_ids:
            fls = await fetch_by_ids_helper(conn, namespace_id, "functionallocations", fl_ids)
            for fl in fls:
                parts = [fl.get("msdyn_address1"), fl.get("msdyn_city")]
                address = ", ".join(p for p in parts if p)
                parent = (
                    fl.get("_FL_PARENT_FMT")
                    or fl.get(
                        "_msdyn_parentfunctionallocation_value@OData.Community.Display.V1.FormattedValue"
                    )
                    or ""
                )
                locations.append(
                    {
                        "id": fl.get("msdyn_functionallocationid") or fl.get("_id"),
                        "name": fl.get("msdyn_name") or "",
                        "address": address,
                        "parent": parent,
                    }
                )

        return {
            "company": company,
            "contacts": contacts,
            "opportunities": opportunities,
            "cases": cases,
            "locations": locations,
        }


async def do_sales_overview(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            f"""SELECT {_STAGE} AS stage, count(*) AS count, coalesce(sum({_VAL_NUM}), 0) AS value
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='0'
                GROUP BY 1 ORDER BY 1""",
            str(namespace_id),
        )
        return {
            "stages": [
                {
                    "stage": r["stage"],
                    "count": r["count"],
                    "value": float(r["value"]),
                }
                for r in rows
            ]
        }


async def do_seller_detail(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")

    user = params.get("user") or params.get("owner_slug")
    if not user:
        raise ValueError("user or owner_slug is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        owner = await owner_id_for_slug_helper(conn, namespace_id, user)
        if not owner:
            return {"user": user, "owner": None, "pipeline": [], "wonValue": 0.0, "wonCount": 0}

        rows = await conn.fetch(
            f"""SELECT id, name, {_VAL_NUM} AS val, source_json->>'estimatedclosedate' AS close_date, {_STAGE} AS stage
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='0' AND source_json->>'_ownerid_value' = $2
                ORDER BY modifiedon DESC NULLS LAST""",
            str(namespace_id),
            owner,
        )
        pipeline = [
            {
                "id": r["id"],
                "name": r["name"],
                "value": float(r["val"]) if r["val"] is not None else 0.0,
                "closeDate": r["close_date"],
                "stage": r["stage"],
            }
            for r in rows
        ]

        won = await conn.fetchrow(
            f"""SELECT count(*) AS count, coalesce(sum({_VAL_NUM}), 0) AS value
                FROM sales_read_model
                WHERE namespace_id = $1 AND entity='opportunities' AND is_deleted=false
                  AND source_json->>'statecode'='1' AND source_json->>'_ownerid_value' = $2""",
            str(namespace_id),
            owner,
        )

        return {
            "user": user,
            "owner": owner,
            "pipeline": pipeline,
            "wonCount": won["count"],
            "wonValue": float(won["value"]),
        }


async def do_sales_dashboard(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")

    slug = params.get("user", "").strip()
    team = slug == "admin"
    owner = None

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        if not team and slug:
            owner = await owner_id_for_slug_helper(conn, namespace_id, slug)
            if not owner:
                data = _empty_dashboard()
                data.update({"scope": "mine", "user": slug, "owner": None})
                return data

        today_val = params.get("today")
        if isinstance(today_val, str):
            today_date = date.fromisoformat(today_val)
        elif isinstance(today_val, date):
            today_date = today_val
        else:
            today_date = date.today()

        data = await dashboard_aggregate_helper(
            conn, namespace_id, owner, team=team, today=today_date
        )
        data["scope"] = "team" if team else "mine"
        data["user"] = slug
        data["owner"] = owner
        data["generatedAt"] = datetime.now().isoformat()
        return data


async def do_sales_stats(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")

    period = params.get("period", "month").strip().lower()
    if period not in ("month", "quarter", "year", "ytd"):
        period = "month"
    offset = int(params.get("offset", 0))

    today_val = params.get("today")
    if isinstance(today_val, str):
        today_date = date.fromisoformat(today_val)
    elif isinstance(today_val, date):
        today_date = today_val
    else:
        today_date = date.today()

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        data = await stats_dashboard_helper(
            conn, namespace_id, period, today=today_date, offset=offset
        )
        data["generatedAt"] = datetime.now().isoformat()
        return data


async def do_sales_manager(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")

    period = params.get("period", "month").strip().lower()
    if period not in ("month", "quarter", "year", "ytd"):
        period = "month"
    offset = int(params.get("offset", 0))

    today_val = params.get("today")
    if isinstance(today_val, str):
        today_date = date.fromisoformat(today_val)
    elif isinstance(today_val, date):
        today_date = today_val
    else:
        today_date = date.today()

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        data = await manager_dashboard_helper(
            conn, namespace_id, period, today=today_date, offset=offset
        )
        data["generatedAt"] = datetime.now().isoformat()
        return data


async def do_list_agreements(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    q = params.get("q", "")
    size = int(params.get("size", 100))
    page = int(params.get("page", 0))
    include_deleted = bool(
        params.get("includeDeleted", False) or params.get("include_deleted", False)
    )
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        return await fetch_records_helper(
            conn,
            namespace_id,
            "agreements",
            q=q,
            size=size,
            page=page,
            include_deleted=include_deleted,
        )


async def do_agreement_detail(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    agreementid = params.get("agreementid") or params.get("agreement_id")
    if not agreementid:
        raise ValueError("agreementid is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        res = await fetch_one_helper(conn, namespace_id, "agreements", agreementid)
        if res is None:
            return {"error": "unknown_agreement", "detail": f"Ukjent avtale: {agreementid!r}"}
        return res


async def do_quote_detail(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    quoteid = params.get("quoteid") or params.get("quote_id")
    if not quoteid:
        raise ValueError("quoteid is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        res = await fetch_one_helper(conn, namespace_id, "quotes", quoteid)
        if res is None:
            return {"error": "unknown_quote", "detail": f"Ukjent tilbud: {quoteid!r}"}
        return res


async def do_get_targets(
    engine: NCEEngine, params: dict[str, Any]
) -> dict[str, dict[str, float | None]]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        return await get_targets_helper(conn, namespace_id)


async def do_set_target(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    owner_slug = params.get("owner_slug") or params.get("owner")
    metric = params.get("metric")
    value = float(params.get("value", 0.0))
    if not owner_slug or metric not in ("meetings_monthly", "won_monthly"):
        raise ValueError("owner_slug and valid metric (meetings_monthly/won_monthly) are required")
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        await set_target_helper(conn, namespace_id, owner_slug, metric, value)
        return {"ok": True}
