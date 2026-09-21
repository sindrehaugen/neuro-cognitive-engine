"""AST census of ONE detectable shape of "mocked-DB-dependent test": a module that
directly calls a do_*/handle_*/`.handler(...)`-shaped name against a mocked
connection, with no live-Postgres sibling covering the same code. See "THE
GUARANTEE, AND ITS BOUNDARY" below before assuming this file covers your case --
it does not claim to see every module in the broader class, only this one shape.

Why this file exists
---------------------
2026-09-21 measurement (Lane F, dispatched from G's identifier-resolution finding):
a test module whose fixtures never wire a real ``pg_pool`` -- either
``nce.admin_state.engine`` stays ``None``/a bare mock, or the module builds its own
``MagicMock``/``AsyncMock`` connection with canned ``fetchrow``/``fetch``/``execute``
return values -- can run every assertion green while the ``do_*`` core or generated
``handle_*`` function it calls builds SQL, coerces types, or relies on a real
constraint (``NOT NULL``, ``CHECK``, ``UNIQUE``, a real ``EXCLUDE USING gist`` window,
an ``ON CONFLICT`` target) that the mock cannot enforce. A background research agent
surveyed all 243 modules under ``tests/unit/`` and classified 118 candidates by
reading each one; 81 met both conditions (mocks a DB-dependent call, not testing pure
logic that would behave identically against a real database); of those, 51 have no
live-Postgres sibling anywhere and 13 were explicit judgement calls (a module mostly
pure with one or two tests that slip past the patch boundary). The full report,
including the 30 with a same-named live sibling and the 9 generated-surface modules
whose HAND-WRITTEN half stays uncovered while the CRUD half looks covered, is archived
at ``_internal/work-docs/mlv16-orchestration/MOCKED_DB_TEST_INSTRUMENT_CENSUS.md``
(gitignored, main worktree only) -- not reproduced here, since this file's allowlist
is the enforceable subset, not the full audit.

This is a SHRINK-ONLY ratchet, not a fix-tonight list, per ML-orch's explicit ruling:
51 (plus the 13 borderline, included per that same ruling -- "the 13 are existing,
they belong in the baseline; force-classifying them to keep the list clean is how a
baseline becomes a lie") is dozens, not a handful, and demanding all 64 be fixed
before this lands would make the ratchet the kind that gets suppressed in a week. It
does not fire on the 64. It fires on the 65th.

THE GUARANTEE, AND ITS BOUNDARY -- read this before trusting this file for your case
--------------------------------------------------------------------------------------
This file does NOT guarantee "no 65th mocked-DB-dependent test joins tests/unit/."
It guarantees a narrower thing: "no 65th module joins tests/unit/ via the
``do_*``/``handle_*``/``.handler(...)`` DIRECT-CALL shape this scanner's AST matcher
looks for." A new module that reaches the same class of bug through a DIFFERENT
core-function naming or dispatch convention -- the same 24-of-64 pattern this file's
own calibration could not rediscover (see below) -- passes this census silently,
green, with nothing to flag it. ``test_a7_outbound_webhooks.py`` is the confirmed,
hand-checked example: it is a genuine member of the class this file exists to catch
(it is in the frozen baseline below), yet it contains zero calls whose name matches
``do_[a-z0-9_]+`` -- whatever DB-dependent function it mocks is named differently,
and the scanner cannot see it. If you are adding a new test module and asking
"would this census catch me if I got this wrong," the honest answer is: only if your
mocked call is a bare, unpatched, direct call to a name shaped like ``do_thing(...)``
or ``some_tool.handler(...)``. A dispatch through ``getattr``, a class method, a
differently-named core, or an indirection through a helper function is invisible to
this file, the same way ``test_internal_cores_prune_check_floor_census.py``'s own
scanner cannot see a load reached only through a helper. This file is a census of
ONE detectable shape within the larger class, not a census of the class itself --
treat its name accordingly, and see
``_internal/work-docs/mlv16-orchestration/MOCKED_DB_TEST_INSTRUMENT_CENSUS.md`` for
the full, human-read population this scanner's own reach does not cover.

Scope, deliberately narrow -- and what this census does NOT catch
---------------------------------------------------------------------
The research agent's classification was semantic: it read each file's code and asked
whether the mock hides something a real Postgres constraint would catch. That
judgement cannot be fully mechanized without re-deriving it per file, and the
narrower scanner below can only prove the ONE demonstrated shape:

    A test module, NOT marked ``pytest.mark.integration``, contains a direct
    (i.e. NOT ``unittest.mock.patch``-replaced) call to a name matching
    ``do_[a-z0-9_]+`` or a generated resource_surface handler name
    (``handle_create``/``handle_patch``/``handle_get``/``handle_list``/
    ``handle_archive``/``handle_restore``/``handle_upsert``/``handle_bulk``, or the
    comment/tag/document sub-resource handlers), or calls a ``.handler(`` attribute
    (the MCP ``ToolSpec.handler(engine, args)`` invocation convention this session's
    own live siblings use throughout).

Calibrated directly against the 64-entry baseline below, not assumed: of the 64, this
scanner independently rediscovers 40 by the shape above. The other 24 use a
core-function naming or dispatch pattern this scanner's narrower signal does not
reach (confirmed by hand for one, ``test_a7_outbound_webhooks.py``: it contains no
``do_*``-named call at all, so whatever DB-dependent function it mocks is named
differently). This is the SAME class of gap ``test_internal_cores_prune_check_floor_
census.py`` documents for its own scanner ("indirected loads are invisible") -- named
here rather than silently implied away. The baseline below is seeded from the
research agent's semantic read, kept as history; the scanner's own job from here is
narrower and mechanical: catch a NEW file matching the ``do_*``/handler-direct-call
shape that is not already in this baseline. It cannot catch a new violation shaped
like the 24 it cannot already see -- that limitation is real, not hidden.

Positive controls verify the scanner flags the shape it claims to catch and does not
fire on a patched-out or integration-marked equivalent. Modeled directly on
``test_swallowed_exception_census.py``'s shape (shrink-only allowlist with owner and
reason, a discovery floor on the scanner itself, standing positive controls) rather
than inventing a new pattern.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_UNIT_DIR: Final[Path] = _REPO_ROOT / "tests" / "unit"
_INTEGRATION_DIR: Final[Path] = _REPO_ROOT / "tests" / "integration"

_HANDLER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "handle_create",
        "handle_patch",
        "handle_get",
        "handle_list",
        "handle_archive",
        "handle_restore",
        "handle_upsert",
        "handle_bulk",
        "handle_add_comment",
        "handle_list_comments",
        "handle_add_tag",
        "handle_remove_tag",
        "handle_attach_document",
        "handle_detach_document",
        "handle_list_documents",
    }
)

# Shrink-only allowlist. Seeded from the 2026-09-21 research agent's semantic
# classification (see module docstring), not from this file's own narrower scanner --
# the scanner rediscovers 40 of these 64 independently (calibrated, not assumed; see
# test_discovery_floor_for_mocked_db_test_scanner). The other 24 are real, verified
# instances of the class this census exists to freeze, just not reachable by this
# scanner's specific `do_*`/handler-direct-call signal -- kept here anyway, per
# ML-orch's ruling, rather than dropped to make the scanner's own output look
# complete.
#
# "borderline": true marks the 13 modules that are mostly pure with one or two tests
# that slip past the patch boundary -- included per standing instruction ("force-
# classifying them to keep the list clean is how a baseline becomes a lie"), not
# because the classification is as clean as the other 51.
KNOWN_MOCKED_DB_DEPENDENT_TESTS: Final[dict[str, dict[str, object]]] = {
    "tests/unit/test_dealroom_cutover.py": {
        "owner": "sales",
        "borderline": False,
        "reason": "Asserts the starts_with prefix SQL, but manufacturer/model are hand-fed into the mock row -- the LEFT JOIN to product_catalog that must produce them never runs against a real table.",
    },
    "tests/unit/test_support_on_call_rota.py": {
        "owner": "support",
        "borderline": False,
        "reason": "The time-window/overlap WHERE clause is never exercised: the mock returns the allocation row regardless of the at/starts_at/ends_at values passed, so a broken window predicate is invisible.",
    },
    "tests/unit/test_assets_service_history.py": {
        "owner": "assets",
        "borderline": False,
        "reason": "MockDBConn dispatches on query substrings; every JOIN's output column is invented by the test, so a broken FROM service_tickets predicate would return the identical timeline.",
    },
    "tests/unit/test_agreements_coverage_surface.py": {
        "owner": "agreements",
        "borderline": False,
        "reason": "extracted is pre-serialized by the test and flagged_at is fed in as a str where a real timestamptz column would round-trip as a datetime -- a real type mismatch is unobservable.",
    },
    "tests/unit/test_agreements_price_rules.py": {
        "owner": "agreements",
        "borderline": False,
        "reason": "A two-table lookup is faked by a query-substring router; metadata is hand-fed as a dict and amounts as Decimal, so a real jsonb codec or numeric cast failure cannot surface.",
    },
    "tests/unit/test_agreements_procurement_cross_engine.py": {
        "owner": "agreements",
        "borderline": False,
        "reason": "The idempotency-key burn is asserted without the real UNIQUE-key write ever executing, so a collision that a real constraint would reject is untestable here.",
    },
    "tests/unit/test_assets_health.py": {
        "owner": "assets",
        "borderline": False,
        "reason": "The \"age-only, no telemetry\" verdict is produced by fetch() returning an empty list by test fixture choice -- a genuinely broken telemetry query would look identical.",
    },
    "tests/unit/test_assets_person_subcomponents.py": {
        "owner": "assets",
        "borderline": False,
        "reason": "The circular-dependency refusal is decided by fetchval returning 1 from the test; the real recursive-CTE cycle-detection query this guards never executes.",
    },
    "tests/unit/test_assets_surface.py": {
        "owner": "assets",
        "borderline": False,
        "reason": "do_advance_lifecycle is treated as successful because execute() returns the literal string \"UPDATE 1\" -- a zero-row update silently filtered by RLS would be indistinguishable.",
    },
    "tests/unit/test_business_insights_ask_ratchet.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Grounding is derived from hand-built snapshot rows with raw fed in as the literal string \"{}\" rather than a value a real jsonb column would ever actually return.",
    },
    "tests/unit/test_economy_gl_records.py": {
        "owner": "economy",
        "borderline": False,
        "reason": "The join-produced vendor_label column is fed directly into the mock row; the leakage verdict under test rests entirely on a canned agreements row, never a real join.",
    },
    "tests/unit/test_economy_peppol_surface.py": {
        "owner": "economy",
        "borderline": False,
        "reason": "Both the CPI-cap verdict and the \"no contract found\" path come from a canned economy_contracts row with plain int amounts standing in for what is really a numeric column.",
    },
    "tests/unit/test_field_tech_ladder.py": {
        "owner": "field_tech",
        "borderline": False,
        "reason": "BOM_LINE status transition and as-built edge writes are simulated via fetchrow.side_effect; the real UPDATE ... RETURNING semantics that decide success are never exercised.",
    },
    "tests/unit/test_hr_compliance_watcher.py": {
        "owner": "hr",
        "borderline": False,
        "reason": "The UPDATE absences call binds a JSON-serialised argument to what is a real jsonb column; transaction rollback is faked by a plain Python dict standing in for a commit.",
    },
    "tests/unit/test_hr_core.py": {
        "owner": "hr",
        "borderline": False,
        "reason": "The INSERT ... RETURNING row is hand-fed, including a jsonb raw field as a Python str vs. dict inconsistently -- no real asyncpg codec ever runs to catch the mismatch.",
    },
    "tests/unit/test_hr_events_a2a.py": {
        "owner": "hr",
        "borderline": False,
        "reason": "Both the kg_nodes/kg_edges writes and the GROUP BY aggregate counts under test are supplied directly by the mock, not computed by a real query.",
    },
    "tests/unit/test_marketing_advisor.py": {
        "owner": "marketing",
        "borderline": False,
        "reason": "Status transitions are decided by a fetchrow keyed on query substrings, so the actual WHERE/SET clauses that would enforce a real transition are never checked.",
    },
    "tests/unit/test_marketing_consent.py": {
        "owner": "marketing",
        "borderline": False,
        "reason": "A missing or cross-tenant testimonial still yields ok: True, because the mock's execute() returns the literal \"INSERT 0 1\" regardless of whether a real row existed to update.",
    },
    "tests/unit/test_marketing_events_brief.py": {
        "owner": "marketing",
        "borderline": False,
        "reason": "Morning-brief GROUP BY results are hand-fed per query substring rather than computed by a real aggregate query against real rows.",
    },
    "tests/unit/test_marketing_publish.py": {
        "owner": "marketing",
        "borderline": False,
        "reason": "The MK-1/MK-4 gate row is invented by the test, so the UPDATE content_assets transition plus event_log write inside one transaction is never actually verified together.",
    },
    "tests/unit/test_marketing_retract_surface.py": {
        "owner": "marketing",
        "borderline": False,
        "reason": "The DB block is wrapped in try/except and returns status: \"retracted\" regardless of outcome -- a no-op UPDATE that changed nothing is invisible to every assertion.",
    },
    "tests/unit/test_product_manufacturer_sources.py": {
        "owner": "product",
        "borderline": False,
        "reason": "etim_specs is hand-fed as a Python dict where Postgres stores it as jsonb, and the provenance write is patched away entirely -- neither codec nor write path is proven.",
    },
    "tests/unit/test_product_match_decision_c10.py": {
        "owner": "product",
        "borderline": False,
        "reason": "fetchval/fetchrow return canned ids in every case, so a real NOT NULL, FK, or jsonb-cast failure on the underlying write has nothing to surface against.",
    },
    "tests/unit/test_product_p2_p3_ratchet.py": {
        "owner": "product",
        "borderline": False,
        "reason": "Three separate INSERTs are checked only by substring match on the captured query text -- none of them run against real column types or constraints.",
    },
    "tests/unit/test_product_quality_rest.py": {
        "owner": "product",
        "borderline": False,
        "reason": "The mock never inspects the query it receives; the grade/completeness score under test is computed from a hand-built row, not a real read.",
    },
    "tests/unit/test_product_related.py": {
        "owner": "product",
        "borderline": False,
        "reason": "An ON CONFLICT (subject_label, predicate, object_label, namespace_id) upsert runs against a mock -- the real unique index this clause depends on is never exercised.",
    },
    "tests/unit/test_product_surface.py": {
        "owner": "product",
        "borderline": False,
        "reason": "kg_nodes/kg_edges ON CONFLICT upserts are asserted only as SQL substrings, never against a real unique constraint that could reject a conflicting row.",
    },
    "tests/unit/test_project_case_study_edge_ratchet.py": {
        "owner": "project",
        "borderline": False,
        "reason": "The project<->CASE_STUDY join's output columns are hand-fed directly into conn.fetch, so a broken join predicate would return the identical result set.",
    },
    "tests/unit/test_resources_allocations.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "The real btree_gist EXCLUDE USING gist overlap constraint is re-implemented in Python inside the mock (max(starts,ex_start) < min(ends,ex_end)) instead of being enforced by Postgres.",
    },
    "tests/unit/test_resources_field_schedule.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "A five-table compose is hand-assembled by the mock and tstzrange window filtering is performed in Python, not by the real range-type query this schedule depends on.",
    },
    "tests/unit/test_resources_forecast.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "A four-table aggregate runs against a plain dict store; only the returned numbers are asserted, never the SQL that would have produced them against real tables.",
    },
    "tests/unit/test_resources_material_flow.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "The stock_locations lookup and the allocations INSERT both run against canned rows, so a real FK or stock-level constraint violation has no path to surface.",
    },
    "tests/unit/test_resources_planner.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "The AVG(rating)/AVG(quality) aggregate a real query would compute is instead summed and divided in Python inside the mock's fetchrow, keyed on a query-substring match.",
    },
    "tests/unit/test_resources_registry.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "The tenant-isolation \"proof\" is the mock's own namespace_id string comparison in Python, not a real RLS policy or a WHERE namespace_id = $1 clause.",
    },
    "tests/unit/test_resources_travel.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "Idempotency replay is decided by the mock matching attrs->>'idempotency_key' in Python; the real unique constraint that would enforce this at the database never runs.",
    },
    "tests/unit/test_resources_watcher.py": {
        "owner": "resources",
        "borderline": False,
        "reason": "The metadata-jsonb employee lookup and the future-allocation UPDATE predicate are both re-implemented in Python against the mock rather than run as real SQL.",
    },
    "tests/unit/test_sales_divergence_surface.py": {
        "owner": "sales",
        "borderline": False,
        "reason": "The window/materiality/pagination SQL executes, but the counts and rows returned are entirely canned -- only the handler's own returned dict is ever asserted.",
    },
    "tests/unit/test_sales_stalled_and_brief.py": {
        "owner": "sales",
        "borderline": False,
        "reason": "slip_days and the updated_at staleness predicate are only ever exercised as a Python post-filter over hand-fed rows, never as a real WHERE clause.",
    },
    "tests/unit/test_support_ecosystem.py": {
        "owner": "support",
        "borderline": False,
        "reason": "An ON CONFLICT DO UPDATE edge insert is checked only by substring on the query text, and the JOIN's output columns are hand-fed into the mock.",
    },
    "tests/unit/test_support_sla_health.py": {
        "owner": "support",
        "borderline": False,
        "reason": "The customer_health UPSERT's ON CONFLICT target and jsonb codec are both invisible -- the row the test asserts on is hand-fed, not produced by a real upsert.",
    },
    "tests/unit/test_support_sla_watcher.py": {
        "owner": "support",
        "borderline": False,
        "reason": "A LEFT JOIN sweep runs against a mock, and transactional rollback is proven only by a hand-written fake transaction object, not a real ROLLBACK.",
    },
    "tests/unit/test_support_ticket_summary.py": {
        "owner": "support",
        "borderline": False,
        "reason": "kg_edges label-parsing rows are hand-fed directly; the edge insert this summary depends on is never validated against the real table's own constraints.",
    },
    "tests/unit/test_support_troubleshoot.py": {
        "owner": "support",
        "borderline": False,
        "reason": "The v3_cognitive_ledger jsonb retrieval that drives the confidence score is replaced by canned JSON-string rows, never a real jsonb column read.",
    },
    "tests/unit/test_vendors_seed.py": {
        "owner": "vendors",
        "borderline": False,
        "reason": "A DISTINCT plus source_json jsonb harvest runs against hand-fed rows while the real writer, do_upsert_vendor, is patched out entirely.",
    },
    "tests/unit/test_system_design_fl_tree.py": {
        "owner": "system_design",
        "borderline": False,
        "reason": "pg_pool=None routes cycle detection and recursive ancestor traversal to in-memory dict walks (_MEM_EDGES), never the recursive-CTE query the real path uses.",
    },
    "tests/unit/test_system_design_design_versions.py": {
        "owner": "system_design",
        "borderline": False,
        "reason": "A poolless engine hides the atomic single-active-design constraint and the real jsonb round-trip this module's own docstring claims to prove.",
    },
    "tests/unit/test_system_design_room_categories.py": {
        "owner": "system_design",
        "borderline": False,
        "reason": "has_category/responsible_for edge upserts become pure dict operations on _MEM_NODES/_MEM_EDGES rather than real kg_edges writes.",
    },
    "tests/unit/test_system_design_sow.py": {
        "owner": "system_design",
        "borderline": False,
        "reason": "Ordered fetch() side_effect values stand in for the design/functional-location/line queries this statement-of-work builder depends on.",
    },
    "tests/unit/test_a7_outbound_webhooks.py": {
        "owner": "core-webhooks",
        "borderline": False,
        "reason": "Selector/predicate matching that is supposed to happen in SQL, plus RLS scoping, is hidden behind canned rows the test hands back instead of a real query result.",
    },
    "tests/unit/test_action_approval_queue.py": {
        "owner": "core-actions",
        "borderline": False,
        "reason": "A SimulatedDatabase substring-matches SQL text; the idempotency unique index and the WHERE status='pending' race condition it guards are both plain Python dict operations.",
    },
    "tests/unit/test_signing_credentials_q4_ratchet.py": {
        "owner": "sales",
        "borderline": False,
        "reason": "A FakeDb dict hides the ON CONFLICT upsert, the per-namespace uniqueness constraint, and the ciphertext column's persistence (only the encryption itself is real).",
    },
    "tests/unit/test_hr_c9b_guard.py": {
        "owner": "hr",
        "borderline": True,
        "reason": "Mostly a pure guard-rejection-before-SQL module; one or two tests slip past the patch boundary and run a real query shape against a canned row -- a judgement call, not a clean instance.",
    },
    "tests/unit/test_marketing_core.py": {
        "owner": "marketing",
        "borderline": True,
        "reason": "Mostly scoring/config logic with no DB dependency; a small number of tests exercise a DB-touching path against a mocked connection -- borderline by the same shape as the others in this group.",
    },
    "tests/unit/test_marketing_property_and_negative_lift.py": {
        "owner": "marketing",
        "borderline": True,
        "reason": "Primarily statistical/property-based checks with no database involved; a minority of cases touch a mocked write path, making module-level classification a policy call.",
    },
    "tests/unit/test_netbox_circuits.py": {
        "owner": "system_design",
        "borderline": True,
        "reason": "Mostly parsing/normalisation of NetBox API payloads with no SQL; a handful of tests persist the parsed result through a mocked connection.",
    },
    "tests/unit/test_product_hardening.py": {
        "owner": "product",
        "borderline": True,
        "reason": "Predominantly input-validation/hardening checks that run before any query; a minority of tests reach a mocked write beyond that validation boundary.",
    },
    "tests/unit/test_sales_ai_surface.py": {
        "owner": "sales",
        "borderline": True,
        "reason": "Mostly prompt/response shaping logic with no DB dependency; a small number of tests persist AI output through a mocked connection.",
    },
    "tests/unit/test_sales_quote_render_ratchet.py": {
        "owner": "sales",
        "borderline": True,
        "reason": "Primarily a rendering/formatting ratchet with no database involved; a minority of cases touch a mocked persistence path for the rendered artifact.",
    },
    "tests/unit/test_sales_signing_loop.py": {
        "owner": "sales",
        "borderline": True,
        "reason": "Mostly state-machine transition logic; a small number of tests advance the loop through a mocked write rather than pure in-memory state.",
    },
    "tests/unit/test_support_sync.py": {
        "owner": "support",
        "borderline": True,
        "reason": "Predominantly payload translation/mapping logic between systems; a minority of tests persist the translated result through a mocked connection.",
    },
    "tests/unit/test_system_design_standards_and_signals.py": {
        "owner": "system_design",
        "borderline": True,
        "reason": "Mostly standards-lookup and signal-classification logic with no DB dependency; a small number of tests touch a mocked write path.",
    },
    "tests/unit/test_decision_feedback.py": {
        "owner": "core-decisions",
        "borderline": True,
        "reason": "Pins the literal INSERT INTO decision_feedback text and every bound parameter (including JSON-serialised fields) -- more rigorous than most of this list -- but the read-back is still a canned fetchrow.return_value, so a real NOT NULL/FK/type mismatch on write stays unprovable. Whether pinning the SQL text is \"covered enough\" is a policy call, not a measurement.",
    },
    "tests/unit/test_degradation_register.py": {
        "owner": "business_insights",
        "borderline": True,
        "reason": "Mostly pure register/threshold logic; a minority of tests persist a degradation event through a mocked connection.",
    },
    "tests/unit/test_economy_approve_invoice.py": {
        "owner": "economy",
        "borderline": True,
        "reason": "Mostly approval-workflow state logic with no DB dependency; a small number of tests advance the approval through a mocked write.",
    },
}

# Modules the scanner itself surfaced (2026-09-21) beyond the research agent's
# original 118-candidate sweep -- the agent read 118 of 243 tests/unit/ modules by
# hand; this file's own do_*/handler-direct-call scanner, run against the full 243,
# found these additional matches with no do_* name overlap against any
# tests/integration/*.py file. Kept SEPARATE from KNOWN_MOCKED_DB_DEPENDENT_TESTS
# rather than merged in, because these have not had the same per-file semantic read
# the 64 above got -- each reason below states plainly what was mechanically found,
# not what the mock specifically hides, which is real information but a different
# (weaker) claim than the reasoned 64. Pending semantic review; do not treat "the
# reason is 60+ chars" as equivalent to "this was read like the other 64 were."
KNOWN_MOCKED_DB_TESTS_PENDING_SEMANTIC_REVIEW: Final[dict[str, dict[str, object]]] = {
    "tests/unit/test_business_insights_aggregation.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_ask_business directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_business_insights_composed_slices_ratchet.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_generate_board_pack, do_morning_brief, do_risk_radar directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_business_insights_core.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_generate_board_pack, do_morning_brief, do_run_scenario directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_business_insights_coverage.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_risk_radar directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_business_insights_degradation.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_kpi_dashboard directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_business_insights_egress.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_ask_business directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_business_insights_fabricated_defaults_ratchet.py": {
        "owner": "business_insights",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_generate_board_pack, do_risk_radar, do_run_scenario directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_customer_portal_advisor.py": {
        "owner": "customer_portal",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_advisor_answer directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_customer_portal_post_handover.py": {
        "owner": "customer_portal",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_get_document, do_list_documents, do_list_invoices directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_customer_portal_tracker.py": {
        "owner": "customer_portal",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_asset_register, do_room_overview, do_room_tracker directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_economy_financial_event.py": {
        "owner": "economy",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_emit_financial_event directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_economy_forecast_dunning.py": {
        "owner": "economy",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_compute_dunning, do_forecast_cashflow directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_economy_match.py": {
        "owner": "economy",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_match_invoice directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_economy_ngaap.py": {
        "owner": "economy",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_compute_bucket_targets directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_hr_onboarding.py": {
        "owner": "hr",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_build_onboarding_quest, do_get_onboarding_progress, do_query_absences directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_hr_privacy_coach.py": {
        "owner": "hr",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_coach, do_log_one_on_one directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_inventory_hardening.py": {
        "owner": "inventory",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_stock_levels, do_transfer_stock directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_procurement_ranking.py": {
        "owner": "procurement",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_rank_suppliers directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_procurement_tco.py": {
        "owner": "procurement",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_calculate_tco directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_product_enrich_ratchet.py": {
        "owner": "product",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_enrich_product directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_product_pricing.py": {
        "owner": "product",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_price_product directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_project_case_study.py": {
        "owner": "project",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_generate_case_study_edge directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_support_autoclose.py": {
        "owner": "support",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_resolve_ticket directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_support_proactive.py": {
        "owner": "support",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_open_proactive_telemetry_ticket directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
    "tests/unit/test_system_design_lucid.py": {
        "owner": "system_design",
        "borderline": False,
        "reason": "Scanner-detected 2026-09-21: calls do_publish_design_docs directly against a mocked connection/engine, with no matching do_* name found in any tests/integration/ file. Not individually read to confirm the specific mechanism the mock hides -- flagged for the same class the reviewed 64 were, pending a semantic read.",
    },
}

# Modules the scanner flags (calls a do_*/handler name directly, no patch) but whose
# do_* name(s) also appear in at least one tests/integration/*.py file -- redundant-
# but-harmless by the same name-match heuristic the research agent used for its own
# "(a) covered" bucket (30 files). A name match is not a proof the live test exercises
# the SAME branch; kept here as an upper bound, not a clean guarantee, per the
# agent's own stated caveat.
KNOWN_MOCKED_DB_TESTS_WITH_LIVE_SIBLING: Final[dict[str, str]] = {
    "tests/unit/test_resource_surface.py": "generated CRUD covered by test_resource_surface_patch_live.py/archive_restore_live.py across 39-56 specs",
    "tests/unit/test_resource_surface_generated_controls.py": "generated CRUD covered by the same live patch/archive siblings as test_resource_surface.py",
    "tests/unit/test_agreements_resources.py": "agreements:agreements CRUD covered live; agreements-specific hand-written cores not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_sales_resources.py": "sales:* CRUD covered live; hand-written sales cores not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_assets_resource_surface.py": "assets CRUD covered live; move/merge/link-product hand-written cores not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_c13_notifications.py": "notifications CRUD covered live; fire_pending_reminders not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_c14_documents.py": "documents CRUD covered live; share-token cores not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_c15_legal_entities.py": "legal_entities CRUD covered live; legal_entities service + org_nr uniqueness not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_c17_site_master.py": "site_master CRUD covered live; site vessel telemetry not covered -- see the 9-module gap in the archived census",
    "tests/unit/test_customer_portal_actions.py": "do_raise_service_request/do_register_expansion_interest also exercised by test_customer_portal_cp1_loop.py-adjacent live coverage",
    "tests/unit/test_customer_portal_cp1_loop.py": "do_raise_service_request/do_register_expansion_interest name-matched in tests/integration/",
    "tests/unit/test_field_tech_core.py": "do_assign/do_attach_photo/do_complete_checklist name-matched in tests/integration/test_field_tech_identifier_writable_fields_live.py-adjacent coverage",
    "tests/unit/test_inventory_goods_receipt_created.py": "do_record_goods_receipt name-matched in tests/integration/",
    "tests/unit/test_outcome_feed_c10_ratchet.py": "do_record_allocation_outcome/do_record_outcome name-matched in tests/integration/",
    "tests/unit/test_procurement_po_lifecycle_tools.py": "do_generate_po/do_submit_po name-matched in tests/integration/",
    "tests/unit/test_procurement_three_way_match.py": "do_evaluate_three_way_match name-matched in tests/integration/",
    "tests/unit/test_project_convert_degraded.py": "do_convert_signed_quote name-matched in tests/integration/",
    "tests/unit/test_project_pj1_sd3_ratchet.py": "do_advance_phase/do_propose_design/do_record_project_outcome name-matched in tests/integration/",
    "tests/unit/test_sales_signing_delivery_ratchet.py": "do_on_signed_callback name-matched in tests/integration/",
    "tests/unit/test_sales_signing_email_code_ratchet.py": "do_on_signed_callback/do_request_signature name-matched in tests/integration/",
    "tests/unit/test_seam_ratchet.py": "do_raise_service_request/do_register_expansion_interest name-matched in tests/integration/",
    "tests/unit/test_support_dispatch.py": "do_dispatch_work_order name-matched in tests/integration/",
    "tests/unit/test_support_field_tech_dispatch_event.py": "do_dispatch_work_order name-matched in tests/integration/",
    "tests/unit/test_support_tickets.py": "do_open_ticket/do_query_ticket/do_resolve_ticket name-matched in tests/integration/",
    "tests/unit/test_system_design_sd2_ratchet.py": "do_propose_design name-matched in tests/integration/",
    "tests/unit/test_trust_dial_t1_ratchet.py": "do_propose_design name-matched in tests/integration/",
    "tests/unit/test_vendors_hardening.py": "do_calibrate_weights/do_check_cert_expiry/do_check_tier_at_risk name-matched in tests/integration/",
    "tests/unit/test_wave5_sd2_rs3_ft4_a1_ratchet.py": "do_propose_design/do_record_allocation_outcome/do_record_outcome name-matched in tests/integration/",
    "tests/unit/test_inventory_surface.py": "inventory CRUD covered live via the resource_surface handler signal, not a do_* name",
}

_ALL_KNOWN_MOCKED_SITES: Final[frozenset[str]] = frozenset(
    set(KNOWN_MOCKED_DB_DEPENDENT_TESTS)
    | set(KNOWN_MOCKED_DB_TESTS_PENDING_SEMANTIC_REVIEW)
    | set(KNOWN_MOCKED_DB_TESTS_WITH_LIVE_SIBLING)
)


def _has_integration_marker(tree: ast.AST) -> bool:
    """True if the module sets `pytestmark = pytest.mark.integration` (or includes
    it in a list) -- the convention every live-Postgres sibling in this repo uses."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    if "integration" in ast.dump(node.value):
                        return True
    return False


