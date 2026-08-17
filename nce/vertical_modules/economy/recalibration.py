"""
nce/vertical_modules/economy/recalibration.py
==============================================
Ledger-backed per-supplier recalibration (Module 8 Wave 11).

``do_record_match_decision`` appends each invoice-match outcome to
``v3_cognitive_ledger`` (append-only INSERT; never UPDATE/DELETE — the Batch 51
precedent). ``do_recalibrate_supplier`` reads the rolling ``window_n`` (N=100 in
production) decision window for one supplier, derives a precision-earned
green/yellow threshold movement, and — if and only if the candidate survives
validation — persists it into ``economy-match-thresholds.json``'s
``supplier_overrides`` (the file ``matching.load_economy_thresholds()`` /
``matching._resolve_thresholds`` read at triage time).

The one thing this wave must get right (Batch 116 handoff, verbatim)
----------------------------------------------------------------------
Batch 116 spent three adversarial audit rounds proving ``matching._resolve_thresholds``
is a faithful *reader*: given a config, it obeys it, because "auto-approve at this score"
is a legitimate instruction to give a reader. Its final ledger row records the handoff:
*"it is the first real WRITER of `supplier_overrides` — make validate-and-floor at the
write site an explicit acceptance criterion."* This module is that writer, and the
invariant is enforced by **reusing the reader's own guard**, not a second copy of it:

    ``_validate_candidate_thresholds`` builds the exact ``thresholds`` dict
    ``do_match_invoice`` would resolve against if the candidate were written, and
    passes it through ``matching._resolve_thresholds`` — the identical function the
    scorer calls at triage time. If it raises, the candidate is refused BEFORE either
    of the two writes below runs, so a rejected candidate leaves zero trace anywhere
    — neither the config file nor the audit ledger is touched.

No copy of ``_MIN_GREEN`` (60, not 50 — ``_score_project`` is earned unconditionally
from the invoice, not the candidate, so the floor must exceed the PO-nr+project sum,
see ``matching.py``) is hardcoded here; it is imported and can never drift from the one
the scorer actually enforces. ``_coerce_cutoff`` (bool/NaN/±inf rejection, Decimal
coercion) is reused the same way for reading the existing baseline off disk.

Auditor-reconstructable N=100 window (the durable design point)
------------------------------------------------------------------
A plain "last N rows for this supplier" query is **not** reconstructable after the
fact: rerun it a day later, after more decisions have been recorded, and a naive
``ORDER BY created_at DESC LIMIT N`` returns a *different* set of rows than the one
that actually produced the threshold change. So every successful recalibration writes
its OWN audit row (``tlx_scores->>'event_type' = 'economy_recalibration'``) carrying
the concrete ``window_ledger_ids`` — the exact ledger row ids that fed the computation,
frozen at recalibration time. An auditor reconstructs the window with
``SELECT * FROM v3_cognitive_ledger WHERE id = ANY(window_ledger_ids)`` — a query whose
answer never changes, regardless of what is inserted afterwards.

Design decisions this wave owns (not specified by the batch prompt)
------------------------------------------------------------------------
- **Decision vocabulary** (``"accept"`` / ``"override"``) mirrors the Batch 51
  procurement precedent (``nce/vertical_modules/procurement/recalibration.py``) for
  cross-engine consistency, but the supplier key is named ``supplier_orgnr`` (not
  procurement's ``supplier_id``) because that is the exact field
  ``matching._resolve_thresholds`` keys its override lookup on — using the same name
  end-to-end is what makes the reuse in ``_validate_candidate_thresholds`` correct.
- **Movement formula** rescales the Batch 51 fractional-weight formula
  (``(precision - 0.5) × 0.1``, clamped to ±0.05) onto this engine's 0–180 POINT scale:
  ``trust_delta = (precision - 0.5) × 20``, clamped to ``±_MAX_POINT_DELTA`` (10 points
  — two of matching.py's 5-point scoring increments; a deliberately conservative
  per-window step, not a config knob). Both ``green`` and ``yellow`` shift by the same
  delta so the review-band width is preserved; only its position moves. This clamp is
  **not** the floor guard: it bounds this module's own computed adjustment, and is
  wholly separate from ``_validate_candidate_thresholds`` refusing an out-of-range
  candidate outright (clamping our own delta is a design choice; silently reinterpreting
  an invalid config value is exactly what Batch 116 spent three rounds eliminating, and
  this module never does that).
- **``config_path`` injection.** ``economy-match-thresholds.json`` is a single
  repo-tracked file (config-as-IP, no per-namespace copy exists yet — a known,
  documented gap, not this wave's to close). Tests must never mutate that tracked file,
  so every write/read goes through a ``config_path`` parameter (defaulting to the real
  path in production) that tests point at a ``tmp_path`` fixture instead.
- **Architectural limitation this wave does NOT close (documented, not silently
  ignored): overrides are GLOBAL, not per-tenant.** ``economy-match-thresholds.json``
  is one file shared by every namespace; ``supplier_overrides`` has no namespace
  dimension at all. A recalibration run for one tenant's namespace therefore moves
  the auto-approve/review band for **every other tenant** that happens to share the
  same ``supplier_orgnr`` string — there is no per-namespace copy of this file to
  isolate them. Fixing that properly needs namespace-scoped storage, and therefore a
  migration, which this wave may not add. This is flagged here so the next wave
  inherits an accurate description of the gap rather than a reassuring one.

Concurrency and audit-ledger ordering (Batch 126 round 2 fix-forward)
-----------------------------------------------------------------------
An adversarial audit of the first Batch 126 cut found two CRITICAL defects in how
this module wrote its two durable artefacts — the shared config file and the
append-only audit ledger — and this section documents the fix, in the order the
code now actually runs:

1. **The whole read-modify-write against the shared config file is now serialized**
   by a Postgres advisory lock keyed on the resolved ``config_path``
   (``_locked_config_path`` / ``_advisory_lock_key``). Two concurrent
   ``do_recalibrate_supplier`` calls for *different* suppliers used to be able to
   each snapshot the file, compute a candidate against their own stale copy, and
   write — the second writer's blind ``dict(current_config)`` merge silently erased
   the first writer's override. The lock closes this by holding a single
   session-scoped ``pg_advisory_lock`` across the entire section from the (now
   re-)read of ``current_config`` through the final ``_write_config_atomic`` call,
   so the second caller through the gate always re-reads a file that already
   contains the first caller's change. This is a **cluster-wide** Postgres
   primitive, not an in-process ``asyncio.Lock`` — it serializes against a
   genuinely different OS process hitting the same database with the same config
   path, which is the case the reported race was actually about (two independent
   callers of this function, not two coroutines of one call). ``pg_advisory_lock``/
   ``pg_advisory_unlock`` (session-scoped) are used rather than
   ``pg_advisory_xact_lock`` (transaction-scoped) because the guarded section spans
   more than one Postgres transaction — the audit-ledger INSERT below commits on
   its own connection before the file write happens — and a transaction-scoped lock
   would release the instant that INSERT committed, reopening the exact window it
   exists to close.
2. **The audit-ledger row is now written BEFORE the config file, not after.**
   Previously the file was written and committed first, and the audit-ledger INSERT
   — on a separate ``scoped_pg_session`` — came second with nothing compensating a
   failure of that second step: a monkeypatched failure of the ledger insert left
   the config file updated with **zero** matching ledger rows, and because
   ``baseline_green``/``baseline_yellow`` are re-read from the now-mutated file on
   any retry, the untracked jump became permanently unrecoverable — nothing to
   reconcile against. Reversing the order makes the failure mode strictly safer for
   this module's purpose: if the ledger INSERT succeeds but the file write that
   follows it then fails (disk full, permissions, etc.), scoring behaviour is
   **unchanged** (the file was never touched) and the mismatch is **trivially
   detectable** — compare the newest ``economy_recalibration`` ledger row for a
   supplier against that supplier's live entry in ``supplier_overrides`` on disk;
   a declared-but-never-applied change shows up immediately as a difference between
   the two, rather than as a silent, uncompensated gap with nothing to compare
   against. If the ledger INSERT itself fails, nothing has been written anywhere
   (guard-rejection and ledger-failure now have identical zero-trace semantics).

WORM / RLS invariants
----------------------
- Every SQL statement filters ``namespace_id = $1`` explicitly — RLS is defense in
  depth, never the only guard (owner-pool tests and superuser connections bypass even
  FORCE RLS).
- ``v3_cognitive_ledger`` is append-only: this module only ever INSERTs.
- No SQL ``LIKE`` anywhere (supplier lookups are exact-match, never prefix).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import POOL_ACQUIRE_TIMEOUT, scoped_pg_session
from nce.vertical_modules.economy.matching import _MIN_GREEN, _coerce_cutoff, _resolve_thresholds

log = logging.getLogger("nce.vertical_modules.economy.recalibration")

# Agent label written into v3_cognitive_ledger.model_version for every row this module
# writes (both decision rows and recalibration-audit rows — distinguished by
# tlx_scores->>'event_type', not by model_version).
_MODEL_VERSION = "economy-recal-1.0"

# Zero tensor matching the NOT NULL empathic_tensor column (float[6] in the live schema).
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

_DECISION_EVENT_TYPE = "economy_match_decision"
_RECAL_EVENT_TYPE = "economy_recalibration"
_VALID_DECISIONS = frozenset({"accept", "override"})
_VALID_TIERS = frozenset({"GREEN", "YELLOW", "RED"})

# Precision midpoint — suppliers above this earn a positive (loosening) delta.
_PRECISION_MIDPOINT: float = 0.5

# Maximum per-window point movement (conservative: earned from a whole window of data,
# never from a single lucky batch — mirrors the Batch 51 procurement precedent's
# _MAX_DELTA, rescaled from a 0-1 weight space to this engine's 0-180 point scale).
_MAX_POINT_DELTA: float = 10.0

# Scale factor so precision=1.0 -> +_MAX_POINT_DELTA and precision=0.0 -> -_MAX_POINT_DELTA.
_DELTA_SCALE: float = _MAX_POINT_DELTA / (1.0 - _PRECISION_MIDPOINT)

# Config-as-IP path — mirrors matching.py's own _CONFIG_DATA_DIR calculation exactly.
_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"
_THRESHOLDS_PATH = _CONFIG_DATA_DIR / "economy-match-thresholds.json"


# ---------------------------------------------------------------------------
# do_record_match_decision — append-only ledger write
# ---------------------------------------------------------------------------


async def do_record_match_decision(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    supplier_orgnr: str,
    decision: str,
    score: int,
    tier: str | None = None,
) -> dict[str, Any]:
    """Append one invoice-match outcome to ``v3_cognitive_ledger`` (append-only).

    Parameters
    ----------
    pg_pool:
        asyncpg connection pool. RLS context is set inside ``scoped_pg_session``.
    namespace_id:
        Tenant namespace UUID — every write is scoped to this namespace.
    supplier_orgnr:
        The invoice's supplier orgnr — the exact key ``matching._resolve_thresholds``
        uses for its per-supplier override lookup (normalised the same way: stored
        ``str(...).strip()``).
    decision:
        ``"accept"`` — the automated tier stood (a human did not override it).
        ``"override"`` — a human corrected the automated triage.
    score:
        The invoice's total match score from ``do_match_invoice`` at decision time
        (0..180 on this engine's point scale; see ``matching.py``).
    tier:
        Optional — the tier (``GREEN``/``YELLOW``/``RED``) ``do_match_invoice`` assigned.

    Returns
    -------
    dict with ``ledger_id`` (str UUID of the inserted row) and ``supplier_orgnr``
    (the normalised key actually stored).
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"decision must be 'accept' or 'override', got {decision!r}")
    if tier is not None and tier not in _VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}")

    key = _normalized_supplier_key(supplier_orgnr)
    ledger_id = uuid.uuid4()

    payload: dict[str, Any] = {
        "event_type": _DECISION_EVENT_TYPE,
        "supplier_orgnr": key,
        "decision": decision,
        "score": score,
        "tier": tier,
    }

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                id, namespace_id, memory_id,
                empathic_tensor, tlx_scores, vad_scores, model_version
            ) VALUES (
                $1::uuid, $2::uuid, NULL,
                $3::float[], $4::jsonb, $5::jsonb, $6
            )
            """,
            str(ledger_id),
            str(namespace_id),
            _ZERO_TENSOR,
            json.dumps(payload),
            json.dumps({}),
            _MODEL_VERSION,
        )

    log.info(
        "[ECONOMY-RECAL] decision recorded supplier_orgnr=%s decision=%s score=%d ledger_id=%s",
        key,
        decision,
        score,
        ledger_id,
    )
    return {"ledger_id": str(ledger_id), "supplier_orgnr": key}


# ---------------------------------------------------------------------------
# Pure helpers — no DB, no filesystem (safe to unit-test directly)
# ---------------------------------------------------------------------------


def _normalized_supplier_key(supplier_orgnr: Any) -> str:
    """Normalise exactly the way ``matching._resolve_thresholds`` keys its lookup.

    Called ONCE per public entry point and threaded through everywhere else — the
    documented lesson from ``matching.py`` is that normalising at validation while
    looking up a raw value is the guard that manufactures the hole it exists to close.
    """
    return str(supplier_orgnr).strip()


def _overrides_without(overrides: dict[str, Any], key: str) -> dict[str, Any]:
    """Copy of *overrides* with any entry whose normalised key equals *key* removed.

    Prevents this module's own write from ever creating two raw keys that normalise to
    the same supplier (e.g. a stale ``"123 "`` left alongside a fresh ``"123"``) —
    ``matching._normalised_keys`` would then refuse ALL lookups the next time
    ``do_match_invoice`` runs for ANY supplier, not just this one.
    """
    return {raw_key: entry for raw_key, entry in overrides.items() if str(raw_key).strip() != key}


def _find_override_entry(overrides: dict[str, Any], key: str) -> Any:
    """Return the override entry whose normalised key equals *key*, or ``None``."""
    for raw_key, entry in overrides.items():
        if str(raw_key).strip() == key:
            return entry
    return None


def _derive_candidate_thresholds(
    baseline_green: float, baseline_yellow: float, precision: float
) -> tuple[int, int, float]:
    """Precision-earned threshold movement, rounded to the nearest point.

    ``precision`` = fraction of decisions in the window that were ``"accept"``.
    precision=1.0 (every decision accepted, none overridden) earns the full
    ``+_MAX_POINT_DELTA`` of trust -> the band LOOSENS (both cutoffs move down, easier
    to reach GREEN). precision=0.0 (every decision overridden — the automated tier was
    wrong every time) earns ``-_MAX_POINT_DELTA`` -> the band TIGHTENS. Both cutoffs
    shift by the same amount so the GREEN/YELLOW gap width is preserved.

    Returns ``(new_green, new_yellow, trust_delta)``. This function does not validate
    the result against the runtime floor — that is ``_validate_candidate_thresholds``'s
    job, deliberately kept separate (this one is pure arithmetic; that one is the guard).
    """
    raw_delta = (precision - _PRECISION_MIDPOINT) * _DELTA_SCALE
    trust_delta = max(-_MAX_POINT_DELTA, min(_MAX_POINT_DELTA, raw_delta))
    new_green = round(baseline_green - trust_delta)
    new_yellow = round(baseline_yellow - trust_delta)
    return new_green, new_yellow, trust_delta


def _validate_candidate_thresholds(
    current_config: dict[str, Any],
    supplier_orgnr: str,
    green: float,
    yellow: float,
) -> dict[str, Any]:
    """Guard a candidate (green, yellow) override the SAME way the reader will see it.

    Builds the exact ``thresholds`` dict ``do_match_invoice`` would resolve against if
    this candidate were written, and runs it through ``matching._resolve_thresholds`` —
    the identical function the scorer calls at triage time. Raises ``ValueError``
    (propagated verbatim from ``_resolve_thresholds``) when the candidate would breach
    ``green > _MIN_GREEN``, ``yellow > 0``, or ``green >= yellow``, or is otherwise not a
    usable numeric cutoff (bool/NaN/±inf). No constant is duplicated here that could
    drift from ``matching.py``'s — this calls the real guard instead of re-checking a copy.

    Pure — no DB, no filesystem. This is what proves the guard would fail red if the
    ``_resolve_thresholds`` call below were ever removed or bypassed.
    """
    overrides = current_config.get("supplier_overrides")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError(
            f"economy match thresholds: 'supplier_overrides' must be an object, "
            f"got {type(overrides).__name__}"
        )
    trial_thresholds: dict[str, Any] = {
        "green": current_config.get("green"),
        "yellow": current_config.get("yellow"),
        "supplier_overrides": {
            **_overrides_without(overrides, supplier_orgnr),
            supplier_orgnr: {"green": green, "yellow": yellow},
        },
    }
    return _resolve_thresholds(trial_thresholds, {"supplier_orgnr": supplier_orgnr})


# ---------------------------------------------------------------------------
# Config-as-IP file I/O — path-injectable so tests never touch the tracked file
# ---------------------------------------------------------------------------


def _read_config(path: Path) -> dict[str, Any]:
    """Read and parse *path* as JSON. Not ``matching.load_economy_thresholds()`` — that
    function has no path parameter (it always reads the real repo file), and this
    module's write path needs to read/write the SAME injectable path so tests can point
    both at an isolated ``tmp_path`` copy without ever touching the tracked config."""
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _write_config_atomic(path: Path, config: dict[str, Any]) -> None:
    """Atomically replace *path* with *config*.

    Writes to a sibling temp file first, then ``os.replace`` (atomic rename on the same
    filesystem, both POSIX and Windows) — so a crash mid-write can never leave a
    partially-applied threshold set on disk; readers always see either the old file or
    the fully-written new one, never something in between.
    """
    serialized = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp_path.write_text(serialized, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _advisory_lock_key(path: Path) -> int:
    """Deterministic signed-64-bit Postgres advisory-lock key derived from *path*.

    Keyed on the file's resolved absolute path so that (a) different
    ``config_path`` values — the real repo file in production, a distinct
    ``tmp_path`` fixture per test — never contend with each other, and (b) every
    caller computing the key for the SAME path arrives at the identical bigint
    regardless of which OS process it runs in, which is what makes
    ``pg_advisory_lock`` actually serialize them: advisory locks are keyed purely
    by this integer, not by anything Python-process-local.
    """
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


@asynccontextmanager
async def _locked_config_path(pg_pool: asyncpg.Pool, path: Path) -> AsyncIterator[None]:
    """Hold a session-scoped Postgres advisory lock keyed on *path* for the block.

    Fix 2 (Batch 126 round 2 audit): ``economy-match-thresholds.json`` is one file
    shared by every tenant. Without this lock, two concurrent
    ``do_recalibrate_supplier`` calls for *different* suppliers could each snapshot
    the file, derive a candidate against their own now-stale copy, and write — the
    second writer's blind ``dict(current_config)`` merge silently erased the first
    writer's override, while the erased supplier's own audit-ledger row kept
    asserting a config that was no longer live. See the module docstring
    ("Concurrency and audit-ledger ordering") for the full failure-mode writeup.

    Uses session-scoped ``pg_advisory_lock``/``pg_advisory_unlock``, deliberately
    NOT transaction-scoped ``pg_advisory_xact_lock``: the section this lock guards
    spans more than one Postgres transaction (the audit-ledger INSERT commits on
    its own ``scoped_pg_session`` connection) plus a filesystem write after it — a
    lock that auto-released the instant that INSERT's transaction committed would
    reopen the exact race window this exists to close. Explicitly unlocked in a
    ``finally`` before the connection returns to the pool: forgetting that would
    leave the lock held for as long as this connection sits idle in the pool,
    starving every future recalibration for any supplier, not just this one.

    Cluster-wide, not process-local: this is a Postgres server-side primitive, so
    it correctly serializes against a genuinely different OS process hitting the
    same database with the same config path — not merely a different ``asyncio``
    task inside this interpreter. That is the case the reported race was actually
    about (two independent callers of ``do_recalibrate_supplier``).
    """
    key = _advisory_lock_key(path)
    async with pg_pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT) as conn:
        await conn.execute("SELECT pg_advisory_lock($1::bigint)", key)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1::bigint)", key)


# ---------------------------------------------------------------------------
# do_recalibrate_supplier — the N=100 rolling recalibration
# ---------------------------------------------------------------------------


async def _fetch_decision_window(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    key: str,
    window_n: int,
) -> list[asyncpg.Record]:  # type: ignore[type-arg]
    """Fetch the last *window_n* match-decision rows for one supplier, namespace-scoped
    EXPLICITLY (not relying on RLS alone). Newest first."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        return await conn.fetch(
            """
            SELECT id, tlx_scores
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  tlx_scores->>'event_type' = $2
              AND  tlx_scores->>'supplier_orgnr' = $3
            ORDER BY created_at DESC
            LIMIT  $4
            """,
            str(namespace_id),
            _DECISION_EVENT_TYPE,
            key,
            window_n,
        )


