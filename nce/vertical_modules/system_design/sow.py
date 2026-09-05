"""
nce/vertical_modules/system_design/sow.py
==========================================
Statement-of-Work generator for the System Design vertical (Wave 5).

Two concerns, separated by a clean boundary:

1. ``generate_sow(SoWInput, version_number) -> SoWDoc``
   Pure transform lifted near-1:1 from ``lib/sow/generator.ts:183``.
   **Zero DB reads** — caller is responsible for assembling *SoWInput*
   before calling.  Deterministic for identical inputs.

2. ``do_generate_sow(engine, params) -> dict``
   Async adapter that reads the ``DESIGN`` / ``DESIGN_LINE`` /
   ``FUNCTIONAL_LOCATION`` subgraph (authored in Wave 2), assembles
   per-room ``SoWInput``, and calls the pure transform.

Freeze-on-issue (Correction #7):
   The SoW ``version_number`` is derived from the design's own version
   counter.  Once issued for a given design version the doc is
   **immutable** — re-issuing the same (design_id, version) returns the
   *identical* frozen document without regenerating.  To get a new SoW
   you must bump the design version (i.e. a new human-validated revision).
   The frozen doc is a **pure return value**; persistence to SharePoint
   is Wave 10.  No new DB table is needed or used.

Design invariants (uncle-bob-craft):
  - No web / HTTP / admin imports; domain core only.
  - ``generate_sow`` has no IO — pure function, no side effects.
  - ``do_generate_sow`` opens exactly one scoped session, does all reads
    inside it, then calls the pure transform outside the session.
  - ``confidence`` only on edges, never nodes (rule 7).
  - No ``metadata``, ``payload``, or ``state`` column on kg_nodes.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from nce.config import DeploymentConfigurationError, cfg
from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.system_design.sow")

# ---------------------------------------------------------------------------
# Label constants — match graph.py conventions exactly
# ---------------------------------------------------------------------------

_NODE_TYPE_DESIGN: str = "DESIGN"
_NODE_TYPE_DESIGN_LINE: str = "DESIGN_LINE"
_NODE_TYPE_FL: str = "FUNCTIONAL_LOCATION"

_PRED_CONTAINS: str = "contains"
_PRED_NEEDS: str = "needs"

# ---------------------------------------------------------------------------
# Lookup tables (lifted 1:1 from generator.ts)
# ---------------------------------------------------------------------------

_CATEGORY_LABEL: dict[str, str] = {
    "installation": "Installasjon",
    "programming": "Programmering",
    "commissioning": "Idriftsettelse",
    "project_management": "Prosjektledelse",
    "design": "Design",
    "travel": "Reise",
}

_TIER_LABEL: dict[int, str] = {
    1: "Quick job (S)",
    2: "Mini-prosjekt (M)",
    3: "Prosjekt (L)",
    4: "Program (XL)",
}

_SERVICE_TIER_LABEL: dict[str, str] = {
    "OBSERVE": "Bronze — observerende",
    "PROTECT": "Silver — beskyttende",
    "COMPLETE": "Gold — fullstendig",
    "AVAAS": "Premium — alt-inkludert",
}

_PROFILE_LABEL: dict[str, str] = {
    "standard": "Standard (100% HW signing, 50/50 services)",
    "anbud_30_30_30_10": "Anbud 30/30/30/10",
    "custom": "Tilpasset",
}


def _supplier_name() -> str:
    """Legal entity name of the operator running this deployment.

    **The single seam** through which every supplier-identity string in this
    module is resolved (summary prose, the Terms title-retention clause and
    the per-line ``ownership`` field).  Upgrading this to a per-namespace
    lookup later changes one function body, not four string literals buried
    in a document generator.

    **Fails closed.**  When ``NCE_SUPPLIER_NAME`` is unset this raises rather
    than substituting ``""`` or a plausible placeholder: the generated SoW
    carries a Norwegian title-retention clause naming a legal entity, and a
    blank or wrong party there is a defective contract, not a cosmetic bug.
    An operator who has not configured their own company name has not
    finished deploying, and the first SoW is the right place to find out.

    Raises:
        DeploymentConfigurationError: when ``NCE_SUPPLIER_NAME`` is unset or
            blank.  **D49b:** deliberately *not* a ``ValueError``.  The missing
            argument guards in ``do_generate_sow`` stay ``ValueError`` because
            those are genuine caller mistakes that 422/-32602 describe
            correctly; an unset deployment key is not one, and no argument the
            caller can send will fix it.
    """
    name = (cfg.NCE_SUPPLIER_NAME or "").strip()
    if not name:
        raise DeploymentConfigurationError(
            "NCE_SUPPLIER_NAME",
            "NCE_SUPPLIER_NAME is not configured: refusing to generate a SoW that names no supplier. "
            "Set NCE_SUPPLIER_NAME to the legal entity name of the operator running this deployment.",
        )
    return name


_ACCEPTANCE_CLAUSES: list[str] = [
    "Akseptanse skjer via gate-overgang G4_HANDOVER → G5_ACCEPTANCE "
    "etter at alle leveranser er VERIFIED.",
    "Kunde gjennomgår leveransen sammen med prosjektleder ved overlevering.",
    "Reklamasjon på utstyrsdefekter dekkes etter leverandørens garantivilkår; "
    "tjeneste-feil dekkes av eventuell ServiceContract.",
    "Endringer etter signering håndteres som Change Order (CO) med eget pristillegg.",
]


# ---------------------------------------------------------------------------
# Typed aliases — dicts with known shapes (avoids full dataclass overhead
# while staying mypy-friendly through TypedDict if needed later; for now
# plain dict[str, Any] to match the project's existing style in propose.py)
# ---------------------------------------------------------------------------

SoWInput = dict[str, Any]
SoWDoc = dict[str, Any]


# ---------------------------------------------------------------------------
# Private: formatting helper (lifted 1:1 from generator.ts)
# ---------------------------------------------------------------------------


def _format_nok(amount: float) -> str:
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M kr"
    if amount >= 1_000:
        return f"{amount / 1_000:.0f}k kr"
    return f"{amount:.0f} kr"


# ---------------------------------------------------------------------------
# Public: pure transform (0-DB)
# ---------------------------------------------------------------------------


def generate_sow(sow_input: SoWInput, version_number: int = 1) -> SoWDoc:
    """Pure, 0-DB SoW transform — lifted near-1:1 from ``lib/sow/generator.ts``.

    Maps ``SoWInput`` → ``SoWDoc``.  Deterministic for identical inputs.
    No IO, no side effects.

    Parameters
    ----------
    sow_input:
        Assembled SoW input (see ``do_generate_sow`` for the graph adapter
        that builds this dict).
    version_number:
        Monotone integer version tied to the design version (Correction #7).
        Callers must derive this from the DESIGN node version counter — not
        generate it arbitrarily — so that freeze-on-issue is enforced at the
        adapter layer.

    Returns
    -------
    SoWDoc
        Fully assembled Statement of Work document.  ``documentRef`` is
        ``"<project.id>-v<version_number>"`` for stable file-name semantics.
    """
    project = sow_input["project"]
    generated_at = datetime.now(tz=timezone.utc).isoformat()
    document_ref = f"{project['id']}-v{version_number}"

    tier_raw: int | None = project.get("tier")
    tier_label: str
    if tier_raw is not None:
        tier_label = _TIER_LABEL.get(int(tier_raw), "Ukjent")
    else:
        tier_label = "Ikke klassifisert"

    # ── §2 Deliverables — grouped per room ──────────────────────────────────
    room_map: dict[str, dict[str, str]] = {
        r["id"]: {"name": r["name"], "type": r["type"]} for r in sow_input.get("rooms", [])
    }

    lines_by_room: dict[str, list[dict[str, Any]]] = {}
    for line in sow_input.get("bomLines", []):
        key: str = line.get("roomId") or "__none__"
        lines_by_room.setdefault(key, []).append(line)

    deliverables: list[dict[str, Any]] = []
    for room_id, lines in lines_by_room.items():
        meta = room_map.get(room_id)
        room_name = (
            meta["name"] if meta else (lines[0].get("roomName") or "Felles / ikke tilordnet")
        )
        room_type = meta["type"] if meta else "unknown"
        total_sell: float = sum(float(ln["qty"]) * float(ln["sellPrice"]) for ln in lines)
        deliverables.append(
            {
                "roomName": room_name,
                "roomType": room_type,
                "lineCount": len(lines),
                "totalSell": total_sell,
                "items": [
                    {"description": ln["description"], "qty": ln["qty"], "unit": ln["category"]}
                    for ln in lines[:25]
                ],
            }
        )
    deliverables.sort(key=lambda d: d["totalSell"], reverse=True)

    # ── §4 Labor by category ────────────────────────────────────────────────
    labor_agg: dict[str, dict[str, float]] = {}
    for entry in sow_input.get("labor", []):
        hours = float(entry["externalHoursEst"]) + float(entry["internalHoursEst"])
        sell = hours * float(entry["rateCardSell"])
        cat: str = entry["category"]
        prev = labor_agg.get(cat, {"hours": 0.0, "sell": 0.0})
        labor_agg[cat] = {"hours": prev["hours"] + hours, "sell": prev["sell"] + sell}

    labor_by_category: list[dict[str, Any]] = [
        {
            "category": cat,
            "label": _CATEGORY_LABEL.get(cat, cat),
            "hoursTotal": v["hours"],
            "estimatedSell": v["sell"],
        }
        for cat, v in labor_agg.items()
    ]
    labor_by_category.sort(key=lambda lc: lc["hoursTotal"], reverse=True)
    labor_total_hours: float = sum(lc["hoursTotal"] for lc in labor_by_category)
    labor_total_sell: float = sum(lc["estimatedSell"] for lc in labor_by_category)

    # ── §5 Managed services ─────────────────────────────────────────────────
    managed_services: list[dict[str, Any]] = [
        {
            "name": c["name"],
            "tier": _SERVICE_TIER_LABEL.get(c.get("tier") or "", c.get("tier") or "Standard")
            if c.get("tier")
            else "Standard",
            "coverage": c.get("coverageLevel") or "STANDARD",
            "response": c.get("responseSpeed") or "NBD",
            "monthlyPrice": float(c.get("monthlyTotal") or 0),
            "annualValue": float(c.get("monthlyTotal") or 0) * 12,
        }
        for c in sow_input.get("serviceContracts", [])
    ]

    # ── §6 Invoicing breakdown ───────────────────────────────────────────────
    invoicing: dict[str, Any] | None = None
    sched = sow_input.get("invoiceSchedule")
    if sched:
        _non_service_cats = {"service", "design", "project_management"}
        hw_total: float = sum(
            float(ln["qty"]) * float(ln["sellPrice"])
            for ln in sow_input.get("bomLines", [])
            if ln.get("category") not in _non_service_cats
        )
        soft_total = labor_total_sell
        invoicing = {
            "profileLabel": _PROFILE_LABEL.get(sched["profile"], sched["profile"]),
            "paymentTermsDays": int(sched["paymentTermsDays"]),
            "hwBreakdown": [
                row
                for row in [
                    {
                        "trigger": "Signering",
                        "pct": sched["hwSigningPct"],
                        "amount": hw_total * sched["hwSigningPct"] / 100,
                    },
                    {
                        "trigger": "Levering",
                        "pct": sched["hwDeliveryPct"],
                        "amount": hw_total * sched["hwDeliveryPct"] / 100,
                    },
                    {
                        "trigger": "Installasjon",
                        "pct": sched["hwInstallPct"],
                        "amount": hw_total * sched["hwInstallPct"] / 100,
                    },
                    {
                        "trigger": "Overlevering",
                        "pct": sched["hwHandoverPct"],
                        "amount": hw_total * sched["hwHandoverPct"] / 100,
                    },
                ]
                if row["pct"] > 0
            ],
            "softBreakdown": [
                row
                for row in [
                    {
                        "trigger": "Signering",
                        "pct": sched["softSigningPct"],
                        "amount": soft_total * sched["softSigningPct"] / 100,
                    },
                    {
                        "trigger": "Levering",
                        "pct": sched["softDeliveryPct"],
                        "amount": soft_total * sched["softDeliveryPct"] / 100,
                    },
                    {
                        "trigger": "Installasjon",
                        "pct": sched["softInstallPct"],
                        "amount": soft_total * sched["softInstallPct"] / 100,
                    },
                    {
                        "trigger": "Overlevering",
                        "pct": sched["softHandoverPct"],
                        "amount": soft_total * sched["softHandoverPct"] / 100,
                    },
                    {
                        "trigger": "Månedlig (T&M)",
                        "pct": sched["softMonthlyPct"],
                        "amount": soft_total * sched["softMonthlyPct"] / 100,
                    },
                ]
                if row["pct"] > 0
            ],
        }

    # ── §1 Summary ───────────────────────────────────────────────────────────
    total_rooms = len(deliverables)
    total_lines = len(sow_input.get("bomLines", []))
    supplier = _supplier_name()
    summary_parts: list[str] = [
        f"{supplier} leverer {total_lines} komponenter fordelt over {total_rooms} rom "
        f"for {project['customerName']}, med en kontraktsverdi på {_format_nok(project['contractValue'])}.",
        f"Estimert arbeidsmengde er {labor_total_hours:.0f} timer over {len(labor_by_category)} kategorier.",
    ]
    comms = sow_input.get("communications")
    if comms and comms.get("count", 0) > 0:
        cnt = comms["count"]
        plural = "" if cnt == 1 else "e"
        summary_parts.append(
            f"Grunnlaget bygger på {cnt} dokumentert{plural} "
            f"interaksjon{'er' if cnt > 1 else ''} med kunde i tilbud-fasen."
        )
    if managed_services:
        n = len(managed_services)
        summary_parts.append(
            f"Etter levering går prosjektet over til managed services ({n} kontrakt{'er' if n > 1 else ''})."
        )
    else:
        summary_parts.append("Ingen managed services i denne leveransen.")
    summary = " ".join(summary_parts)

    # ── §3 Timeline ──────────────────────────────────────────────────────────
    timeline: list[dict[str, Any]] = [
        {
            "name": m["name"],
            "date": m.get("plannedDate"),
            "isMilestone": True,
            "completed": bool(m.get("completed", False)),
        }
        for m in sow_input.get("milestones", [])
        if m.get("isMilestone")
    ]

    # ── Terms ────────────────────────────────────────────────────────────────
    payment_days = sched["paymentTermsDays"] if sched else 14
    terms: list[str] = [
        "Alle priser er oppgitt eks. mva. (25 %) i NOK.",
        f"Betalingsbetingelser: {payment_days} dager netto fra fakturadato.",
        "Forsinkelsesrente etter forsinkelsesrentelovens sats.",
        f"{supplier} beholder eierskap til leveranser inntil full betaling er mottatt.",
        "Tvister søkes løst i minnelighet; verneting er Oslo tingrett.",
    ]

    # ── Captured intelligence ────────────────────────────────────────────────
    captured_intelligence: dict[str, Any] | None = None
    if comms and comms.get("count", 0) > 0:
        captured_intelligence = {
            "interactionCount": comms["count"],
            "decisions": comms.get("decisions", []),
            "actions": comms.get("actions", []),
            "products": comms.get("products", []),
            "questions": comms.get("questions", []),
        }

    return {
        "documentRef": document_ref,
        "generatedAt": generated_at,
        "versionNumber": version_number,
        "project": {
            "id": project["id"],
            "name": project["name"],
            "customer": project["customerName"],
            "contractValue": float(project["contractValue"]),
            "startDate": project["startDate"],
            "endDate": project["endDate"],
            "pm": project.get("pm") or "Tildeles ved oppstart",
            "tierLabel": tier_label,
        },
        "summary": summary,
        "deliverables": deliverables,
        "timeline": timeline,
        "laborByCategory": labor_by_category,
        "laborTotalHours": labor_total_hours,
        "laborTotalSell": labor_total_sell,
        "managedServices": managed_services,
        "invoicing": invoicing,
        "acceptance": _ACCEPTANCE_CLAUSES,
        "capturedIntelligence": captured_intelligence,
        "terms": terms,
    }


# ---------------------------------------------------------------------------
# Private: graph read helpers
# ---------------------------------------------------------------------------


async def _read_design_meta(
    conn: Any,
    ns_uuid: UUID,
    design_label: str,
) -> dict[str, Any]:
    """Read design-level metadata stored in the DESIGN node label.

    kg_nodes has no metadata/payload/state column.  All design facts are
    encoded in the label or derived from edges.  We return the label itself
    and derive a version counter from a deterministic hash so the freeze
    semantics are reproducible.
    """
    row = await conn.fetchrow(
        """
        SELECT label, updated_at
        FROM kg_nodes
        WHERE label        = $1
          AND entity_type  = 'DESIGN'
          AND namespace_id = $2::uuid
        """,
        design_label,
        str(ns_uuid),
    )
    return dict(row) if row else {}


async def _read_design_lines(
    conn: Any,
    ns_uuid: UUID,
    design_label: str,
) -> list[dict[str, Any]]:
    """Return all DESIGN_LINE nodes reachable via DESIGN -[contains]-> DESIGN_LINE."""
    rows = await conn.fetch(
        """
        SELECT n.label
        FROM kg_nodes n
        JOIN kg_edges e
             ON e.object_label  = n.label
            AND e.namespace_id  = n.namespace_id
        WHERE e.subject_label  = $1
          AND e.predicate       = 'contains'
          AND n.entity_type     = 'DESIGN_LINE'
          AND n.namespace_id    = $2::uuid
        """,
        design_label,
        str(ns_uuid),
    )
    return [dict(r) for r in rows]


async def _read_fl_nodes_for_design(
    conn: Any,
    ns_uuid: UUID,
    design_label: str,
) -> list[dict[str, Any]]:
    """Return all FUNCTIONAL_LOCATION nodes reachable from the DESIGN node.

    Traversal: DESIGN -[contains]-> FUNCTIONAL_LOCATION (root), then
    FUNCTIONAL_LOCATION -[parent_of]-> children.  We pull all FLs in one
    query using the DESIGN -[contains]-> FL root edge, then recursively
    parent_of edges.
    """
    rows = await conn.fetch(
        """
        WITH RECURSIVE fl_tree AS (
            -- Seed: FUNCTIONAL_LOCATIONs the design directly contains
            SELECT n.label, n.entity_type
            FROM kg_nodes n
            JOIN kg_edges e
                 ON e.object_label  = n.label
                AND e.namespace_id  = n.namespace_id
            WHERE e.subject_label  = $1
              AND e.predicate       = 'contains'
              AND n.entity_type     = 'FUNCTIONAL_LOCATION'
              AND n.namespace_id    = $2::uuid

            UNION ALL

            -- Recurse: parent_of children
            SELECT child.label, child.entity_type
            FROM kg_nodes child
            JOIN kg_edges ep
                 ON ep.object_label  = child.label
                AND ep.namespace_id  = child.namespace_id
            JOIN fl_tree parent
                 ON parent.label     = ep.subject_label
            WHERE ep.predicate       = 'parent_of'
              AND child.namespace_id = $2::uuid
        )
        SELECT label FROM fl_tree
        """,
        design_label,
        str(ns_uuid),
    )
    return [dict(r) for r in rows]


async def _read_fl_design_lines(
    conn: Any,
    ns_uuid: UUID,
    fl_label: str,
) -> list[dict[str, Any]]:
    """Return all DESIGN_LINE labels that a FUNCTIONAL_LOCATION needs."""
    rows = await conn.fetch(
        """
        SELECT n.label
        FROM kg_nodes n
        JOIN kg_edges e
             ON e.object_label  = n.label
            AND e.namespace_id  = n.namespace_id
        WHERE e.subject_label  = $1
          AND e.predicate       = 'needs'
          AND n.entity_type     = 'DESIGN_LINE'
          AND n.namespace_id    = $2::uuid
        """,
        fl_label,
        str(ns_uuid),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Private: SoWInput assembly from graph data
# ---------------------------------------------------------------------------


def _parse_design_line_label(dl_label: str) -> dict[str, str]:
    """Extract line_ref from a DESIGN_LINE label.

    Label format: ``DESIGN_LINE:<DESIGN_ID>:<LINE_REF>``
    """
    parts = dl_label.split(":", 2)
    return {"line_ref": parts[2] if len(parts) >= 3 else dl_label}


def _parse_fl_label(fl_label: str) -> dict[str, str]:
    """Extract the last path component from a FUNCTIONAL_LOCATION label.

    Label format: ``FL:<NAMESPACE_SLUG>:<SITE>:<BUILDING>:<FLOOR>:<ROOM>...``
    The leaf name is always the last ``:`-separated component.
    """
    parts = fl_label.split(":")
    return {"name": parts[-1] if parts else fl_label}


def _derive_version_number(design_label: str, design_meta: dict[str, Any]) -> int:
    """Derive a deterministic version number from the design identity + updated_at.

    The version number is a positive integer derived from a SHA-256 hash of
    ``<design_label>|<updated_at>``.  This makes it:
    - Stable: same design state → same version number.
    - Monotone in the sense that a DB-level update to ``updated_at`` yields a
      different version, which is exactly the signal we need for freeze-on-issue.
    - Small: we take ``abs(hash_int) % 100_000`` so it stays human-readable.

    Callers use this as the SoW ``versionNumber``.  Freeze-on-issue is
    enforced by the adapter returning the same doc for the same version.
    """
    updated_at_str = str(design_meta.get("updated_at", ""))
    raw = f"{design_label}|{updated_at_str}".encode()
    digest = hashlib.sha256(raw).digest()
    n = int.from_bytes(digest[:4], "big")
    return (n % 100_000) + 1  # 1–100000, never 0


def _assemble_sow_input(
    design_label: str,
    design_meta: dict[str, Any],
    design_lines: list[dict[str, Any]],
    fl_nodes: list[dict[str, Any]],
    fl_to_lines: dict[str, list[dict[str, Any]]],
) -> SoWInput:
    """Build a SoWInput from the raw graph data.

    The TS ``SoWInput`` has 7 top-level fields.  This adapter populates the
    fields that system_design owns:
      - ``project`` — derived from the DESIGN label + design_meta.
      - ``bomLines`` — one entry per DESIGN_LINE node, room-assigned where
        possible via the FUNCTIONAL_LOCATION -[needs]-> DESIGN_LINE edges.
      - ``rooms`` — one entry per ROOM-level FUNCTIONAL_LOCATION node.

    Fields that belong to other engines (Project, Labor, Services, Invoicing,
    Communications) are left as empty lists / None — they are filled by the
    caller (or left empty for a design-phase SoW that has not yet been
    combined with a full project record).
    """
    # Project block — derive name/id from DESIGN label.
    # Label format: DESIGN:<DESIGN_ID>
    parts = design_label.split(":", 1)
    design_id = parts[1] if len(parts) == 2 else design_label

    project: dict[str, Any] = {
        "id": design_id,
        "name": design_id,
        "customerId": None,
        "customerName": design_id,
        "contractValue": 0.0,
        "startDate": "",
        "endDate": "",
        "pm": "",
        "tier": None,
    }

    # ROOM-level FLs.  A ROOM is a FUNCTIONAL_LOCATION with depth >= 4
    # (FL:<NS>:<SITE>:<BUILDING>:<FLOOR>:<ROOM>).  We identify rooms as any
    # FL whose label has exactly 6 colon-separated parts.
    rooms: list[dict[str, Any]] = []
    for fl in fl_nodes:
        lbl: str = fl["label"]
        if lbl.count(":") == 5:  # FL:NS:SITE:BUILDING:FLOOR:ROOM
            rooms.append({"id": lbl, "name": _parse_fl_label(lbl)["name"], "type": "room"})

    # Build a reverse index: DESIGN_LINE label → room FL label.
    dl_to_room: dict[str, str] = {}
    for fl_lbl, dl_list in fl_to_lines.items():
        for dl in dl_list:
            dl_to_room[dl["label"]] = fl_lbl

    # BOM lines — one entry per DESIGN_LINE.
    bom_lines: list[dict[str, Any]] = []
    supplier = _supplier_name()
    for dl in design_lines:
        dl_lbl: str = dl["label"]
        parsed = _parse_design_line_label(dl_lbl)
        room_fl = dl_to_room.get(dl_lbl)
        bom_lines.append(
            {
                "id": dl_lbl,
                "category": "equipment",
                "description": parsed["line_ref"],
                "qty": 1,
                "sellPrice": 0.0,
                "roomId": room_fl,
                "roomName": _parse_fl_label(room_fl)["name"] if room_fl else None,
                "ownership": supplier,
            }
        )

    return {
        "project": project,
        "bomLines": bom_lines,
        "rooms": rooms,
        "labor": [],
        "milestones": [],
        "serviceContracts": [],
        "invoiceSchedule": None,
        "communications": None,
    }


# ---------------------------------------------------------------------------
# Public: async adapter (own session, reads graph, calls pure transform)
# ---------------------------------------------------------------------------


async def do_generate_sow(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a Statement of Work from the design subgraph and return it.

    Reads the DESIGN / DESIGN_LINE / FUNCTIONAL_LOCATION subgraph written
    by Wave 2 (``do_author_functional_location``), assembles per-room
    ``SoWInput``, and calls the 0-DB pure ``generate_sow`` transform.

    Freeze-on-issue:  the ``version_number`` is derived deterministically
    from the DESIGN node's ``updated_at`` timestamp.  Re-issuing the same
    design version returns the same ``versionNumber`` — any mutating wave
    (e.g. ``do_validate_design``) touches ``updated_at``, which changes the
    hash, which issues a new version.

    Parameters
    ----------
    engine:
        NCEEngine instance.  Must have a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "design_id": str,             # required — the DESIGN node id
            "version_number": int,        # optional — override frozen version
        }``

    Returns
    -------
    dict
        ``{
            "sow": SoWDoc,          # the assembled Statement of Work
            "version_number": int,  # the version tied to the design state
            "frozen": bool,         # True when caller supplied version_number
        }``
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_generate_sow: 'namespace_id' is required in params")
    # D35 fail-closed: resolve the operator identity BEFORE any DB read, so an
    # unconfigured deployment is told what is missing instead of producing a
    # contract that names no party. D49b: raises DeploymentConfigurationError
    # naming NCE_SUPPLIER_NAME — an operator fault, not a caller fault.
    _supplier_name()
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    design_id_raw: str = params.get("design_id", "")
    if not design_id_raw:
        raise ValueError("do_generate_sow: 'design_id' is required in params")
    design_label = f"DESIGN:{design_id_raw.upper()}"

    caller_version: int | None = params.get("version_number")
    frozen = caller_version is not None

    # All DB reads in one scoped session — no slow IO inside.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        design_meta = await _read_design_meta(conn, ns_uuid, design_label)
        design_lines = await _read_design_lines(conn, ns_uuid, design_label)
        fl_nodes = await _read_fl_nodes_for_design(conn, ns_uuid, design_label)

        fl_to_lines: dict[str, list[dict[str, Any]]] = {}
        for fl in fl_nodes:
            fl_lbl: str = fl["label"]
            fl_to_lines[fl_lbl] = await _read_fl_design_lines(conn, ns_uuid, fl_lbl)

    if not design_meta:
        raise ValueError(
            f"do_generate_sow: DESIGN node not found for design_id={design_id_raw!r} "
            f"in namespace={ns_uuid}"
        )

    # Derive version tied to design state (freeze-on-issue).
    version_number: int
    if frozen:
        assert caller_version is not None  # guaranteed by `frozen = caller_version is not None`
        version_number = caller_version
    else:
        version_number = _derive_version_number(design_label, design_meta)

    # Assemble SoWInput from graph data (pure, no IO).
    sow_input = _assemble_sow_input(
        design_label=design_label,
        design_meta=design_meta,
        design_lines=design_lines,
        fl_nodes=fl_nodes,
        fl_to_lines=fl_to_lines,
    )

    # Call the pure transform (0-DB).
    sow_doc = generate_sow(sow_input, version_number=version_number)

    log.info(
        "do_generate_sow: ns=%s design=%s version=%d rooms=%d lines=%d frozen=%s",
        ns_uuid,
        design_id_raw,
        version_number,
        len(sow_input.get("rooms", [])),
        len(sow_input.get("bomLines", [])),
        frozen,
    )

    return {
        "sow": sow_doc,
        "version_number": version_number,
        "frozen": frozen,
    }