def _patched_out_names(tree: ast.AST) -> set[str]:
    """Bare names passed as the dotted-path string argument to `patch(...)`
    (call, decorator, or context manager) -- these are explicitly mocked AWAY,
    never actually invoked, so a direct-call finding on the same name would be a
    false positive."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            if fname == "patch" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    names.add(arg0.value.rsplit(".", 1)[-1])
    return names


def _is_dependent_call_name(name: str) -> bool:
    if name in _HANDLER_NAMES:
        return True
    return name.startswith("do_") and name[3:].replace("_", "").isalnum()


def _direct_dependent_calls(tree: ast.AST, patched_out: set[str]) -> set[str]:
    """Every `do_*`/handler-shaped name actually CALLED in this module (not
    merely patched out), plus any `.handler(` attribute call -- the
    `ToolSpec.handler(engine, args)` convention this session's own MCP live
    siblings use."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            fname = node.func.id
            if fname not in patched_out and _is_dependent_call_name(fname):
                found.add(fname)
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
            if fname == "handler":
                found.add("<mcp-tool>.handler(...)")
            elif fname not in patched_out and _is_dependent_call_name(fname):
                found.add(fname)
    return found


def _scan_file(path: Path) -> set[str]:
    """Returns the set of DB-dependent names this file calls directly, or an
    empty set if the file is integration-marked, unparseable, or calls nothing
    matching the shape. An empty return means "not flagged", not "clean" --
    see module docstring's documented 24-file gap."""
    try:
        tree = ast.parse(path.read_bytes())
    except Exception:
        return set()
    if _has_integration_marker(tree):
        return set()
    patched_out = _patched_out_names(tree)
    return _direct_dependent_calls(tree, patched_out)


