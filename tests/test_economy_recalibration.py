"""
tests/test_economy_recalibration.py
====================================
Tests for ``nce.vertical_modules.economy.recalibration`` (Module 8 Wave 11).

Pure-logic tests (no DB, no filesystem) verify:
- The precision-earned movement formula (``_derive_candidate_thresholds``).
- The normalised-key collision-avoidance helpers (``_overrides_without``,
  ``_find_override_entry``).
- **The validate-and-floor guard** (``_validate_candidate_thresholds``) — these would
  FAIL if the guard's internal ``matching._resolve_thresholds`` call were removed or
  bypassed (proven by hand: commenting out that call and re-running these tests turns
  them red — see the batch report).

Integration tests (``@pytest.mark.integration``, live Postgres) verify:
- ``do_record_match_decision`` appends to ``v3_cognitive_ledger`` (append-only).
- Recalibration does NOT fire below N decisions, and does NOT touch the config file.
- Recalibration fires at N=100 and persists a validated override.
- A candidate that would breach the runtime floor is REJECTED and leaves the config
  file byte-for-byte unchanged (and writes no audit-ledger row either).
- The N-decision window is auditor-reconstructable: the audit row's
  ``window_ledger_ids`` names the exact rows, independent of what is inserted later.
- RLS isolates ``v3_cognitive_ledger`` rows between namespaces when queried as
  ``nce_app`` (the ``pg_app_conn`` fixture) with NO explicit namespace filter — proving
  the database-level guarantee, not just this module's own explicit WHERE clause.

Every test that exercises ``do_recalibrate_supplier`` passes an explicit
``config_path`` pointed at a ``tmp_path`` file — this suite never reads or writes the
tracked ``nce/config_data/economy-match-thresholds.json``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.vertical_modules.economy.recalibration import (
    _advisory_lock_key,
    _derive_candidate_thresholds,
    _find_override_entry,
    _overrides_without,
    _validate_candidate_thresholds,
    do_recalibrate_supplier,
    do_record_match_decision,
)

# Small window so most tests run quickly without inserting 100 rows each.
_TEST_WINDOW = 5
# The Acceptance line's literal N — exercised once, end-to-end.
_FULL_WINDOW = 100


def _base_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"green": 115, "yellow": 70, "supplier_overrides": {}}
    cfg.update(overrides)
    return cfg


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config), encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure-logic unit tests — no DB, no filesystem, no @pytest.mark.integration.
# ---------------------------------------------------------------------------


class TestDeriveCandidateThresholds:
    def test_precision_one_loosens_by_max_delta(self) -> None:
        new_green, new_yellow, delta = _derive_candidate_thresholds(115, 70, precision=1.0)
        assert delta == pytest.approx(10.0)
        assert new_green == 105
        assert new_yellow == 60

    def test_precision_zero_tightens_by_max_delta(self) -> None:
        new_green, new_yellow, delta = _derive_candidate_thresholds(115, 70, precision=0.0)
        assert delta == pytest.approx(-10.0)
        assert new_green == 125
        assert new_yellow == 80

    def test_midpoint_precision_no_movement(self) -> None:
        new_green, new_yellow, delta = _derive_candidate_thresholds(115, 70, precision=0.5)
        assert delta == pytest.approx(0.0)
        assert new_green == 115
        assert new_yellow == 70

    def test_gap_width_is_preserved(self) -> None:
        """Both cutoffs shift by the same delta — the review band's width never changes."""
        new_green, new_yellow, _ = _derive_candidate_thresholds(115, 70, precision=0.8)
        assert new_green - new_yellow == 115 - 70


class TestOverridesWithoutAndFindEntry:
    def test_overrides_without_removes_padded_duplicate(self) -> None:
        overrides = {
            "123 ": {"green": 100, "yellow": 60},
            "456": {"green": 90, "yellow": 50},
        }
        result = _overrides_without(overrides, "123")
        assert result == {"456": {"green": 90, "yellow": 50}}

    def test_overrides_without_leaves_unrelated_entries(self) -> None:
        overrides = {"456": {"green": 90, "yellow": 50}}
        assert _overrides_without(overrides, "123") == overrides

    def test_find_override_entry_matches_normalised_key(self) -> None:
        overrides = {" 123": {"green": 100, "yellow": 60}}
        assert _find_override_entry(overrides, "123") == {"green": 100, "yellow": 60}

    def test_find_override_entry_returns_none_when_absent(self) -> None:
        assert _find_override_entry({"456": {}}, "999") is None