async def do_recalibrate_supplier(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    supplier_orgnr: str,
    window_n: int,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Recalibrate one supplier's (green, yellow) override from its rolling decision window.

    Reads the last ``window_n`` match-decision rows from ``v3_cognitive_ledger`` for
    ``supplier_orgnr`` in this namespace. Below ``window_n`` decisions, returns early
    without touching the config file (mirrors the Batch 51 procurement precedent).
    At or above ``window_n``, derives a candidate (green, yellow) and validates it via
    ``_validate_candidate_thresholds`` (reusing ``matching._resolve_thresholds`` — see
    module docstring). A rejected candidate raises ``ValueError`` before either durable
    write below runs, leaving both the config file and the ledger untouched.

    On success, the whole read-modify-write against the shared config file is
    serialized behind a Postgres advisory lock keyed on *path*
    (``_locked_config_path`` — see module docstring, "Concurrency and audit-ledger
    ordering"), and — inside that lock — the two writes happen in this order, not
    the reverse:

    1. The ``economy_recalibration`` audit-ledger row is appended FIRST (its own
       committed ``scoped_pg_session`` transaction), naming the exact
       ``window_ledger_ids`` that drove the computation and the candidate
       ``green``/``yellow`` this recalibration intends to apply.
    2. Only then is the new override atomically written to
       ``economy-match-thresholds.json`` (or ``config_path`` when given — tests
       always pass one).

    Failure semantics (deliberately asymmetric, see module docstring for the full
    writeup): if step 1 fails, nothing is written anywhere — same zero-trace
    guarantee as a guard rejection. If step 1 succeeds but step 2 then fails, the
    ledger row survives declaring an intended change that was never applied; live
    scoring behaviour is unaffected (the file was never touched), and the gap is
    trivially detectable by comparing that ledger row's ``green``/``yellow`` against
    the supplier's actual (or absent) entry in the on-disk ``supplier_overrides``.

    Parameters
    ----------
    pg_pool, namespace_id:
        Standard RLS-scoped connection parameters.
    supplier_orgnr:
        Which supplier to recalibrate (normalised the same way ``matching.py`` does).
    window_n:
        Rolling window size (N=100 in production; smaller in tests for speed). Must be
        >= 1.
    config_path:
        Override for the config file path. Defaults to the real
        ``nce/config_data/economy-match-thresholds.json``. Tests MUST pass a
        ``tmp_path``-based file — this module never mutates the tracked repo config
        during a test run.

    Returns
    -------
    dict with:
      ``recalibrated``      bool — True when a new override was computed and written.
      ``supplier_orgnr``    str  — the normalised key.
      ``decision_count``    int  — decisions found in the rolling window.
      ``precision``         float | None — fraction accepted (None when skipped).
      ``previous_green``/``previous_yellow``  float | None — the baseline before this
                             recalibration (existing override, or the top-level default
                             when the supplier had none yet).
      ``green``/``yellow``  int | None — the newly-persisted cutoffs (None when skipped).
      ``threshold_delta``   float | None — signed point movement applied.
      ``ledger_id``         str  — the audit row's id (only present when recalibrated).
      ``window_ledger_ids`` list[str] — the exact ledger row ids the window was built
                             from (present even when skipped, so callers can always see
                             what was read; auditor-reconstructable per the module
                             docstring).
    """
    if window_n < 1:
        raise ValueError(f"window_n must be >= 1, got {window_n!r}")

    key = _normalized_supplier_key(supplier_orgnr)
    path = config_path or _THRESHOLDS_PATH

    rows = await _fetch_decision_window(pg_pool, namespace_id, key, window_n)
    decision_count = len(rows)
    window_ledger_ids = [str(r["id"]) for r in rows]

    if decision_count < window_n:
        log.debug(
            "[ECONOMY-RECAL] skip supplier_orgnr=%s decisions=%d < window=%d",
            key,
            decision_count,
            window_n,
        )
        return {
            "recalibrated": False,
            "supplier_orgnr": key,
            "decision_count": decision_count,
            "precision": None,
            "previous_green": None,
            "previous_yellow": None,
            "green": None,
            "yellow": None,
            "threshold_delta": None,
            "window_ledger_ids": window_ledger_ids,
        }

    accepted = sum(
        1
        for r in rows
        if (
            json.loads(r["tlx_scores"]) if isinstance(r["tlx_scores"], str) else r["tlx_scores"]
        ).get("decision")
        == "accept"
    )
    precision = accepted / decision_count

    # Fix 2 (Batch 126 round 2): the entire read-modify-write against the shared
    # config file is serialized behind a Postgres advisory lock keyed on *path*.
    # ``current_config`` is (re-)read only AFTER the lock is held, so a concurrent
    # recalibration for a DIFFERENT supplier that finished first is always visible
    # here — this call never derives its candidate from, or writes on top of, a
    # stale pre-lock snapshot.
    async with _locked_config_path(pg_pool, path):
        current_config = _read_config(path)

        top_green = _coerce_cutoff(current_config.get("green"))
        top_yellow = _coerce_cutoff(current_config.get("yellow"))
        if top_green is None or top_yellow is None:
            raise ValueError(
                f"economy match thresholds config at {path} has a non-numeric top-level "
                f"green/yellow (green={current_config.get('green')!r} "
                f"yellow={current_config.get('yellow')!r}) — refusing to recalibrate "
                f"supplier_orgnr={key!r} against a broken base config"
            )

        raw_overrides = current_config.get("supplier_overrides")
        if raw_overrides is None:
            raw_overrides = {}
        if not isinstance(raw_overrides, dict):
            raise ValueError(
                f"economy match thresholds config at {path}: 'supplier_overrides' must be "
                f"an object, got {type(raw_overrides).__name__}"
            )

        existing_entry = _find_override_entry(raw_overrides, key)
        if existing_entry is not None and not isinstance(existing_entry, dict):
            raise ValueError(
                f"existing override for supplier_orgnr={key!r} must be an object, "
                f"got {type(existing_entry).__name__}"
            )
        existing_entry = existing_entry or {}

        baseline_green = _coerce_cutoff(existing_entry.get("green", top_green))
        baseline_yellow = _coerce_cutoff(existing_entry.get("yellow", top_yellow))
        if baseline_green is None or baseline_yellow is None:
            raise ValueError(
                f"existing override for supplier_orgnr={key!r} has a non-numeric "
                f"green/yellow — refusing to recalibrate on top of a broken baseline"
            )

        new_green, new_yellow, trust_delta = _derive_candidate_thresholds(
            baseline_green, baseline_yellow, precision
        )

        # Validate-and-floor at the write site (Batch 116 handoff) — the SAME guard the
        # scorer runs at read time. Raising here leaves the config file (and the ledger)
        # completely untouched: no partially-applied write, ever.
        try:
            _validate_candidate_thresholds(current_config, key, new_green, new_yellow)
        except ValueError as exc:
            raise ValueError(
                f"economy recalibration for supplier_orgnr={key!r} REJECTED: candidate "
                f"green={new_green} yellow={new_yellow} (baseline green={baseline_green} "
                f"yellow={baseline_yellow}, delta={trust_delta:.2f}) would violate the "
                f"runtime threshold invariants (green must exceed {_MIN_GREEN}, yellow must "
                f"be positive, green must be >= yellow) — leaving {path} unchanged: {exc}"
            ) from exc

        audit_payload: dict[str, Any] = {
            "event_type": _RECAL_EVENT_TYPE,
            "supplier_orgnr": key,
            "decision_count": decision_count,
            "precision": precision,
            "previous_green": baseline_green,
            "previous_yellow": baseline_yellow,
            "green": new_green,
            "yellow": new_yellow,
            "threshold_delta": trust_delta,
            "window_ledger_ids": window_ledger_ids,
        }
        ledger_id = uuid.uuid4()

        # Fix 1 (Batch 126 round 2): the audit-ledger row is written BEFORE the
        # config file, not after. If the file write below then fails, this row
        # survives as a detectable "intended, not applied" declaration (compare its
        # green/yellow against the supplier's live on-disk override) instead of the
        # old ordering's silent, unrecoverable, untracked config mutation. See the
        # module docstring ("Concurrency and audit-ledger ordering") for the full
        # failure-mode writeup.
        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (
                    id, namespace_id, memory_id,
                    empathic_tensor, tlx_scores, vad_scores, model_version
                ) VALUES (
                    $1::uuid, $2::uuid, NULL,
                    $3::float[], $4::jsonb, $5::jsonb, $6
                )
                """,
                str(ledger_id),
                str(namespace_id),
                _ZERO_TENSOR,
                json.dumps(audit_payload),
                json.dumps({}),
                _MODEL_VERSION,
            )

        new_overrides = _overrides_without(raw_overrides, key)
        new_overrides[key] = {"green": new_green, "yellow": new_yellow}
        new_config = dict(current_config)
        new_config["supplier_overrides"] = new_overrides

        _write_config_atomic(path, new_config)

    log.info(
        "[ECONOMY-RECAL] recalibrated supplier_orgnr=%s precision=%.3f "
        "green=%s->%s yellow=%s->%s ledger_id=%s",
        key,
        precision,
        baseline_green,
        new_green,
        baseline_yellow,
        new_yellow,
        ledger_id,
    )

    return {
        "recalibrated": True,
        "supplier_orgnr": key,
        "decision_count": decision_count,
        "precision": precision,
        "previous_green": baseline_green,
        "previous_yellow": baseline_yellow,
        "green": new_green,
        "yellow": new_yellow,
        "threshold_delta": trust_delta,
        "ledger_id": str(ledger_id),
        "window_ledger_ids": window_ledger_ids,
    }
