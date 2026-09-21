"""
nce/vertical_modules/legal_entities/brreg_feed.py
=====================================================
National business registry feed for C15 Legal-Entity Register (MLV16
charter, Lane F, Wave F-8): on-demand, single-`org_nr` enrichment from
Norway's Brønnøysundregistrene (BRREG) Enhetsregisteret — public, free,
no authentication.

Read for shape only (Q-25): the host's own BRREG integration. This
module's schema, functions and tests are its own, not a port of the
host's.

Two decisions made and documented rather than guessed past
--------------------------------------------------------------
1. The host's OWN pattern for this data is a nightly bulk mirror of the
   whole national register (~1.1M units + ~2.0M sub-units via
   ``.../lastned``, streamed gzip parsed with ``ijson``, COPY-batched into
   a staging table, atomic swap) — the same class of decision as Wave
   F-11's declined ``.osm.pbf``/``osmium`` parser: a new heavyweight
   dependency and a background-sync architecture, bigger than one wave
   should decide unilaterally. This module does the OPPOSITE shape
   instead: a single on-demand lookup per ``org_nr``, the same
   "vendor telemetry adapter" pattern as Waves F-1..F-7 (given an
   existing record's identity, pull its current enrichment fields) —
   never a bulk download.

2. "A field's name is not its meaning" (estate-wide rule, filed after
   Wave F-6's Q-SYS Reflect `serial`/`serialNumber` trap). NCE's
   ``legal_entities.roles`` (see ``models.py``) is a small tenant-chosen
   relationship-tag set (e.g. ``"customer"``, ``"vendor"`` — a caller-chosen
   label, not a BRREG concept). BRREG's own
   "roller" means something entirely different: government-registered
   PERSONAL signatory roles (``DAGL``/daglig leder, ``LEDE``/``NEST``/
   ``MEDL`` board chair/deputy/member, ``INNH`` sole-proprietor owner) —
   sourced from a feed that also carries each role-holder's name and
   birthdate, which the host's own code is emphatic about never storing
   (only a salted hash). This module never reads or writes
   ``legal_entities.roles`` and never touches any person-level BRREG
   data at all; enrichment is limited to company-level fields with no
   such ambiguity, written into ``legal_entities.metadata`` (already a
   free-form jsonb column) under a ``brreg_``-prefixed key set so this
   feed's writes never collide with another source's.

Attribution: BRREG's Enhetsregisteret data is public sector information
under the Norwegian Licence for Open Government Data (NLOD); nothing
about it is the host's proprietary work.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.normalizers import normalize_org_nr
from nce.vertical_modules.legal_entities.service import update_legal_entity

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.legal_entities.brreg_feed")

_BASE_PATH = "/enhetsregisteret/api/enheter"
_BASE_URL = f"https://data.brreg.no{_BASE_PATH}"
_ORGNR = re.compile(r"^\d{9}$")

# The allow-list literal (charter §0 rule 3): one GET, one path template.
# No credentials exist for this gate to protect (BRREG's per-unit lookup is
# public), so its job is narrower and just as real: it is what stops this
# module from silently growing into the bulk `.../lastned` or `.../roller`
# endpoints declined above without a code change AND a new test.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", f"{_BASE_PATH}/{{org_nr}}")})


class BrregAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this feed's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(f"BRREG feed refused {method} {gate_path}: not in its read allow-list")
        self.method = method
        self.path = gate_path


async def _gated_request(client: httpx.AsyncClient, method: str, org_nr: str) -> httpx.Response:
    gate_path = f"{_BASE_PATH}/{{org_nr}}"
    if (method, gate_path) not in _ALLOWED_READS:
        raise BrregAllowListRefusal(method, gate_path)
    if not _ORGNR.match(org_nr):
        raise ValueError(
            f"brreg_feed: org_nr {org_nr!r} is not a valid 9-digit Norwegian organisation number"
        )
    return await client.request(method, f"{_BASE_URL}/{org_nr}")


def _fields_from_enhet(enhet: dict[str, Any]) -> dict[str, Any]:
    """Company-level enrichment fields only — never a role, a person's name,
    or a birthdate. See the module docstring's decision (2)."""
    fa = enhet.get("forretningsadresse") or {}
    adr = fa.get("adresse")
    if isinstance(adr, list):
        adresse = ", ".join(a for a in adr if a) or None
    else:
        adresse = (str(adr).strip() or None) if adr else None
    nace1 = enhet.get("naeringskode1") or {}

    fields: dict[str, Any] = {
        "brreg_navn": (enhet.get("navn") or "").strip() or None,
        "brreg_naeringskode": nace1.get("kode"),
        "brreg_naeringsbeskrivelse": nace1.get("beskrivelse"),
        "brreg_organisasjonsform": (enhet.get("organisasjonsform") or {}).get("kode"),
        "brreg_adresse": adresse,
        "brreg_postnummer": fa.get("postnummer"),
        "brreg_poststed": fa.get("poststed"),
        "brreg_kommunenummer": fa.get("kommunenummer"),
        "brreg_landkode": fa.get("landkode") or "NO",
        "brreg_slettedato": enhet.get("slettedato"),
        "brreg_konkurs": bool(enhet.get("konkurs")),
        "brreg_under_avvikling": bool(enhet.get("underAvvikling")),
        "brreg_synced_at": datetime.now(timezone.utc).isoformat(),
    }
    return {k: v for k, v in fields.items() if v is not None}


