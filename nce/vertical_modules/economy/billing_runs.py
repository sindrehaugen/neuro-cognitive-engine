"""
nce/vertical_modules/economy/billing_runs.py
=============================================
B-12 billing-run generation (Lane G, dispatched by ML-orch under Sindre's
extended Q-5 carve-out — owner engine: economy).

A billing run consolidates one candidate invoice per agreement per billing
period, priced from the rooms that agreement's ``covers`` edge (Agreements'
own aspect — ``agreements/sla.py::do_set_sla_coverage``) names, via C-2's
room category (``system_design/room_categories.py::get_fl_room_category``)
mapped to a B-10 price tier (``agreements/room_category_pricing.py::
resolve_price_tier``) and priced with the existing ``sla_room_pricing`` rule
(``agreements/price_rules.py::evaluate_price_rule``).

Refuse-and-name, at the run level
----------------------------------
``resolve_price_tier`` refuses loudly on a room whose C-2 category has no
priced tier (Sindre ruling, 2026-09-20, ``ROOM_CATEGORY_PRICE_MAPPING.md``).
A billing run is the auditable unit here, not the individual candidate — so
this module refuses the WHOLE run rather than emitting every other
agreement's candidate and silently dropping the one with the unpriced room:
a billing run that is right about 9 customers and silently wrong (missing
a room, and nothing about the output says so) about the 10th is worse than
one that bills nobody and says exactly which room stopped it. Concretely:
pricing is computed for every requested agreement BEFORE any row is
written; if any room fails to resolve a tier, nothing is written except a
single ``economy_billing_runs`` row with ``status='failed'`` naming the
category and the room (``failure_reason``) — no partial candidates.

CUSTOM_PROJECT rooms never trigger a refusal
-----------------------------------------------
Sindre's follow-up ruling (2026-09-20) on the three-way collapse question
introduced a third outcome alongside "priced" and "refuse": CONFERENCE_MEDIUM/
CONFERENCE_LARGE are ``CUSTOM_PROJECT`` -- priced per project, outside
``sla_room_pricing`` entirely. This module treats that as "skip this room,
it bills elsewhere," never as a reason to refuse the run -- conflating the
two would make a billing run refuse on rooms that are correctly priced,
which is exactly the false-positive failure mode refuse-and-name exists to
avoid. Skipped custom-project rooms are named in ``custom_project_fl_labels``
on the return value (both the confirmed and failed shapes) so they stay
visible without being mistaken for an unpriced gap.

Explicit scope, not inferred
-----------------------------
``agreements.status`` is a free-text column with no enum or CHECK
constraint anywhere in this codebase (checked: no migration constrains it,
no module defines canonical values). Guessing a filter like
``status = 'active'`` would invent a convention that does not exist yet, the
same mistake the charter warned against when it said not to trust a stated
premise without checking it directly. So this module never scans
``agreements`` for "eligible" rows — the caller passes ``agreement_ids``
explicitly, exactly as ``procurement/po.py::do_generate_po`` takes explicit
``candidates``/``artnrs`` rather than scanning for eligible suppliers. A
scheduler that decides which agreements are due this period is a separate,
later concern (hand to whichever lane owns billing scheduling) once
``agreements.status``/``billing_frequency`` semantics are actually defined.

``on_site`` is not modelled on ``agreements`` at all (checked: no column).
Rather than block billing generation on a field that does not exist yet,
this defaults ``on_site=False`` (the cheaper, non-premium rate) — favouring
under- over over-charging when a real per-agreement flag is later added.
This is a genuine gap, not a silent wrong answer, and is worth raising with
ML-orch/Sindre separately; it does not block B-12's schema/logic layer.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.entity_resolution.ownership import assert_owner
from nce.vertical_modules.agreements.price_rules import evaluate_price_rule
from nce.vertical_modules.agreements.room_category_pricing import (
    CUSTOM_PROJECT,
    UnpricedRoomCategoryError,
    resolve_price_tier,
)
from nce.vertical_modules.system_design.room_categories import get_fl_room_category

log = logging.getLogger("nce.vertical_modules.economy.billing_runs")

_NODE_TYPE_BILLING_RUN = "BILLING_RUN"
_NODE_TYPE_BILLING_CANDIDATE = "BILLING_CANDIDATE"
_OWNER_ENGINE = "economy"
_PRED_HAS_CANDIDATE = "has_candidate"
# The rule's rule_id in price-rules.json -- NOT its rule_type ("sla_room_pricing",
# the string evaluate_price_rule() branches on internally). get_price_rules()
# looks up by rule_id.
_PRICE_RULE_ID = "RULE_SLA_ROOM_CATEGORY"
# Mirrors agreements/sla.py's own _PRED_COVERS -- not imported (private
# cross-module symbol); both name the same kg_edges predicate by convention.
_PRED_COVERS = "covers"


class AgreementNotFoundError(ValueError, KeyError):
    """Raised when a requested agreement does not exist in this namespace."""


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


def _parse_date(val: Any, field_name: str) -> datetime.date:
    """Coerce to ``datetime.date`` at the boundary.

    asyncpg does not coerce a ``date``-typed column parameter -- it requires
    an actual ``datetime.date`` and raises ``DataError`` on a plain ISO
    string (``'str' object has no attribute 'toordinal'``). Accepting an ISO
    string from callers (the natural shape for an MCP/JSON argument) means
    this module, not every caller, owns the conversion.
    """
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val
    if not val:
        raise ValueError(f"{field_name} is required")
    try:
        return datetime.date.fromisoformat(str(val))
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} date: {val!r}") from exc


def _billing_run_idempotency_key(
    namespace_id: str,
    period_start: str,
    period_end: str,
    agreement_ids: list[str],
) -> str:
    """Stable idempotency key: same namespace/period/agreement-set replays as a no-op.

    Mirrors ``procurement/po.py::_derive_po_idempotency_key`` — hash of the
    call's own inputs, sorted for stability, no caller-supplied nonce needed.
    """
    payload = json.dumps(
        {
            "namespace_id": namespace_id,
            "period_start": period_start,
            "period_end": period_end,
            "agreement_ids": sorted(agreement_ids),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "generate_billing_run:" + hashlib.sha256(payload.encode()).hexdigest()


async def _get_covered_fl_labels(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    agreement_id: UUID,
) -> list[str]:
    """The ``covers`` edge targets for one agreement, read on the caller's own conn.

    Same query ``agreements/sla.py::get_sla_coverage`` runs, inlined rather
    than called directly — ``get_sla_coverage`` opens its own
    ``scoped_pg_session(pool, ...)``, and this function must stay inside the
    single transaction ``@governed`` already opened for ``do_generate_billing_run``.
    """
    agreement_label = f"Agreement:{agreement_id}"
    rows = await conn.fetch(
        """
        SELECT object_label
        FROM   kg_edges
        WHERE  subject_label = $1
          AND  predicate = $2
          AND  namespace_id = $3::uuid
        ORDER BY object_label
        """,
        agreement_label,
        _PRED_COVERS,
        str(namespace_id),
    )
    return [r["object_label"] for r in rows]


async def _price_agreement(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    agreement_id: UUID,
    *,
    on_site: bool,
) -> dict[str, Any] | None:
    """Compute (never write) one agreement's priced candidate, or ``None`` if uncovered.

    Raises ``UnpricedRoomCategoryError`` (a covered room's C-2 category has
    no B-10 tier at all) or ``ValueError`` (a covered room has no C-2
    category assigned at all) — both propagate to the caller, which treats
    either as "the whole run refuses." A room whose category resolves to
    ``CUSTOM_PROJECT`` (Sindre ruling, 2026-09-20: CONFERENCE_MEDIUM/
    CONFERENCE_LARGE are priced per project, outside ``sla_room_pricing``)
    is neither an error nor billed by this rule -- it is skipped and named
    in ``custom_project_fl_labels`` so it is visible without being mistaken
    for an unpriced gap.
    """
    fl_labels = await _get_covered_fl_labels(conn, namespace_id, agreement_id)
    if not fl_labels:
        return None

    room_counts: dict[str, int] = {}
    fl_labels_by_tier: dict[str, list[str]] = {}
    custom_project_fl_labels: list[str] = []
    for fl_label in fl_labels:
        category_info = await get_fl_room_category(conn, namespace_id, fl_id_or_label=fl_label)
        category_id = category_info.get("category_id")
        if not category_id:
            raise ValueError(
                f"FUNCTIONAL_LOCATION {fl_label!r} has no C-2 room category assigned "
                "-- cannot price without one (assign via system_design.room_categories "
                "before billing this agreement)"
            )
        tier = resolve_price_tier(category_id, fl_label=fl_label)
        if tier == CUSTOM_PROJECT:
            # Priced elsewhere, not a gap -- skip, never pass CUSTOM_PROJECT
            # into room_counts (price_rules.py would silently zero-rate it).
            custom_project_fl_labels.append(fl_label)
            continue
        room_counts[tier] = room_counts.get(tier, 0) + 1
        fl_labels_by_tier.setdefault(tier, []).append(fl_label)

    if not room_counts:
        # Every covered room was custom-project (or there were none) --
        # nothing for sla_room_pricing to price. Not billed by this rule,
        # not an error either; the caller's skipped_agreements list already
        # exists for exactly this "nothing to bill here" shape.
        return {
            "agreement_id": agreement_id,
            "fl_labels_by_tier": {},
            "pricing": None,
            "custom_project_fl_labels": custom_project_fl_labels,
        }

    pricing = evaluate_price_rule(_PRICE_RULE_ID, {"room_counts": room_counts, "on_site": on_site})
    return {
        "agreement_id": agreement_id,
        "fl_labels_by_tier": fl_labels_by_tier,
        "pricing": pricing,
        "custom_project_fl_labels": custom_project_fl_labels,
    }


async def _upsert_billing_run_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
) -> None:
    await assert_owner(conn, namespace_id, _NODE_TYPE_BILLING_RUN, _OWNER_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3::uuid, 'agent')
        ON CONFLICT (label, namespace_id) DO NOTHING
        """,
        label,
        _NODE_TYPE_BILLING_RUN,
        namespace_id,
    )


