"""CI integration-coverage ratchet.

``ci.yml`` runs integration tests from a **hardcoded file allowlist**, while the
unit job selects ``-m "not integration and not perf"``.  Anything carrying an
``@pytest.mark.integration`` marker and absent from that allowlist therefore runs
in **no CI job at all** -- it is written, it passes locally, and it gates nothing.

That has already happened twice.  The comment above the M3 Agreements step in
``.github/workflows/ci.yml`` records it ("these 2 757 lines of tests previously ran
in NO CI job ... so the '90 M3 tests green' claim gated nothing"), and it was fixed
for those four files only.  Batch 122 hit it again.  When this ratchet was added,
120 of 131 files carrying integration markers were unwired -- 362 marked tests.

This module does not fix that backlog.  Most of those files need services CI does
not currently start, so retiring them is an incremental project.  What it does is
make the backlog **visible and non-growing**: a newly added integration file must
either be wired into a workflow or consciously listed in ``KNOWN_UNWIRED``, and an
entry that stops being accurate must be removed.

These are plain unit tests on purpose -- they must run in the job that always runs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Files carrying integration markers that no CI workflow currently runs.
#
# This list may SHRINK freely.  Adding to it means consciously accepting that a
# test gates nothing -- do that only with a reason, and prefer wiring the file
# into the integration job in ci.yml instead.
KNOWN_UNWIRED: frozenset[str] = frozenset(
    {
        "tests/diagnostics/test_diag_worker.py",
        "tests/diagnostics/test_diagnostic_ingest.py",
        "tests/diagnostics/test_digest_writer.py",
        "tests/test_active_learning_signals.py",
        "tests/test_actor_trust.py",
        "tests/test_agreements_coverage.py",
        "tests/test_agreements_extract.py",
        "tests/test_agreements_graph.py",
        "tests/test_agreements_kickback.py",
        "tests/test_agreements_review.py",
        "tests/test_autonomy_schema.py",
        "tests/test_batch44_worm_pii_sidesinks.py",
        "tests/test_batch49_pii_derivation.py",
        "tests/test_c1_donewhen.py",
        "tests/test_c2_donewhen.py",
        "tests/test_c3_adversarial.py",
        "tests/test_c4_donewhen.py",
        "tests/test_c5_donewhen.py",
        "tests/test_cascade_residual.py",
        "tests/test_causal_dag.py",
        "tests/test_chain_and_decay_integration.py",
        "tests/test_change_origin.py",
        "tests/test_consolidation_depth.py",
        "tests/test_cron_chain_verify.py",
        "tests/test_d365_incremental.py",
        "tests/test_d365_kg_upsert.py",
        "tests/test_diag_schema_rls.py",
        "tests/test_dlq_triage.py",
        "tests/test_echo_suppression.py",
        "tests/test_emit_on_graph_write.py",
        "tests/test_entity_merge_queue.py",
        "tests/test_entity_resolution_surface.py",
        "tests/test_entity_resolver.py",
        "tests/test_envelope_encryption_integration.py",
        "tests/test_envelope_read_consumers.py",
        "tests/test_event_bus_interface.py",
        "tests/test_event_log_concurrency.py",
        "tests/test_event_log_verification.py",
        "tests/test_event_retention.py",
        "tests/test_explain_memory.py",
        "tests/test_explain_past_decision.py",
        "tests/test_external_scope_rls.py",
        "tests/test_garbage_collector.py",
        "tests/test_governed_decorator.py",
        "tests/test_grounded_helper.py",
        "tests/test_health_probes.py",
        "tests/test_me_app.py",
        "tests/test_merge_queue_api.py",
        "tests/test_migration_003_quota_check.py",
        "tests/test_migration_004_event_sequences_backfill.py",
        "tests/test_node_ownership_registry.py",
        "tests/test_node_ownership_seed.py",
        "tests/test_outbox_idempotency.py",
        "tests/test_ownership_guard.py",
        "tests/test_pg_trgm_extension.py",
        "tests/test_price_resolution.py",
        "tests/test_pricing_surface.py",
        "tests/test_principal_sessions.py",
        "tests/test_procurement_bids.py",
        "tests/test_procurement_frontier.py",
        "tests/test_procurement_generate_po.py",
        "tests/test_procurement_graph.py",
        "tests/test_procurement_recalibration.py",
        "tests/test_procurement_submit_po.py",
        "tests/test_product_enrich.py",
        "tests/test_product_eol_watcher.py",
        "tests/test_product_golden_record.py",
        "tests/test_product_ingestion.py",
        "tests/test_product_matching.py",
        "tests/test_product_schema.py",
        "tests/test_project_advance.py",
        "tests/test_project_automation.py",
        "tests/test_project_baseline.py",
        "tests/test_project_convert.py",
        "tests/test_project_insights.py",
        "tests/test_project_pl.py",
        "tests/test_project_recall.py",
        "tests/test_project_sync_bom_tasks.py",
        "tests/test_replay_handlers_integration.py",
        "tests/test_sales_ai.py",
        "tests/test_sales_commission.py",
        "tests/test_sales_dealroom.py",
        "tests/test_sales_divergence.py",
        "tests/test_sales_flip.py",
        "tests/test_sales_graph.py",
        "tests/test_sales_public_quote.py",
        "tests/test_sales_read_model.py",
        "tests/test_sales_sign_to_project.py",
        "tests/test_sales_signed_baseline.py",
        "tests/test_sales_write_routing.py",
        "tests/test_schema_contract.py",
        "tests/test_shred_memory_integration.py",
        "tests/test_snapshot_mcp_handlers.py",
        "tests/test_source_mode_resolver.py",
        "tests/test_source_mode_table.py",
        "tests/test_survivorship.py",
        "tests/test_system_design_enrichment.py",
        "tests/test_system_design_from_quote.py",
        "tests/test_system_design_graph.py",
        "tests/test_system_design_netbox_bridge.py",
        "tests/test_system_design_phase1a.py",
        "tests/test_system_design_propose.py",
        "tests/test_system_design_to_quote.py",
        "tests/test_system_design_validation.py",
        "tests/test_tamper_anchor.py",
        "tests/test_vendors_cert_watcher.py",
        "tests/test_vendors_contractor_match.py",
        "tests/test_vendors_contractor_rls.py",
        "tests/test_vendors_frontier.py",
        "tests/test_vendors_partner_view.py",
        "tests/test_vendors_performance.py",
        "tests/test_vendors_procurement_feed.py",
        "tests/test_vendors_registry.py",
        "tests/test_vendors_scorecard.py",
        "tests/test_vendors_tiers.py",
        "tests/test_webhook_clientstate.py",
        "tests/test_worker_inflight_recovery.py",
        "tests/test_worm_db_enforcement.py",
        "tests/unit/test_project_case_study.py",
    }
)


def _has_integration_marker(path: Path) -> bool:
    """True when *path* contains a real integration marker.

    Deliberately AST-based rather than a substring search: a file that merely
    mentions the word, or documents the marker in a docstring, is not marked.
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    if "integration" not in src:
        return False
    if re.search(r"^pytestmark\s*=.*integration", src, re.M):
        return True
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for dec in node.decorator_list:
                if "pytest.mark.integration" in ast.unparse(dec):
                    return True
    return False