def _scan_all_unit_files() -> dict[str, set[str]]:
    findings: dict[str, set[str]] = {}
    for py_path in sorted(_UNIT_DIR.glob("*.py")):
        hits = _scan_file(py_path)
        if hits:
            rel = py_path.relative_to(_REPO_ROOT).as_posix()
            findings[rel] = hits
    return findings


def test_mocked_db_dependent_test_census_is_shrink_only() -> None:
    """No NEW file may appear matching this scanner's do_*/handler-direct-call
    shape without being added to the allowlist with a reason. This does not
    require the scanner to have rediscovered all 64 existing entries (see
    module docstring's documented 24-file gap) -- it only requires that nothing
    NEW, in the shape the scanner CAN see, goes unaccounted for.
    """
    live = _scan_all_unit_files()
    unallowlisted = set(live.keys()) - _ALL_KNOWN_MOCKED_SITES
    assert not unallowlisted, (
        f"Found {len(unallowlisted)} unit test module(s) calling a DB-dependent "
        f"do_*/handler function directly (not patched out), with no live-Postgres "
        f"sibling accounted for: {sorted(unallowlisted)}. Either add a live-Postgres "
        f"sibling covering the same function(s) and leave this module out, or add it "
        f"to KNOWN_MOCKED_DB_DEPENDENT_TESTS with an owner and a reason explaining "
        f"specifically what the mock hides -- see this file's own entries for the "
        f"expected specificity."
    )