async def _require_legal_entity_org_nr(engine: NCEEngine, ns_uuid: UUID, entity_id: UUID) -> str:
    """Refuse an entity that is not visible (or is archived) in the
    caller's namespace. Explicit predicate, not left to RLS alone — same
    reasoning as ``assets.telemetry._require_asset_in_namespace``."""
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            "SELECT org_nr FROM legal_entities "
            "WHERE id = $1::uuid AND namespace_id = $2::uuid AND archived = FALSE",
            str(entity_id),
            str(ns_uuid),
        )
    if row is None:
        raise ValueError(
            f"do_enrich_legal_entity_from_registry: entity {entity_id} is not an active "
            f"legal entity in namespace {ns_uuid}"
        )
    return row["org_nr"]


async def do_enrich_legal_entity_from_registry(
    engine: NCEEngine, params: dict[str, Any], *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Look up one legal entity's company data in BRREG's Enhetsregisteret
    and merge it into ``legal_entities.metadata``.

    Parameters
    ----------
    params:
        ``{"namespace_id": str | UUID, "entity_id": str | UUID}`` — both
        required. The entity's ``org_nr`` is read from its own row, never
        supplied by the caller, so a caller cannot enrich one entity with
        another's registry data.

    Fail-soft on a genuine "not in the registry" (mirrors the host's own
    ``lookup_nace``: a 404/unexpected response means the entity simply
    isn't found there, not an error) — the caller gets
    ``{"matched": False, "reason": ...}`` and the row's ``metadata`` is
    left untouched. Refuses (raises) before any HTTP call for a namespace
    mismatch or a malformed stored ``org_nr``; those are internal data
    problems, not registry-lookup outcomes.

    ``transport`` is a test-only escape hatch (mirrors the host's own
    ``lookup_nace(org_nr, client=None)`` shape): ``None`` in production
    uses httpx's real transport; tests inject an ``httpx.MockTransport``
    so no test reaches the network.

    Returns
    -------
    dict
        ``{"ok": True, "entity_id": str, "org_nr": str, "matched": bool,
        "reason": str | None, "fields_written": list[str]}``.
    """
    ns_uuid = UUID(str(params.get("namespace_id") or "")) if params.get("namespace_id") else None
    entity_id = UUID(str(params.get("entity_id") or "")) if params.get("entity_id") else None
    if ns_uuid is None:
        raise ValueError("do_enrich_legal_entity_from_registry: 'namespace_id' is required")
    if entity_id is None:
        raise ValueError("do_enrich_legal_entity_from_registry: 'entity_id' is required")

    org_nr = normalize_org_nr(await _require_legal_entity_org_nr(engine, ns_uuid, entity_id))
    if not org_nr:
        raise ValueError(
            f"do_enrich_legal_entity_from_registry: entity {entity_id} has no usable org_nr "
            "to look up"
        )

    # Outside any transaction on purpose (same reasoning as assets.telemetry):
    # a real HTTP call held inside scoped_pg_session would bloat locks.
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        try:
            resp = await _gated_request(client, "GET", org_nr)
        except httpx.HTTPError as exc:
            log.warning(
                "do_enrich_legal_entity_from_registry: BRREG request failed for org_nr %s: %s",
                org_nr,
                exc,
            )
            return {
                "ok": True,
                "entity_id": str(entity_id),
                "org_nr": org_nr,
                "matched": False,
                "reason": "registry_unreachable",
                "fields_written": [],
            }

    if resp.status_code != 200:
        return {
            "ok": True,
            "entity_id": str(entity_id),
            "org_nr": org_nr,
            "matched": False,
            "reason": "not_found_in_registry",
            "fields_written": [],
        }

    fields = _fields_from_enhet(resp.json())

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await update_legal_entity(conn, ns_uuid, entity_id, metadata=fields)

    log.info(
        "do_enrich_legal_entity_from_registry entity=%s org_nr=%s fields=%d",
        entity_id,
        org_nr,
        len(fields),
    )
    return {
        "ok": True,
        "entity_id": str(entity_id),
        "org_nr": org_nr,
        "matched": True,
        "reason": None,
        "fields_written": sorted(fields.keys()),
    }