async def _upsert_billing_candidate_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
    run_label: str,
) -> None:
    await assert_owner(conn, namespace_id, _NODE_TYPE_BILLING_CANDIDATE, _OWNER_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3::uuid, 'agent')
        ON CONFLICT (label, namespace_id) DO NOTHING
        """,
        label,
        _NODE_TYPE_BILLING_CANDIDATE,
        namespace_id,
    )
    await conn.execute(
        """
        INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
        VALUES ($1, $2, $3, 1.0, $4::uuid, 'agent')
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
        """,
        run_label,
        _PRED_HAS_CANDIDATE,
        label,
        namespace_id,
    )


@governed(action_type="generate_billing_run")
async def do_generate_billing_run(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    engine: Any = None,
    period_start: Any,
    period_end: Any,
    agreement_ids: list[Any],
    on_site: bool = False,
) -> dict[str, Any]:
    """Governed, confirm-first generation of one billing run (B-12).

    Pricing for every requested agreement is computed first, with no writes;
    only if every agreement prices cleanly does the second pass write the
    ``economy_billing_runs`` / ``economy_billing_candidates`` /
    ``economy_billing_candidate_lines`` rows and their graph nodes/edges
    (module docstring: "refuse-and-name, at the run level"). An agreement
    with no ``covers`` edge yet is not an error -- it is skipped and named
    in ``skipped_agreements`` (nothing to bill, not a pricing failure).

    Parameters
    ----------
    conn:
        asyncpg connection inside an active transaction (``scoped_pg_session``),
        opened by the caller -- ``@governed`` never opens one itself.
    namespace_id:
        Tenant UUID.
    idempotency_key:
        Stable hash of the call inputs. Derive via ``_billing_run_idempotency_key``.
    confirm:
        ``False`` (default) -- ``@governed`` returns ``pending_approval`` and
        this body never runs. ``True`` -- executes once.
    period_start, period_end:
        ``datetime.date`` or ISO date string bounding the billing period.
    agreement_ids:
        Agreements to bill this run. Explicit, not scanned (module docstring:
        "explicit scope, not inferred") -- every id must exist in this
        namespace or the whole call raises ``AgreementNotFoundError`` before
        any pricing or writes happen.
    on_site:
        Passed through to ``price_rules.evaluate_price_rule``'s
        ``sla_room_pricing`` rule. Defaults ``False`` (module docstring
        explains why: no per-agreement field exists yet).

    Returns
    -------
    dict with ``status`` ('confirmed' or 'failed'), ``billing_run_id``,
    ``billing_run_label``, ``candidate_count``, ``candidates`` (list, empty
    on failure), ``skipped_agreements``, and -- only when ``status`` is
    'failed' -- ``failure_reason`` naming the category and the room.
    """
    ns_uuid = _parse_uuid(namespace_id, "namespace_id")
    if not agreement_ids:
        raise ValueError("agreement_ids must be non-empty")
    agreement_uuids = [_parse_uuid(a, "agreement_ids[]") for a in agreement_ids]
    period_start = _parse_date(period_start, "period_start")
    period_end = _parse_date(period_end, "period_end")

    rows = await conn.fetch(
        """
        SELECT id, customer_id
        FROM   agreements
        WHERE  namespace_id = $1::uuid
          AND  id = ANY($2::uuid[])
        """,
        ns_uuid,
        agreement_uuids,
    )
    agreements_by_id = {row["id"]: row for row in rows}
    missing = [a for a in agreement_uuids if a not in agreements_by_id]
    if missing:
        raise AgreementNotFoundError(
            f"Agreement(s) not found in namespace {ns_uuid}: {[str(m) for m in missing]}"
        )

    run_id = uuid4()
    run_label = f"{_NODE_TYPE_BILLING_RUN}:{run_id}"

    # ------------------------------------------------------------------
    # Pass 1: price every agreement. No writes yet -- any failure here
    # means NOTHING from this run is written except the failed marker row.
    # ------------------------------------------------------------------
    priced: list[dict[str, Any]] = []
    skipped_agreements: list[str] = []
    custom_project_fl_labels: list[str] = []
    try:
        for agreement_uuid in agreement_uuids:
            result = await _price_agreement(conn, ns_uuid, agreement_uuid, on_site=on_site)
            if result is None:
                skipped_agreements.append(str(agreement_uuid))
                continue
            custom_project_fl_labels.extend(result.get("custom_project_fl_labels", []))
            if result["pricing"] is None:
                # Every covered room was custom-project -- nothing for
                # sla_room_pricing to bill, same as no coverage at all.
                skipped_agreements.append(str(agreement_uuid))
                continue
            priced.append(result)
    except UnpricedRoomCategoryError as exc:
        failure_reason = str(exc)
        await _upsert_billing_run_node(conn, ns_uuid, run_label)
        await conn.execute(
            """
            INSERT INTO economy_billing_runs
                (id, namespace_id, node_label, period_start, period_end,
                 status, failure_reason, candidate_count)
            VALUES ($1, $2::uuid, $3, $4, $5, 'failed', $6, 0)
            """,
            run_id,
            ns_uuid,
            run_label,
            period_start,
            period_end,
            failure_reason,
        )
        log.warning(
            "generate_billing_run: run=%s ns=%s refused -- %s",
            run_label,
            ns_uuid,
            failure_reason,
        )
        return {
            "status": "failed",
            "billing_run_id": str(run_id),
            "billing_run_label": run_label,
            "period_start": str(period_start),
            "period_end": str(period_end),
            "failure_reason": failure_reason,
            "candidate_count": 0,
            "candidates": [],
            "skipped_agreements": skipped_agreements,
            "custom_project_fl_labels": custom_project_fl_labels,
        }

    # ------------------------------------------------------------------
    # Pass 2: every agreement priced cleanly -- write the run + candidates.
    # ------------------------------------------------------------------
    await _upsert_billing_run_node(conn, ns_uuid, run_label)
    await conn.execute(
        """
        INSERT INTO economy_billing_runs
            (id, namespace_id, node_label, period_start, period_end,
             status, candidate_count)
        VALUES ($1, $2::uuid, $3, $4, $5, 'confirmed', $6)
        """,
        run_id,
        ns_uuid,
        run_label,
        period_start,
        period_end,
        len(priced),
    )

    candidates: list[dict[str, Any]] = []
    for item in priced:
        agreement_uuid = item["agreement_id"]
        agreement_row = agreements_by_id[agreement_uuid]
        pricing = item["pricing"]
        fl_labels_by_tier = item["fl_labels_by_tier"]

        candidate_id = uuid4()
        candidate_label = f"{_NODE_TYPE_BILLING_CANDIDATE}:{candidate_id}"
        total_amount = Decimal(str(pricing["total_monthly_amount"]))

        # kg_nodes row must exist before the FK'd economy_billing_candidates
        # insert below (fk_economy_billing_candidates_kg_nodes).
        await _upsert_billing_candidate_node(conn, ns_uuid, candidate_label, run_label)

        await conn.execute(
            """
            INSERT INTO economy_billing_candidates
                (id, namespace_id, node_label, billing_run_id, customer_id,
                 agreement_id, period_start, period_end, currency, total_amount, status)
            VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, 'draft')
            """,
            candidate_id,
            ns_uuid,
            candidate_label,
            run_id,
            agreement_row["customer_id"],
            agreement_uuid,
            period_start,
            period_end,
            pricing["currency"],
            total_amount,
        )

        for line in pricing["lines"]:
            tier = line["room_category"]
            await conn.execute(
                """
                INSERT INTO economy_billing_candidate_lines
                    (namespace_id, billing_candidate_id, price_tier, room_count,
                     unit_monthly_rate, line_amount, covered_fl_labels)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                ns_uuid,
                candidate_id,
                tier,
                line["count"],
                Decimal(str(line["unit_monthly_rate"])),
                Decimal(str(line["total_monthly"])),
                json.dumps(fl_labels_by_tier[tier]),
            )

        candidates.append(
            {
                "billing_candidate_id": str(candidate_id),
                "billing_candidate_label": candidate_label,
                "agreement_id": str(agreement_uuid),
                "customer_id": str(agreement_row["customer_id"])
                if agreement_row["customer_id"]
                else None,
                "total_amount": float(total_amount),
                "currency": pricing["currency"],
                "lines": pricing["lines"],
            }
        )

    log.info(
        "generate_billing_run: run=%s ns=%s candidates=%d skipped=%d",
        run_label,
        ns_uuid,
        len(candidates),
        len(skipped_agreements),
    )

    return {
        "status": "confirmed",
        "billing_run_id": str(run_id),
        "billing_run_label": run_label,
        "period_start": str(period_start),
        "period_end": str(period_end),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "skipped_agreements": skipped_agreements,
        "custom_project_fl_labels": custom_project_fl_labels,
    }
