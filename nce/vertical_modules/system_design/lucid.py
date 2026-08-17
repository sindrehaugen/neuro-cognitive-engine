"""
nce/vertical_modules/system_design/lucid.py
===========================================
Lucid diagram export adapter for the System Design vertical module
(Wave 11, Phase 1b — EXPORT ONLY).

**Spec correction — Lucid import is CUT.** This module implements export only.
There is no import path.  If asked to add an import function, STOP and report.

Public entry-point:
  ``do_publish_design_docs(engine, {namespace_id, design_id}) -> {lucid_url}``
      Read the DESIGN + DESIGN_LINE + FUNCTIONAL_LOCATION nodes for the given
      design (W2 graph), map them to a Lucid diagram payload, POST via httpx
      (30 s, request_with_retry), auth from ``NCE_SYSTEM_DESIGN_LUCID_*``.
      Returns ``{"lucid_url": str}`` on success.
      Returns ``{"lucid_url": None}`` (clean no-op) when credentials are unset.
      Never raises on missing creds; never logs credential values.

Credentials (env-only, resolved at call time via ``resolve_secret``):
    NCE_SYSTEM_DESIGN_LUCID_API_KEY   — Lucid API key (Bearer token)
    NCE_SYSTEM_DESIGN_LUCID_BASE_URL  — Lucid API base URL
                                         (defaults to _LUCID_DEFAULT_BASE_URL)

HTTP:
    httpx.AsyncClient with 30 s timeout, routed through
    ``nce.http_resilience.request_with_retry`` for exponential back-off on
    transient errors.

uncle-bob design notes:
    - This is an edge adapter: it depends inward (domain nodes → HTTP edge).
    - Domain code (graph.py, sow.py) has zero dependency on this module.
    - ``_creds()``, ``_read_design_nodes()``, and ``_build_lucid_payload()``
      are three focused functions — SRP, single level of abstraction each.
    - No import path — export only (spec correction, Wave 11).
    - Introduce abstraction only on the third duplication.
"""

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Any,
)

import httpx

from nce.config import resolve_secret
from nce.db_utils import scoped_pg_session
from nce.http_resilience import request_with_retry

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.system_design.lucid")

_LUCID_DEFAULT_BASE_URL = "https://api.lucid.co"
_LUCID_DIAGRAMS_PATH = "/diagrams"
_TIMEOUT_S = 30.0

# Node entity types authored by W2.
_TYPE_DESIGN = "DESIGN"
_TYPE_DESIGN_LINE = "DESIGN_LINE"
_TYPE_FL = "FUNCTIONAL_LOCATION"


# ---------------------------------------------------------------------------
# Private: credential resolution
# ---------------------------------------------------------------------------


def _creds() -> tuple[str, str] | None:
    """Return (api_key, base_url) or None when unconfigured.

    Credentials are resolved at call time so ``monkeypatch.setenv`` in tests
    takes effect without reloading the module.  Values are never logged.
    """
    api_key = resolve_secret("NCE_SYSTEM_DESIGN_LUCID_API_KEY")
    if not api_key:
        return None  # Phase 1b not configured — clean no-op
    base_url = resolve_secret("NCE_SYSTEM_DESIGN_LUCID_BASE_URL") or _LUCID_DEFAULT_BASE_URL
    return api_key, base_url


# ---------------------------------------------------------------------------
# Private: read design nodes from the knowledge graph
# ---------------------------------------------------------------------------