class TestValidateCandidateThresholds:
    """These tests would FAIL RED if ``_validate_candidate_thresholds`` stopped calling
    ``matching._resolve_thresholds`` (e.g. if that call were commented out or replaced
    with a no-op) — confirmed by hand during development, then reverted."""

    def test_accepts_valid_candidate(self) -> None:
        resolved = _validate_candidate_thresholds(_base_config(), "123", green=105, yellow=60)
        assert resolved == {"green": 105, "yellow": 60}

    def test_rejects_floor_breach(self) -> None:
        with pytest.raises(ValueError, match="green must be"):
            _validate_candidate_thresholds(_base_config(), "123", green=55, yellow=50)

    def test_rejects_non_positive_yellow(self) -> None:
        with pytest.raises(ValueError, match="green must be"):
            _validate_candidate_thresholds(_base_config(), "123", green=150, yellow=0)

    def test_rejects_inverted_pair(self) -> None:
        # green(65) > _MIN_GREEN(60) so the floor clause does NOT fire here —
        # this isolates the green >= yellow clause specifically.
        with pytest.raises(ValueError, match="green must be"):
            _validate_candidate_thresholds(_base_config(), "123", green=65, yellow=90)

    def test_does_not_disturb_other_suppliers_overrides(self) -> None:
        config = _base_config(supplier_overrides={"999": {"green": 120, "yellow": 80}})
        resolved = _validate_candidate_thresholds(config, "123", green=105, yellow=60)
        assert resolved == {"green": 105, "yellow": 60}
        # The trial dict must not have mutated the caller's config in place.
        assert config["supplier_overrides"] == {"999": {"green": 120, "yellow": 80}}


class TestAdvisoryLockKey:
    """Pure-logic tests for the Fix 2 (Batch 126 round 2) advisory-lock key
    derivation — no DB required, so these run even where the integration suite's
    ``pg_pool`` fixture would skip."""

    def test_key_is_deterministic_for_the_same_path(self, tmp_path: Path) -> None:
        p = tmp_path / "economy-match-thresholds.json"
        assert _advisory_lock_key(p) == _advisory_lock_key(p)

    def test_key_differs_for_different_paths(self, tmp_path: Path) -> None:
        p1 = tmp_path / "a.json"
        p2 = tmp_path / "b.json"
        assert _advisory_lock_key(p1) != _advisory_lock_key(p2)

    def test_key_is_stable_across_relative_and_resolved_forms(self, tmp_path: Path) -> None:
        """Keyed on the RESOLVED path -- a caller passing ``"./x.json"`` from a
        different CWD must still collide with one passing the absolute form,
        or two config_path spellings of the same file could dodge the lock."""
        p = tmp_path / "economy-match-thresholds.json"
        assert _advisory_lock_key(p) == _advisory_lock_key(Path(str(p.resolve())))

    def test_key_fits_signed_bigint(self, tmp_path: Path) -> None:
        """``pg_advisory_lock`` takes a signed 64-bit bigint -- the key must fit."""
        key = _advisory_lock_key(tmp_path / "economy-match-thresholds.json")
        assert -(2**63) <= key < 2**63


