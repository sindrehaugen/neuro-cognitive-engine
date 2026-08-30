"""Assets Engine vertical module (Module 9).

Owns the business lifecycle of every installed device — see
``docs/vertical_engines/09-assets-engine.md``. This wave (Batch 141,
Module 9.Wave 1, ``lifecycle-core``) ships only the pure 14-state
lifecycle transform (:mod:`nce.vertical_modules.assets.lifecycle`) and a
liveness-probe MCP tool (``assets_ping``) that opens the module's slot in
``TOOL_REGISTRY``.

What this package holds, as of Batch 142b:

* ``lifecycle.py`` (B141) — the pure 14-state machine. No DB, no HTTP, no graph.
* ``seed.py`` (B142) — the ``assets`` table's writer. Relational ONLY: it
  writes no ``kg_nodes`` and no ``kg_edges`` and calls no ``assert_owner``,
  and says so in its own docstring.
* ``graph.py`` (B142b) — the graph projection, and the ONLY module here that
  writes ``kg_nodes``/``kg_edges``. **The guard is asymmetric, deliberately.**
  Every ``kg_nodes`` write goes through ``assert_owner`` (one call site,
  ``_upsert_asset_node``). The ``kg_edges`` writes do NOT, and must not: an
  edge names cross-engine endpoints (``BOM_LINE``, ``FUNCTIONAL_LOCATION``)
  that this engine does not own, ``kg_edges`` has no FK to ``kg_nodes``, and
  the same rule is already stated by ``economy/graph.py::_upsert_edge`` and
  ``procurement/graph.py::upsert_offers_edge``. See ``_upsert_edge``'s own
  docstring.

  An earlier revision of this paragraph claimed every graph write goes
  through ``assert_owner``. That was false, and it is corrected here rather
  than left standing: a later wave adding a second edge-writing helper could
  read it, conclude the guard already covers every graph write in the
  package, and never notice the node/edge asymmetry — which is otherwise
  documented only inside ``graph.py``.

``ASSET`` **is** registered in ``nce/config_data/node-ownership.json``
(``owner_engine="assets"``, ``transition=null``) as of Batch 142b. This
paragraph previously said the opposite — true when B141 wrote it, made stale
first by B142 shipping the table and then by B142b registering the row.
Corrected here rather than left to mislead: a reader taking the old text at
face value would conclude that ``graph.py`` cannot work, when in fact the
guard permits exactly its writes and refuses everyone else's.

``FUNCTIONAL_LOCATION`` remains ``system_design``'s and ``BOM_LINE`` is
unregistered (B132a/B133b) — ``graph.py`` writes EDGES to those, never the
nodes.
"""

from __future__ import annotations
