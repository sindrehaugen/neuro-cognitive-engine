"""
nce/pricing/fx.py
======================================
FX rate feed for the shared pricing service (MLV16 charter, Lane F, Wave
F-15 — the first half of the geodata/platform FEED module's "weather and
FX" pairing, F44).

Same shape decision as the host's own ``fx.py``: a live per-request call
to Norges Bank does not scale across every pricing read, so a fetched rate
is cached in-process for a TTL and degrades to the last known-good rate on
any failure — never a guessed exchange rate. Read for shape only (Q-25);
this module's schema, functions and tests are its own, not a port of the
host's.

What this wave does NOT do
------------------------------
The host's own module freezes a rate into an offer line at quote time (its
own frontend's job); this module only answers "what is today's rate", the
same boundary ``resolve_price`` already draws for cost tiers — freezing a
rate into a specific priced line is a caller decision, not this feed's.

Q-25 (AGPL / proprietary boundary)
--------------------------------------
Norges Bank's EXR dataset is public, no-auth, no-key data; nothing about
it is the host's proprietary work. What was read from the host for shape
only was the ARCHITECTURAL DECISION (in-process TTL cache, degrade to
last-good on failure, persist last-good so a restart isn't a cold start)
— re-derived and re-implemented here with an independent schema, parser
and persistence table, never the host's code.
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

import asyncpg  # type: ignore[import-untyped]
import httpx

from nce.config import cfg
from nce.db_utils import unmanaged_pg_connection

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.pricing.fx")

# The currencies this feed tracks against NOK. Adding one means widening
# this tuple AND the allow-list literal below in the same change — the
# gate's whole job is to make that a visible diff, not a silent one.
CURRENCIES: tuple[str, ...] = ("EUR", "USD")
_SOURCE = "Norges Bank"
_BASE_PATH = f"/data/EXR/B.{'+'.join(CURRENCIES)}.NOK.SP"
_BASE_URL = f"https://data.norges-bank.no{_BASE_PATH}"
_QUERY = {"lastNObservations": "1", "format": "csv"}
# Norges Bank rejects requests with no identifying User-Agent.
_HEADERS = {"User-Agent": "NCE/1.0 (geodata FX feed)"}

# The allow-list literal (charter §0 rule 3): one GET, one exact path. The
# currency set is a module constant, not a per-call argument, so there is
# no template slot to gate — widening CURRENCIES is the only way this
# feed's surface grows, and it shows up as a diff to this literal too.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", _BASE_PATH)})

# In-memory cache, one process-wide entry (unlike weather, a rate is not
# keyed by location). `fetched_at` only advances on a fresh hit, so a
# degraded response keeps retrying the source on every subsequent call.
_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


class FxAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this feed's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"fx feed refused {method} {path}: not in its read allow-list")
        self.method = method
        self.path = path


async def _gated_request(client: httpx.AsyncClient, method: str) -> httpx.Response:
    if (method, _BASE_PATH) not in _ALLOWED_READS:
        raise FxAllowListRefusal(method, _BASE_PATH)
    return await client.request(method, _BASE_URL, params=_QUERY)


def parse_csv(text: str) -> dict[str, Any]:
    """Norges Bank's semicolon-delimited EXR CSV -> ``{"rates": {cur: float},
    "date": "YYYY-MM-DD"}``, or ``{}`` on empty/unrecognisable input.

    Column order in SDMX-CSV is not guaranteed, so this reads the header
    row to find ``BASE_CUR``/``OBS_VALUE``/``TIME_PERIOD`` by name rather
    than by a fixed position.
    """
    rows = [line for line in (text or "").splitlines() if line.strip()]
    if len(rows) < 2:
        return {}
    columns = [name.strip() for name in rows[0].split(";")]
    positions = {name: i for i, name in enumerate(columns)}
    cur_pos, value_pos, date_pos = (
        positions.get("BASE_CUR"),
        positions.get("OBS_VALUE"),
        positions.get("TIME_PERIOD"),
    )
    if cur_pos is None or value_pos is None:
        return {}

    rates: dict[str, float] = {}
    rate_date: str | None = None
    for row in rows[1:]:
        cells = row.split(";")
        if len(cells) <= max(cur_pos, value_pos):
            continue
        currency = cells[cur_pos].strip().upper()
        try:
            value = float(cells[value_pos].strip().replace(",", "."))
        except ValueError:
            continue
        if not currency or value <= 0:
            continue
        rates[currency] = value
        if date_pos is not None and date_pos < len(cells) and cells[date_pos].strip():
            rate_date = cells[date_pos].strip()
    return {"rates": rates, "date": rate_date} if rates else {}


async def fetch_rates(
    *, client: httpx.AsyncClient | None = None, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Fetch today's rates from Norges Bank. Never raises — any failure
    (network, timeout, non-200, unparseable body) degrades to ``{}``, the
    same contract as ``brreg_feed``'s registry lookup."""
    owns = client is None
    cl = client or httpx.AsyncClient(
        timeout=cfg.NCE_FX_TIMEOUT_SECONDS,
        headers=_HEADERS,
        follow_redirects=True,
        transport=transport,
    )
    try:
        resp = await _gated_request(cl, "GET")
        if resp.status_code != 200:
            log.warning("fx: Norges Bank returned HTTP %s", resp.status_code)
            return {}
        return parse_csv(resp.text)
    except FxAllowListRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into the caller
        log.warning("fx: fetch failed: %s", str(exc)[:200])
        return {}
    finally:
        if owns:
            await cl.aclose()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _contract(
    rates: dict[str, float], date: str | None, fetched_at: str, *, stale: bool
) -> dict[str, Any]:
    return {
        "base": "NOK",
        "rates": rates or {},
        "date": date,
        "fetchedAt": fetched_at,
        "stale": bool(stale),
        "source": _SOURCE,
    }


