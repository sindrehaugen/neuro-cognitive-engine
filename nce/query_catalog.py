"""
Query Catalog: intent-based template matching, safe Jinja2 execution, and
write-time graph schema registry.

Phase 1 — ``CatalogManager.suggest()`` / ``execute()``:
  ``suggest()`` embeds the LLM's natural-language intent and does an ANN cosine
  lookup against ``query_templates.description_embedding``.  Returns ranked slugs
  with confidence scores so the agent can pick without constructing raw DB params.

  ``execute()`` fetches the chosen template, validates caller-supplied slots
  against its JSON Schema, **strips any caller-supplied ``namespace_id``**,
  then compiles the ``raw_template`` string via a bind-tracking Jinja2 macro
  that emits asyncpg positional parameters (``$1``, ``$2``, …).

Phase 3 — ``CatalogManager.describe_schema()`` / ``record_schema()``:
  ``describe_schema()`` queries ``graph_schema_registry`` — an O(1) PK index
  lookup that replaces expensive ``SELECT DISTINCT`` scans.

  ``record_schema()`` is a static method that must be called inside an open
  Saga transaction (uses the same ``asyncpg.Connection``).  It upserts
  vocabulary-level type keys (entity_type for nodes, predicate for edges)
  at zero extra round-trip cost.

Template ``raw_template`` authoring contract::

  {{ bind('param_name') }}   → emits $N and appends value to the bind list
  {% if param_name %} … {% endif %}  → safe structural branching on optionals
  {{ param_name }}           → FORBIDDEN for data values; StrictUndefined raises.
                               All value placements must use ``{{ bind('x') }}``.

Connection pattern:
  All queries use ``scoped_pg_session`` — RLS returns both global (NULL
  ``namespace_id``) templates and this tenant's custom templates.
  ``record_schema`` takes an existing open connection from the Saga coordinator.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg
import jsonschema
from jinja2 import Environment, StrictUndefined

from nce.db_utils import scoped_pg_session
from nce.embeddings import embed

log = logging.getLogger(__name__)

# Module-level Jinja2 environment shared across all CatalogManager instances.
# Creating a new Environment per request throws away Jinja2's internal template
# cache, forcing re-parsing of raw_template strings on every call.
_JINJA_ENV: Environment = Environment(undefined=StrictUndefined)  # nosec B701 — SQL templating only; data values emitted as asyncpg positional params via bind()->${N}; raw {{ value }} is FORBIDDEN (StrictUndefined raises); no HTML/browser sink


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateSuggestion:
    slug: str
    description: str
    tools: list[str]
    param_schema: dict[str, Any]
    confidence: float


@dataclass(frozen=True)
class GraphSchema:
    entity_types: list[str]  # e.g. ["Person", "Organization", "CONCEPT"]
    edge_predicates: list[str]  # e.g. ["AUTHORED", "ATTENDED", "REFERENCES"]
    sampled_at: str  # ISO 8601 UTC timestamp


# ---------------------------------------------------------------------------
# CatalogManager
# ---------------------------------------------------------------------------


@dataclass
class CatalogManager:
    pool: asyncpg.Pool

    # Points at the module-level singleton — stateless, safe across async requests.
    _jinja: Environment = field(
        default_factory=lambda: _JINJA_ENV,
        repr=False,
        compare=False,
    )

    # ── Phase 1: suggest ────────────────────────────────────────────────────

    async def suggest(
        self,
        intent: str,
        namespace_id: uuid.UUID,
        limit: int = 5,
    ) -> list[TemplateSuggestion]:
        """Return ranked query templates matching the LLM's natural-language intent.

        Uses ANN cosine lookup on ``description_embedding``.  RLS policy returns
        both global seeds (``namespace_id IS NULL``) and this tenant's custom
        templates in a single query.
        """
        embedding = await embed(intent)

        async with scoped_pg_session(self.pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT slug, description, tools, param_schema,
                       1 - (description_embedding <=> $1::vector) AS confidence
                FROM query_templates
                WHERE is_active = TRUE
                  AND description_embedding IS NOT NULL
                ORDER BY description_embedding <=> $1::vector
                LIMIT $2
                """,
                json.dumps(embedding),
                limit,
            )

        return [
            TemplateSuggestion(
                slug=r["slug"],
                description=r["description"],
                tools=list(r["tools"]),
                param_schema=(
                    r["param_schema"]
                    if isinstance(r["param_schema"], dict)
                    else json.loads(r["param_schema"])
                ),
                confidence=float(r["confidence"]),
            )
            for r in rows
        ]

    # ── Phase 1: compile ────────────────────────────────────────────────────

    def _compile_template(
        self,
        template_str: str,
        params: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        """Compile a Jinja2 template string into an (sql, args) pair.

        Structural branching (``{% if x %}…{% endif %}``) uses Jinja2 normally.
        Value injection **must** use ``{{ bind('param_name') }}``, which appends
        the value to ``bind_values`` and emits the corresponding ``$N`` token.

        Direct value interpolation (``{{ param_name }}``) is prevented by
        ``StrictUndefined`` — all params are pre-populated as ``None`` by
        ``execute()`` before this method is called, so the env will see them
        as ``False``-y for conditional checks but cannot stringify them as values
        (the bind macro is the only path to positional parameter injection).
        """
        bind_values: list[Any] = []

        def bind(param_name: str) -> str:
            if param_name not in params:
                raise KeyError(f"Required template slot missing: {param_name!r}")
            bind_values.append(params[param_name])
            return f"${len(bind_values)}"

        template = self._jinja.from_string(template_str)
        compiled = template.render(bind=bind, **params)
        return compiled, bind_values

    # ── Phase 1: execute ────────────────────────────────────────────────────

    async def execute(
        self,
        slug: str,
        params: dict[str, Any],
        namespace_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Execute a named template by slug.

        Security invariants
        -------------------
        1. ``namespace_id`` is always the session namespace — any
           caller-supplied ``namespace_id`` key in ``params`` is stripped
           before validation and re-injected as a ``uuid.UUID`` after.
        2. Missing optional schema properties are pre-populated as ``None``
           so ``{% if optional_field %}`` evaluates to ``False`` rather than
           raising ``UndefinedError`` from ``StrictUndefined``.
        3. ``jsonschema.validate`` runs before any DB access.
        """
        # Single session covers both the template fetch and the data query so
        # both operations share the same MVCC snapshot (no TOCTOU window) and
        # we only pay one pool checkout + SET LOCAL round-trip per call.
        async with scoped_pg_session(self.pool, namespace_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT raw_template, param_schema, target_engine, pipeline
                FROM query_templates
                WHERE slug = $1 AND is_active = TRUE
                """,
                slug,
            )

            if row is None:
                raise ValueError(f"Query template {slug!r} not found or is inactive.")

            schema: dict[str, Any] = (
                row["param_schema"]
                if isinstance(row["param_schema"], dict)
                else json.loads(row["param_schema"])
            )

            # Strip any caller-supplied namespace_id before validation so the
            # JSON Schema does not need to exclude it and there is no injection path.
            params = {k: v for k, v in params.items() if k != "namespace_id"}

            jsonschema.validate(instance=params, schema=schema)

            # Optional field trap fix: pre-populate all declared schema properties
            # that were not supplied with None.  This allows {% if optional_field %}
            # to evaluate as False rather than raising UndefinedError.
            for prop_name in schema.get("properties", {}):
                if prop_name not in params:
                    params[prop_name] = None

            # Re-inject namespace_id as uuid.UUID — never as a string.
            params["namespace_id"] = namespace_id

            engine = row["target_engine"]

            if engine == "postgres" and row.get("raw_template"):
                sql, args = self._compile_template(row["raw_template"], params)
                return [dict(r) for r in await conn.fetch(sql, *args)]

            if engine == "pipeline":
                # Pipeline dispatch is not yet implemented — return a clean 400-style
                # error rather than propagating NotImplementedError from _execute_pipeline.
                # Remove this gate once the pipeline executor is wired (Phase 2).
                raise ValueError(
                    f"Template {slug!r} uses target_engine='pipeline' which is not "
                    "yet supported. Use a 'postgres' template or wait for the pipeline "
                    "executor to be wired."
                )

            raise ValueError(
                f"Unsupported target_engine {row['target_engine']!r} for template {slug!r}."
            )

    # ── Phase 3: describe schema ─────────────────────────────────────────────

    async def describe_schema(
        self,
        namespace_id: uuid.UUID,
        limit: int = 50,
    ) -> GraphSchema:
        """Return the live vocabulary schema for this namespace.

        Reads ``graph_schema_registry`` — an O(1) PK lookup populated at
        write time by ``record_schema()``.  Never runs SELECT DISTINCT.
        """
        async with scoped_pg_session(self.pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT element_type, type_key
                FROM graph_schema_registry
                ORDER BY element_type, type_key
                LIMIT $1
                """,
                limit,
            )

        entity_types: list[str] = []
        edge_predicates: list[str] = []
        for r in rows:
            if r["element_type"] == "NODE":
                entity_types.append(r["type_key"])
            else:
                edge_predicates.append(r["type_key"])

        return GraphSchema(
            entity_types=entity_types,
            edge_predicates=edge_predicates,
            sampled_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Phase 3: write-time registry upsert ──────────────────────────────────

    @staticmethod
    async def record_schema(
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        nodes: list[Any],
        edges: list[Any],
    ) -> None:
        """Upsert vocabulary-level types into ``graph_schema_registry``.

        Must be called with an already-open connection from the Saga coordinator
        so the upsert shares the Saga's transaction — zero extra round trips.

        For nodes: ``type_key = n.entity_type``  (e.g. "Person", "CONCEPT")
        For edges: ``type_key = e.predicate``    (e.g. "AUTHORED", "ATTENDED")

        Instance labels (``n.label``, ``e.subject_label``, ``e.object_label``)
        are never stored — they live in ``kg_nodes``/``kg_edges``.

        Parameters
        ----------
        conn : asyncpg.Connection
            Open connection inside an active transaction.
        namespace_id : uuid.UUID
            Tenant namespace — passed as a native UUID, never as a string.
        nodes : list
            Objects exposing ``.entity_type`` (str).
        edges : list
            Objects exposing ``.predicate`` (str).
        """
        if nodes:
            node_data: list[tuple[uuid.UUID, str, str]] = [
                (namespace_id, "NODE", n.entity_type)
                for n in nodes
                if getattr(n, "entity_type", None)
            ]
            if node_data:
                await conn.executemany(
                    """
                    INSERT INTO graph_schema_registry
                        (namespace_id, element_type, type_key)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (namespace_id, element_type, type_key)
                    DO UPDATE SET updated_at = now()
                    """,
                    node_data,
                )

        if edges:
            seen: set[str] = set()
            edge_data: list[tuple[uuid.UUID, str, str]] = []
            for e in edges:
                pred = getattr(e, "predicate", None)
                if pred and pred not in seen:
                    seen.add(pred)
                    edge_data.append((namespace_id, "EDGE", pred))
            if edge_data:
                await conn.executemany(
                    """
                    INSERT INTO graph_schema_registry
                        (namespace_id, element_type, type_key)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (namespace_id, element_type, type_key)
                    DO UPDATE SET updated_at = now()
                    """,
                    edge_data,
                )


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------


async def _execute_pipeline(
    pool: asyncpg.Pool,
    steps: list[dict[str, Any]],
    params: dict[str, Any],
    namespace_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Execute an ordered list of pipeline steps.

    Parameter resolution: step params containing ``"$key"`` strings are resolved
    to their native typed values from the parent ``params`` dict.  This preserves
    int, float, and UUID types — nothing is cast to string.

    Concrete tool dispatch is wired to domain orchestrators during the
    Phase 1 integration step (``nce/catalog_mcp_handlers.py``).
    """
    results: list[dict[str, Any]] = []
    for step in steps:
        resolved: dict[str, Any] = {}
        for k, v in step.get("params", {}).items():
            if isinstance(v, str) and v.startswith("$"):
                resolved[k] = params.get(v[1:])
            else:
                resolved[k] = v
        # Dispatch is injected by the orchestrator layer.  Raise with context
        # so integration failures surface with the offending step name.
        raise NotImplementedError(
            f"Pipeline step dispatch not wired for tool {step.get('tool')!r}. "
            "Wire _execute_pipeline in nce/catalog_mcp_handlers.py."
        )
    return results