# ---------------------------------------------------------------------------
# Integration tests — live Postgres required.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestEconomyRecalibration:
    @pytest_asyncio.fixture
    async def ns_a(self, make_namespace: Any) -> uuid.UUID:
        return await make_namespace()

    @pytest_asyncio.fixture
    async def ns_b(self, make_namespace: Any) -> uuid.UUID:
        return await make_namespace()

    async def _count_decision_rows(
        self, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, supplier_orgnr: str
    ) -> int:
        async with pg_pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM   v3_cognitive_ledger
                WHERE  namespace_id = $1::uuid
                  AND  tlx_scores->>'event_type' = 'economy_match_decision'
                  AND  tlx_scores->>'supplier_orgnr' = $2
                """,
                str(namespace_id),
                supplier_orgnr,
            )

    async def _fetch_recal_audit_rows(
        self, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, supplier_orgnr: str
    ) -> list[dict[str, Any]]:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, tlx_scores
                FROM   v3_cognitive_ledger
                WHERE  namespace_id = $1::uuid
                  AND  tlx_scores->>'event_type' = 'economy_recalibration'
                  AND  tlx_scores->>'supplier_orgnr' = $2
                ORDER BY created_at DESC
                """,
                str(namespace_id),
                supplier_orgnr,
            )
        return [
            {
                "id": str(r["id"]),
                "tlx_scores": r["tlx_scores"]
                if isinstance(r["tlx_scores"], dict)
                else json.loads(r["tlx_scores"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # do_record_match_decision
    # ------------------------------------------------------------------

    async def test_record_decision_appends_to_ledger(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        supplier = f"orgnr-{uuid.uuid4().hex[:8]}"
        before = await self._count_decision_rows(pg_pool, ns_a, supplier)

        result = await do_record_match_decision(
            pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=150, tier="GREEN"
        )

        after = await self._count_decision_rows(pg_pool, ns_a, supplier)
        assert after == before + 1
        assert result["supplier_orgnr"] == supplier
        assert "ledger_id" in result

    async def test_record_decision_rejects_invalid_decision(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        with pytest.raises(ValueError, match="decision must be"):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr="sup-x", decision="approve", score=100
            )

    async def test_record_decision_rejects_invalid_tier(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        with pytest.raises(ValueError, match="tier must be"):
            await do_record_match_decision(
                pg_pool,
                ns_a,
                supplier_orgnr="sup-x",
                decision="accept",
                score=100,
                tier="BLUE",
            )

    # ------------------------------------------------------------------
    # do_recalibrate_supplier — below-window skip
    # ------------------------------------------------------------------

    async def test_no_recalibration_below_window_and_file_untouched(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        supplier = f"999{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        original = _base_config()
        _write_config(config_path, original)

        for _ in range(_TEST_WINDOW - 1):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=150
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_orgnr=supplier,
            window_n=_TEST_WINDOW,
            config_path=config_path,
        )

        assert result["recalibrated"] is False
        assert result["decision_count"] == _TEST_WINDOW - 1
        assert result["green"] is None
        assert json.loads(config_path.read_text()) == original

    # ------------------------------------------------------------------
    # do_recalibrate_supplier — fires at N, persists a valid override
    # ------------------------------------------------------------------

    async def test_recalibration_fires_at_full_n_and_persists_override(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        """Exercises the Acceptance line's literal N=100 window, end-to-end."""
        supplier = f"777{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        _write_config(config_path, _base_config())

        for i in range(_FULL_WINDOW):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=140 + (i % 5)
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_orgnr=supplier,
            window_n=_FULL_WINDOW,
            config_path=config_path,
        )

        assert result["recalibrated"] is True
        assert result["decision_count"] == _FULL_WINDOW
        assert result["precision"] == pytest.approx(1.0)
        assert result["green"] == 105  # 115 - 10 (max loosening delta)
        assert result["yellow"] == 60  # 70 - 10
        assert len(result["window_ledger_ids"]) == _FULL_WINDOW

        on_disk = json.loads(config_path.read_text())
        assert on_disk["supplier_overrides"][supplier] == {"green": 105, "yellow": 60}
        # Untouched top-level defaults and no stray keys introduced.
        assert on_disk["green"] == 115
        assert on_disk["yellow"] == 70

    async def test_mixed_decisions_precision_formula(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        supplier = f"555{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        _write_config(config_path, _base_config())

        # 3 accept + 2 override out of 5 -> precision 0.6
        for _ in range(3):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=140
            )
        for _ in range(2):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="override", score=80
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_orgnr=supplier,
            window_n=_TEST_WINDOW,
            config_path=config_path,
        )

        assert result["recalibrated"] is True
        assert result["precision"] == pytest.approx(0.6)
        # (0.6 - 0.5) * 20 = 2.0
        assert result["threshold_delta"] == pytest.approx(2.0)
        assert result["green"] == 113
        assert result["yellow"] == 68

    async def test_recalibration_compounds_on_existing_override(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        """A second recalibration round starts from the supplier's EXISTING override,
        not the top-level defaults — proving the per-supplier band actually evolves."""
        supplier = f"333{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        _write_config(
            config_path,
            _base_config(supplier_overrides={supplier: {"green": 105, "yellow": 60}}),
        )

        for _ in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=140
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_orgnr=supplier,
            window_n=_TEST_WINDOW,
            config_path=config_path,
        )

        assert result["recalibrated"] is True
        assert result["previous_green"] == 105
        assert result["previous_yellow"] == 60
        assert result["green"] == 95
        assert result["yellow"] == 50

    # ------------------------------------------------------------------
    # The one thing this wave must get right: validate-and-floor at the write site
    # ------------------------------------------------------------------

    async def test_recalibration_refuses_floor_breach_and_leaves_file_untouched(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        """A supplier already tightened close to the floor (green=65) would breach it
        on a full-trust (precision=1.0) round. The write must be refused loudly, the
        config file must come back byte-for-byte identical, and no audit row is written.
        """
        supplier = f"888{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        seeded_override = {"green": 65, "yellow": 60}
        original = _base_config(supplier_overrides={supplier: dict(seeded_override)})
        _write_config(config_path, original)
        original_bytes = config_path.read_bytes()

        for _ in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=180, tier="GREEN"
            )

        with pytest.raises(ValueError, match="REJECTED"):
            await do_recalibrate_supplier(
                pg_pool,
                ns_a,
                supplier_orgnr=supplier,
                window_n=_TEST_WINDOW,
                config_path=config_path,
            )

        # File is untouched -- not merely "logically equal", byte-for-byte identical.
        assert config_path.read_bytes() == original_bytes
        reloaded = json.loads(config_path.read_text())
        assert reloaded["supplier_overrides"][supplier] == seeded_override

        # No partially-applied state anywhere: the audit ledger has zero rows too.
        audit_rows = await self._fetch_recal_audit_rows(pg_pool, ns_a, supplier)
        assert audit_rows == []

    # ------------------------------------------------------------------
    # Batch 126 round 2 fix-forward: Fix 1 -- ledger-before-file ordering, and
    # its failure semantics are DETECTABLE rather than a silent gap.
    # ------------------------------------------------------------------

    async def test_ledger_row_survives_a_failed_file_write_and_is_detectable(
        self,
        pg_pool: asyncpg.Pool,
        ns_a: uuid.UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The audit-ledger row is now written BEFORE the config file. Forcing the
        file write (the new *second* step) to fail must leave: (a) the ledger row
        declaring the intended change, durably committed, and (b) the on-disk
        config still at its OLD baseline. Comparing the two immediately reveals a
        declared-but-never-applied change -- there is no longer a silent,
        unrecoverable gap the way there was when the file write came first.
        """
        supplier = f"666{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        original = _base_config()
        _write_config(config_path, original)

        for _ in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=150
            )

        def _boom(path: Path, config: dict[str, Any]) -> None:
            raise OSError("simulated disk failure writing economy-match-thresholds.json")

        monkeypatch.setattr(
            "nce.vertical_modules.economy.recalibration._write_config_atomic", _boom
        )

        with pytest.raises(OSError, match="simulated disk failure"):
            await do_recalibrate_supplier(
                pg_pool,
                ns_a,
                supplier_orgnr=supplier,
                window_n=_TEST_WINDOW,
                config_path=config_path,
            )

        # The file write failed -- the config must still show the OLD baseline,
        # not a partially-applied or silently-adopted one.
        on_disk = json.loads(config_path.read_text())
        assert on_disk == original
        assert supplier not in on_disk["supplier_overrides"]

        # But the ledger row declaring the INTENDED change survived the failure --
        # this is the detectable trace the old (file-first) ordering could never
        # produce (that ordering had zero ledger rows here, and an untracked,
        # unrecoverable config mutation instead).
        audit_rows = await self._fetch_recal_audit_rows(pg_pool, ns_a, supplier)
        assert len(audit_rows) == 1
        declared = audit_rows[0]["tlx_scores"]
        assert declared["green"] == 105  # precision=1.0 -> 115 - 10 (max loosening delta)
        assert declared["yellow"] == 60

        # The detection itself: the ledger's declared cutoffs do not match a live
        # on-disk override for this supplier (there is none) -- an auditor
        # comparing the latest ledger row per supplier against the config file
        # surfaces exactly this mismatch, rather than finding nothing at all.
        live_override = on_disk["supplier_overrides"].get(supplier)
        assert live_override is None

    # ------------------------------------------------------------------
    # Batch 126 round 2 fix-forward: Fix 2 -- concurrent recalibrations for
    # DIFFERENT suppliers sharing one config file must not lose an update, and
    # the surviving ledger rows must not misdescribe the live config.
    # ------------------------------------------------------------------

    async def test_concurrent_recalibrations_for_different_suppliers_do_not_clobber(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        """Two REAL concurrent ``do_recalibrate_supplier`` calls for different
        suppliers, sharing one config file. Before Fix 2, the second writer's
        blind ``dict(current_config)`` merge (built from a pre-write snapshot)
        silently erased the first writer's override while that first writer's own
        ledger row kept asserting a config that was no longer live. The advisory
        lock (``_locked_config_path``) now serializes the two calls and forces each
        one to re-read the file only after acquiring the lock, so the loser of the
        race always merges on top of the winner's fresh write instead of a stale
        snapshot.
        """
        supplier_a = f"711{uuid.uuid4().hex[:6]}"
        supplier_b = f"712{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        _write_config(config_path, _base_config())

        for supplier in (supplier_a, supplier_b):
            for _ in range(_TEST_WINDOW):
                await do_record_match_decision(
                    pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=150
                )

        result_a, result_b = await asyncio.gather(
            do_recalibrate_supplier(
                pg_pool,
                ns_a,
                supplier_orgnr=supplier_a,
                window_n=_TEST_WINDOW,
                config_path=config_path,
            ),
            do_recalibrate_supplier(
                pg_pool,
                ns_a,
                supplier_orgnr=supplier_b,
                window_n=_TEST_WINDOW,
                config_path=config_path,
            ),
        )

        assert result_a["recalibrated"] is True
        assert result_b["recalibrated"] is True

        on_disk = json.loads(config_path.read_text())
        overrides = on_disk["supplier_overrides"]

        # BOTH overrides must survive -- neither writer's merge erased the other's.
        assert overrides[supplier_a] == {"green": 105, "yellow": 60}
        assert overrides[supplier_b] == {"green": 105, "yellow": 60}

        # And neither ledger row now "lies" about the live config -- each
        # supplier's ledger-declared cutoffs match what actually ended up on disk
        # for it (the exact defect the audit reproduced: an erased supplier's
        # ledger row still asserting green=105 yellow=60 after being clobbered).
        rows_a = await self._fetch_recal_audit_rows(pg_pool, ns_a, supplier_a)
        rows_b = await self._fetch_recal_audit_rows(pg_pool, ns_a, supplier_b)
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0]["tlx_scores"]["green"] == overrides[supplier_a]["green"]
        assert rows_a[0]["tlx_scores"]["yellow"] == overrides[supplier_a]["yellow"]
        assert rows_b[0]["tlx_scores"]["green"] == overrides[supplier_b]["green"]
        assert rows_b[0]["tlx_scores"]["yellow"] == overrides[supplier_b]["yellow"]

    # ------------------------------------------------------------------
    # Auditor-reconstructable N-decision window
    # ------------------------------------------------------------------

    async def test_auditor_reconstructs_window_from_ledger(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, tmp_path: Path
    ) -> None:
        supplier = f"222{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        _write_config(config_path, _base_config())

        recorded_ids: list[str] = []
        for i in range(_TEST_WINDOW):
            decision = "accept" if i % 5 != 0 else "override"  # 4 accept + 1 override
            outcome = await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision=decision, score=130
            )
            recorded_ids.append(outcome["ledger_id"])

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_orgnr=supplier,
            window_n=_TEST_WINDOW,
            config_path=config_path,
        )
        assert result["recalibrated"] is True

        audit_rows = await self._fetch_recal_audit_rows(pg_pool, ns_a, supplier)
        assert len(audit_rows) == 1
        window_ledger_ids = audit_rows[0]["tlx_scores"]["window_ledger_ids"]

        # Exactly the ids this test itself recorded -- nothing more, nothing less.
        assert set(window_ledger_ids) == set(recorded_ids)
        assert len(window_ledger_ids) == _TEST_WINDOW

        # An auditor re-derives precision purely from those ids (ANY(...) lookup),
        # independent of do_recalibrate_supplier's internals, and gets the same answer.
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tlx_scores FROM v3_cognitive_ledger WHERE id = ANY($1::uuid[])",
                [uuid.UUID(i) for i in window_ledger_ids],
            )
        payloads = [
            (json.loads(r["tlx_scores"]) if isinstance(r["tlx_scores"], str) else r["tlx_scores"])
            for r in rows
        ]
        auditor_accepted = sum(1 for p in payloads if p.get("decision") == "accept")
        auditor_precision = auditor_accepted / len(payloads)
        assert auditor_precision == pytest.approx(result["precision"])

        # This reconstruction is stable even after MORE decisions are recorded later --
        # a naive "last N for this supplier" re-query would NOT be (it would shift).
        await do_record_match_decision(
            pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=130
        )
        async with pg_pool.acquire() as conn:
            rows_again = await conn.fetch(
                "SELECT tlx_scores FROM v3_cognitive_ledger WHERE id = ANY($1::uuid[])",
                [uuid.UUID(i) for i in window_ledger_ids],
            )
        assert len(rows_again) == _TEST_WINDOW

    # ------------------------------------------------------------------
    # Namespace scoping / RLS
    # ------------------------------------------------------------------

    async def test_recalibration_scoped_to_namespace(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, ns_b: uuid.UUID, tmp_path: Path
    ) -> None:
        """ns_b has zero decisions for this supplier -- recalibration must not fire,
        even though ns_a has a full window for the SAME supplier_orgnr string."""
        supplier = f"444{uuid.uuid4().hex[:6]}"
        config_path = tmp_path / "economy-match-thresholds.json"
        _write_config(config_path, _base_config())

        for _ in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=140
            )

        result_b = await do_recalibrate_supplier(
            pg_pool,
            ns_b,
            supplier_orgnr=supplier,
            window_n=_TEST_WINDOW,
            config_path=config_path,
        )

        assert result_b["recalibrated"] is False
        assert result_b["decision_count"] == 0

    async def test_rls_isolates_ledger_rows_between_namespaces(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        ns_a: uuid.UUID,
        ns_b: uuid.UUID,
    ) -> None:
        """Connects as nce_app (never the superuser pool) and queries with NO explicit
        namespace filter, so a pass here proves the DATABASE's FORCE RLS policy isolates
        v3_cognitive_ledger rows -- not merely this module's own WHERE namespace_id=..."""
        supplier = f"111{uuid.uuid4().hex[:6]}"

        await do_record_match_decision(
            pg_pool, ns_a, supplier_orgnr=supplier, decision="accept", score=150
        )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            visible_from_b = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM v3_cognitive_ledger WHERE tlx_scores->>'supplier_orgnr' = $1",
                supplier,
            )
        assert visible_from_b == 0, "ns_b must not see ns_a's match-decision row"

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            visible_from_a = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM v3_cognitive_ledger WHERE tlx_scores->>'supplier_orgnr' = $1",
                supplier,
            )
        assert visible_from_a == 1, "ns_a must see its own match-decision row"