async def _persist_last_good(
    engine: NCEEngine, rates: dict[str, float], rate_date: str | None
) -> bool:
    """Best-effort upsert of the fresh rates into ``pricing_fx_rates`` so a
    process restart still has a last-known-good rate before the source
    answers again. A persistence failure never blocks returning the fresh
    rate this call already fetched, but it is never swallowed silently
    either -- only a genuine database-connectivity error is caught here
    (logged at ``error``, since a failed write on a money-adjacent path
    deserves visibility, not a ``warning``); anything else (a programming
    bug in this function) propagates. Returns whether the write landed,
    for callers/tests that want to distinguish the two.
    """
    if not rates or not rate_date:
        return False
    try:
        async with unmanaged_pg_connection(engine.pg_pool, site="pricing.fx.persist") as conn:
            await conn.executemany(
                """
                INSERT INTO pricing_fx_rates (currency, rate, rate_date)
                VALUES ($1, $2, $3::date)
                ON CONFLICT (currency) DO UPDATE SET
                    rate = EXCLUDED.rate,
                    rate_date = EXCLUDED.rate_date,
                    fetched_at = now()
                """,
                [(currency, value, rate_date) for currency, value in rates.items()],
            )
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        log.error("fx: could not persist last-good rates: %s", str(exc)[:200])
        return False
    return True


async def _load_last_good(engine: NCEEngine) -> dict[str, Any] | None:
    """Read the persisted last-good rates, or ``None`` if the table is
    empty (never seeded, or wiped) or unreachable.

    Only a genuine database-connectivity error is caught (logged at
    ``error``); anything else propagates rather than being swallowed.
    """
    try:
        async with unmanaged_pg_connection(engine.pg_pool, site="pricing.fx.load") as conn:
            rows = await conn.fetch(
                "SELECT currency, rate, rate_date, fetched_at FROM pricing_fx_rates"
            )
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        log.error("fx: could not load last-good rates: %s", str(exc)[:200])
        return None
    if not rows:
        return None
    rates = {row["currency"]: float(row["rate"]) for row in rows}
    rate_date = max(row["rate_date"] for row in rows).isoformat()
    fetched_at = max(row["fetched_at"] for row in rows).isoformat()
    return _contract(rates, rate_date, fetched_at, stale=True)


async def get_rates(
    engine: NCEEngine, *, force: bool = False, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """FX rate contract: ``{"base": "NOK", "rates": {"EUR": 11.14, ...},
    "date": "2026-09-18", "fetchedAt": <iso>, "stale": bool, "source":
    "Norges Bank"}``.

    In-process TTL cache first; on a miss or ``force=True``, fetches fresh
    and persists the last-good rates to ``pricing_fx_rates``. On fetch
    failure, degrades to the in-process cache, then the persisted
    last-good row, then an empty ``stale=True`` contract — never a
    fabricated rate.
    """
    cached = _cache.get("data")
    if not force and cached and (time.monotonic() - _cache["fetched_at"] < cfg.NCE_FX_TTL_SECONDS):
        return cached

    fresh = await fetch_rates(transport=transport)
    if fresh.get("rates"):
        data = _contract(fresh["rates"], fresh.get("date"), _now_iso(), stale=False)
        _cache["data"] = data
        _cache["fetched_at"] = time.monotonic()
        await _persist_last_good(engine, fresh["rates"], fresh.get("date"))
        return data

    if cached:
        return {**cached, "stale": True}
    last_good = await _load_last_good(engine)
    if last_good is not None:
        _cache["data"] = last_good
        return last_good
    return _contract({}, None, _now_iso(), stale=True)


def convert_to_nok(amount: float, currency: str, rates: dict[str, float]) -> float | None:
    """``amount`` in ``currency`` -> NOK, using an already-fetched
    ``rates`` mapping (from ``get_rates()["rates"]``). ``None`` when the
    currency has no rate rather than a silently wrong 1:1 conversion.
    """
    rate = rates.get(currency.upper())
    if rate is None:
        return None
    return amount * rate


async def do_get_fx_rates(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """MCP-callable entry point: today's FX rates against NOK.

    Parameters
    ----------
    params:
        ``{"force": bool}`` (optional, default ``False``) — bypass the
        in-process TTL cache and fetch fresh.
    """
    force = bool(params.get("force", False))
    data = await get_rates(engine, force=force)
    return {"ok": True, **data}