def test_mocked_db_dependent_test_allowlist_is_reasoned() -> None:
    """Every allowlisted module must have an owner, a borderline flag, and a
    reason of substantive length (>= 60 chars, matching the swallowed-exception
    census's own bar)."""
    for site_id, meta in KNOWN_MOCKED_DB_DEPENDENT_TESTS.items():
        assert "owner" in meta and meta["owner"], f"{site_id} missing owner"
        assert "borderline" in meta and isinstance(meta["borderline"], bool), (
            f"{site_id} missing a boolean 'borderline' flag"
        )
        reason = meta.get("reason", "")
        assert isinstance(reason, str) and len(reason) >= 60, (
            f"{site_id} reason must be >= 60 chars, got {len(reason)}"
        )


def test_mocked_db_dependent_test_allowlist_files_still_exist() -> None:
    """Shrink-only in the other direction: an allowlisted path that no longer
    exists (renamed or deleted) must be removed, not left as a stale entry."""
    missing = [
        site_id
        for site_id in KNOWN_MOCKED_DB_DEPENDENT_TESTS
        if not (_REPO_ROOT / site_id).is_file()
    ]
    assert not missing, (
        f"Allowlist references file(s) that no longer exist: {missing}. Remove them "
        f"from KNOWN_MOCKED_DB_DEPENDENT_TESTS -- if remediated by deletion, that is "
        f"a ratchet-down; if renamed, update the key rather than leaving a dead entry."
    )