async def _read_design_nodes(
    engine: NCEEngine,
    namespace_id: str,
    design_id: str,
) -> dict[str, Any]:
    """Read DESIGN, DESIGN_LINE, and FUNCTIONAL_LOCATION nodes for *design_id*.

    Queries are namespace-scoped via ``scoped_pg_session``.

    Returns a dict with keys:
      ``design_label`` (str | None), ``design_lines`` (list[str]),
      ``functional_locations`` (list[str]).
    """
    design_label_prefix = f"DESIGN:{design_id.upper()}"
    design_line_prefix = f"DESIGN_LINE:{design_id.upper()}:"

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        # DESIGN node
        design_row = await conn.fetchrow(
            """
            SELECT label FROM kg_nodes
            WHERE namespace_id = $1::uuid
              AND entity_type   = $2
              AND label         = $3
            LIMIT 1
            """,
            namespace_id,
            _TYPE_DESIGN,
            design_label_prefix,
        )

        # DESIGN_LINE nodes for this design
        dl_rows = await conn.fetch(
            """
            SELECT label FROM kg_nodes
            WHERE namespace_id = $1::uuid
              AND entity_type   = $2
              AND label LIKE $3
            ORDER BY label
            """,
            namespace_id,
            _TYPE_DESIGN_LINE,
            f"{design_line_prefix}%",
        )

        # FUNCTIONAL_LOCATION nodes linked to this design via 'contains' edges
        fl_rows = await conn.fetch(
            """
            SELECT n.label FROM kg_nodes n
            JOIN kg_edges e
              ON e.object_label  = n.label
             AND e.namespace_id  = n.namespace_id
            WHERE n.namespace_id = $1::uuid
              AND n.entity_type  = $2
              AND e.subject_label = $3
              AND e.predicate     = 'contains'
            ORDER BY n.label
            """,
            namespace_id,
            _TYPE_FL,
            design_label_prefix,
        )

    return {
        "design_label": design_row["label"] if design_row else None,
        "design_lines": [r["label"] for r in dl_rows],
        "functional_locations": [r["label"] for r in fl_rows],
    }


# ---------------------------------------------------------------------------
# Private: map design nodes to a Lucid diagram payload
# ---------------------------------------------------------------------------


def _build_lucid_payload(
    design_id: str,
    nodes: dict[str, Any],
) -> dict[str, Any]:
    """Map *nodes* (from ``_read_design_nodes``) to a Lucid diagram request body.

    The payload follows the Lucid REST API v1 diagram-create format:
      - One "container" page named after the design.
      - One shape per DESIGN_LINE (representing a line item / product intent).
      - One shape per FUNCTIONAL_LOCATION (representing a site/room node).

    This is a focused one-way transform (export only — no import path).
    """
    items: list[dict[str, Any]] = []

    for dl_label in nodes["design_lines"]:
        items.append(
            {
                "type": "shape",
                "shapeType": "rectangle",
                "text": dl_label,
                "category": "DESIGN_LINE",
            }
        )

    for fl_label in nodes["functional_locations"]:
        items.append(
            {
                "type": "shape",
                "shapeType": "ellipse",
                "text": fl_label,
                "category": "FUNCTIONAL_LOCATION",
            }
        )

    return {
        "title": f"Design: {design_id}",
        "pageTitle": f"Design export — {design_id}",
        "items": items,
        "source": "NCE system_design",
    }


# ---------------------------------------------------------------------------
# Public: do_publish_design_docs
# ---------------------------------------------------------------------------


async def do_publish_design_docs(
    engine: NCEEngine,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Export a DESIGN and its DESIGN_LINE/FUNCTIONAL_LOCATION tree to Lucid.

    **EXPORT ONLY** — there is no import path (spec correction, Wave 11).

    Parameters
    ----------
    engine:
        Active NCEEngine (provides pg_pool).
    arguments:
        Must contain ``namespace_id`` (str UUID) and ``design_id`` (str).

    Returns
    -------
    dict
        ``{"lucid_url": str}`` on success.
        ``{"lucid_url": None}`` when credentials are unset (clean no-op).

    Never raises on missing credentials.  Credentials are never logged.
    """
    namespace_id: str = arguments["namespace_id"]
    design_id: str = arguments["design_id"]

    creds = _creds()
    if creds is None:
        log.debug(
            "do_publish_design_docs: Lucid credentials unset — no-op (namespace=%s)", namespace_id
        )
        return {"lucid_url": None}

    api_key, base_url = creds  # credentials never logged

    nodes = await _read_design_nodes(engine, namespace_id, design_id)
    payload = _build_lucid_payload(design_id, nodes)

    url = f"{base_url.rstrip('/')}{_LUCID_DIAGRAMS_PATH}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await request_with_retry(
            client,
            "POST",
            url,
            operation_name="lucid_publish_design_docs",
            json=payload,
            headers=headers,
        )

    body = resp.json()
    lucid_url: str = body.get("editUrl") or body.get("url") or body.get("diagramUrl", "")
    log.info(
        "do_publish_design_docs: published design=%s ns=%s lucid_url=%s",
        design_id,
        namespace_id,
        lucid_url,
    )
    return {"lucid_url": lucid_url}