def _files_with_integration_markers() -> set[str]:
    return {
        p.relative_to(_REPO_ROOT).as_posix()
        for p in _TESTS_DIR.rglob("*.py")
        if _has_integration_marker(p)
    }


def _files_named_in_workflows() -> set[str]:
    text = "".join(
        f.read_text(encoding="utf-8", errors="replace") for f in _WORKFLOWS_DIR.glob("*.yml")
    )
    return set(re.findall(r"(tests/[A-Za-z0-9_/]+\.py)", text))


def test_every_integration_file_is_wired_or_explicitly_excluded() -> None:
    """A new integration file must gate something, or say out loud that it does not."""
    unaccounted = sorted(
        _files_with_integration_markers() - _files_named_in_workflows() - KNOWN_UNWIRED
    )
    assert not unaccounted, (
        "These files carry @pytest.mark.integration but are run by no CI workflow, "
        "so they gate nothing: " + ", ".join(unaccounted) + ". Either add them to the "
        "integration job in .github/workflows/ci.yml (preferred), or add them to "
        "KNOWN_UNWIRED in this file with a reason."
    )


def test_known_unwired_list_has_no_stale_entries() -> None:
    """Keep the backlog honest so it can only shrink.

    An entry is stale once the file is wired into a workflow, has lost its
    integration markers, or no longer exists.  Leaving stale entries in would let
    the list drift into a rubber stamp.
    """
    marked = _files_with_integration_markers()
    wired = _files_named_in_workflows()
    now_wired = sorted(KNOWN_UNWIRED & wired)
    no_longer_marked = sorted(e for e in KNOWN_UNWIRED if e not in marked)
    assert not now_wired, "Wired into a workflow now -- remove from KNOWN_UNWIRED: " + ", ".join(
        now_wired
    )
    assert not no_longer_marked, (
        "No longer carry integration markers (or no longer exist) -- remove from "
        "KNOWN_UNWIRED: " + ", ".join(no_longer_marked)
    )