def test_mocked_db_dependent_test_baseline_reports_measured_count() -> None:
    """Guard against a silently-edited baseline: the reported population size is
    always len(KNOWN_MOCKED_DB_DEPENDENT_TESTS) itself, never a separately typed
    number -- this test exists so that number has somewhere to be exercised,
    not to assert a specific literal.
    """
    total = len(KNOWN_MOCKED_DB_DEPENDENT_TESTS)
    borderline = sum(1 for m in KNOWN_MOCKED_DB_DEPENDENT_TESTS.values() if m["borderline"])
    clean = total - borderline
    assert total == clean + borderline
    assert total >= 60, (
        f"Baseline count dropped to {total} (expected >= 60, seeded at 64 = 51 clean + "
        f"13 borderline on 2026-09-21) -- if modules were genuinely remediated, this "
        f"drop is real progress; confirm each removed entry actually gained a live "
        f"sibling rather than being deleted from the dict to make this test pass."
    )


def test_discovery_floor_for_mocked_db_test_scanner() -> None:
    """Guard-the-guard: the scanner's do_*/handler-direct-call signal must still
    find at least 40 of the 64 baseline entries (calibrated directly against the
    baseline, not assumed -- see module docstring). A drop below this floor means
    the AST matcher broke, not that the estate remediated two dozen modules
    overnight.
    """
    live = _scan_all_unit_files()
    rediscovered = set(live.keys()) & set(KNOWN_MOCKED_DB_DEPENDENT_TESTS.keys())
    assert len(rediscovered) >= 40, (
        f"Scanner rediscovered only {len(rediscovered)} of the 64 baseline entries "
        f"(expected >= 40) -- the do_*/handler-name matcher likely broke."
    )


def test_positive_control_detects_direct_call_to_mocked_db_dependent_function() -> None:
    """Standing positive control: a NEW module (not in the baseline) with a
    direct, unpatched call to a do_*-shaped function must be flagged."""
    bad_code = """
from unittest.mock import MagicMock

async def test_synthetic_offender():
    conn = MagicMock()
    conn.fetchrow.return_value = {"id": "x"}
    result = await do_synthetic_offending_thing(conn, "arg")
    assert result["id"] == "x"
"""
    tree = ast.parse(bad_code)
    patched_out = _patched_out_names(tree)
    hits = _direct_dependent_calls(tree, patched_out)
    assert hits == {"do_synthetic_offending_thing"}, (
        f"Positive control failed to flag the known-bad shape: {hits}"
    )


def test_positive_control_ignores_patched_out_function() -> None:
    """A do_* name that only ever appears as a patch() target -- never actually
    invoked -- must not be flagged; this is test_hr_rest.py's own shape."""
    good_code = """
from unittest.mock import patch

async def test_patches_it_away():
    with patch("nce.admin_handlers.hr.do_query_employees") as mock_core:
        mock_core.return_value = {"items": []}
"""
    tree = ast.parse(good_code)
    patched_out = _patched_out_names(tree)
    hits = _direct_dependent_calls(tree, patched_out)
    assert hits == set(), f"Positive control over-fired on a fully patched-out call: {hits}"


def test_positive_control_ignores_integration_marked_modules() -> None:
    """A module carrying `pytestmark = pytest.mark.integration` is a live-Postgres
    sibling by this repo's own standing convention and must never be scanned,
    regardless of what it calls directly."""
    live_module_code = """
import pytest

pytestmark = pytest.mark.integration

async def test_real_postgres_call(pg_pool):
    result = await do_something_real(pg_pool)
    assert result
"""
    tree = ast.parse(live_module_code)
    assert _has_integration_marker(tree) is True


def test_positive_control_ignores_pure_logic_with_no_dependent_call() -> None:
    """A module with plenty of mocks but no do_*/handler-shaped call at all must
    not be flagged -- otherwise this census would fire on nearly every test file
    in the suite that happens to use MagicMock for something unrelated."""
    unrelated_code = """
from unittest.mock import MagicMock

def test_pure_math():
    calc = MagicMock()
    calc.compute.return_value = 42
    assert calc.compute() == 42
"""
    tree = ast.parse(unrelated_code)
    patched_out = _patched_out_names(tree)
    hits = _direct_dependent_calls(tree, patched_out)
    assert hits == set(), f"Positive control over-fired on unrelated mock usage: {hits}"


def test_positive_control_detects_handler_attribute_call() -> None:
    """The `.handler(engine, args)` MCP ToolSpec invocation convention this
    session's own live siblings use must also be caught -- a module that mocks
    the engine and calls a tool's .handler(...) directly is the same class of
    risk as a bare do_* call."""
    mcp_style_code = """
async def test_synthetic_mcp_offender(mock_engine, some_tool):
    result = await some_tool.handler(mock_engine, {"id": "x"})
    assert result
"""
    tree = ast.parse(mcp_style_code)
    patched_out = _patched_out_names(tree)
    hits = _direct_dependent_calls(tree, patched_out)
    assert hits == {"<mcp-tool>.handler(...)"}, f"Failed to flag a .handler(...) call: {hits}"
