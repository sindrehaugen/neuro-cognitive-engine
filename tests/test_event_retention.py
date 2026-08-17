"""
tests/test_event_retention.py  — Batch 125: event-retention acceptance gate.

Exercises all four WORM/security contracts:
  1. An aged + fully-anchored partition is archived to MinIO and then DROPPED.
  2. An aged but UNANCHORED partition is KEPT (never dropped).
  3. A resolved contradiction past NCE_CONTRADICTION_RETENTION_DAYS is purged.
  4. A low-confidence non-sync kg_edge is reaped; a low-confidence 'sync' edge SURVIVES.

All tests are ``@pytest.mark.integration`` and require a live Postgres database
(``NCE_INTEGRATION_PG_DSN``) and a MinIO endpoint (``MINIO_ENDPOINT`` env vars).
When either is unavailable the suite is SKIPPED — the orchestrator runs Pass A.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure a 32-char dummy NCE_MASTER_KEY if conftest has not already set one
# (network-sandboxed sandbox won't reach the live stack, so tests skip, but
# import must succeed).
# ---------------------------------------------------------------------------
os.environ.setdefault("NCE_MASTER_KEY", "x" * 32)


# ---------------------------------------------------------------------------
# Helpers — MinIO mock factory
# ---------------------------------------------------------------------------


def _make_mock_minio(anchored_namespaces: dict[uuid.UUID, int]) -> MagicMock:
    """Return a sync MinIO client mock.

    Parameters
    ----------
    anchored_namespaces:
        Mapping of namespace_id → anchor_max_seq.  When a namespace is absent
        the mock simulates no anchor (returns empty object list).
    """
    client = MagicMock(name="MockMinioClient")

    # Track objects stored via put_object so tests can assert on them.
    stored: dict[str, bytes] = {}
    client._stored = stored

    def _list_objects(bucket: str, prefix: str = "", recursive: bool = False):
        """Return mock MinIO objects for the anchor prefix."""
        # Parse uuid from prefix like "<ns_id>/"
        parts = prefix.rstrip("/").split("/")
        try:
            ns_id = uuid.UUID(parts[0])
        except (ValueError, IndexError):
            return iter([])

        if ns_id not in anchored_namespaces:
            return iter([])

        max_seq = anchored_namespaces[ns_id]
        obj = MagicMock()
        obj.object_name = f"{ns_id}/{max_seq}.json"
        obj.is_dir = False
        return iter([obj])

    def _get_object(bucket: str, name: str):
        # Build the anchor JSON from the object_name.
        parts = name.split("/")
        try:
            ns_id = uuid.UUID(parts[0])
            seq = int(parts[1].replace(".json", ""))
        except (ValueError, IndexError):
            raise Exception(f"Unexpected object_name: {name}")
        blob = json.dumps(
            {
                "namespace_id": str(ns_id),
                "max_seq": seq,
                "chain_hash": "00" * 32,
                "anchored_at": datetime.now(timezone.utc).isoformat(),
            }
        ).encode()
        resp = MagicMock()
        resp.read.return_value = blob
        return resp

    def _put_object(bucket, name, data, length, **kwargs):
        content = data.read()
        client._stored[name] = content

    client.list_objects.side_effect = _list_objects
    client.get_object.side_effect = _get_object
    client.put_object.side_effect = _put_object
    client.bucket_exists.return_value = True

    return client


# ---------------------------------------------------------------------------
# Test: partition archive + drop (anchored)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anchored_aged_partition_is_archived_and_dropped(pg_pool, make_namespace):
    """An aged partition whose max_seq is fully anchored must be archived then dropped.

    Idempotency contract: this test DROP-and-recreates the partition at setup so
    ONLY the fresh namespace's events exist in it.  Teardown drops it again so it
    never lingers between runs.  Any prior-run namespace rows that might have been
    left in a leaked partition are eliminated by the fresh DROP before the test
    begins.
    """
    from nce.garbage_collector import run_partition_retention

    ns_id = await make_namespace()

    # Use a distinct far-past partition range for this test only.
    partition_name = "event_log_2017_03"

    # --- Setup: always drop-and-recreate so no foreign/leftover events remain. ---
    async with pg_pool.acquire() as conn:
        try:
            # Drop unconditionally first — ensures pristine state on re-run.
            await conn.execute(f"DROP TABLE IF EXISTS {partition_name}")
        except Exception as exc:
            pytest.skip(f"Cannot drop test partition {partition_name}: {exc}")
        try:
            await conn.execute(
                f"CREATE TABLE {partition_name} "
                "PARTITION OF event_log "
                "FOR VALUES FROM ('2017-03-01') TO ('2017-04-01')"
            )
        except Exception as exc:
            pytest.skip(f"Cannot create test partition {partition_name}: {exc}")

        # Insert exactly ONE event for THIS run's namespace — no other namespace
        # will have rows in this partition, so the anchor check will trivially pass.
        try:
            ev_id = uuid.uuid4()
            await conn.execute(
                f"""
                INSERT INTO {partition_name}
                    (id, namespace_id, agent_id, event_type, event_seq,
                     occurred_at, params, signature, signature_key_id, signature_version)
                VALUES ($1, $2, 'test-agent', 'store_memory', 9999999,
                        '2017-03-15 12:00:00+00', '{{}}', '\\x00'::bytea,
                        'test-key', 2)
                """,
                ev_id,
                ns_id,
            )
        except Exception as exc:
            # Clean up before skipping so we don't leave the table behind.
            try:
                await conn.execute(f"DROP TABLE IF EXISTS {partition_name}")
            except Exception:
                pass
            pytest.skip(f"Cannot insert into partition {partition_name}: {exc}")

    # Anchor covers max_seq=9999999 for this (and only this) namespace.
    minio = _make_mock_minio({ns_id: 9999999})

    try:
        result = await run_partition_retention(pg_pool, minio, retention_months=1)

        # The partition must have been archived (manifest written) and dropped.
        assert result["dropped"] >= 1, f"Expected partition to be dropped; got result={result}"
        assert result["archived"] >= 1, (
            f"Expected at least one archive manifest; got result={result}"
        )
        # skipped_unanchored may be > 0 for OTHER old partitions on the DB that happen
        # to be unanchored — we only assert that THIS run's partition was processed.
        # The dropped >= 1 check above proves the fully-anchored path ran.

        # Manifest should be in MinIO mock storage.
        archive_keys = [k for k in minio._stored if "event_log_archive" in k]
        assert archive_keys, "Expected archive manifest blob in MinIO"

        # The partition table must no longer exist (run_partition_retention dropped it).
        async with pg_pool.acquire() as conn:
            still_exists = await conn.fetchval(
                "SELECT to_regclass('public.' || $1)::text",
                partition_name,
            )
        assert still_exists is None, (
            f"Partition {partition_name} should have been dropped but still exists"
        )
    finally:
        # Teardown guard: drop the partition if it was not dropped by the retention
        # run (e.g. the test failed mid-way) so it never lingers across test runs.
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {partition_name}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test: UNANCHORED aged partition is KEPT
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unanchored_aged_partition_is_kept(pg_pool, make_namespace):
    """An aged partition with no anchor must NOT be dropped."""
    from nce.garbage_collector import run_partition_retention

    ns_id = await make_namespace()
    partition_name = "event_log_2019_06"

    async with pg_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('public.' || $1)::text",
            partition_name,
        )
        if exists is None:
            try:
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {partition_name} "
                    "PARTITION OF event_log "
                    "FOR VALUES FROM ('2019-06-01') TO ('2019-07-01')"
                )
            except Exception as exc:
                pytest.skip(f"Cannot create test partition {partition_name}: {exc}")

        try:
            ev_id = uuid.uuid4()
            await conn.execute(
                f"""
                INSERT INTO {partition_name}
                    (id, namespace_id, agent_id, event_type, event_seq,
                     occurred_at, params, signature, signature_key_id, signature_version)
                VALUES ($1, $2, 'test-agent', 'store_memory', 8888888,
                        '2019-06-15 12:00:00+00', '{{}}', '\\x00'::bytea,
                        'test-key', 2)
                ON CONFLICT DO NOTHING
                """,
                ev_id,
                ns_id,
            )
        except Exception as exc:
            pytest.skip(f"Cannot insert into partition {partition_name}: {exc}")

    # No anchor for this namespace → partition must be kept.
    minio = _make_mock_minio({})  # empty — no anchors

    result = await run_partition_retention(pg_pool, minio, retention_months=1)

    assert result["skipped_unanchored"] >= 1, (
        f"Expected at least one unanchored skip; got result={result}"
    )

    # Partition must still exist.
    async with pg_pool.acquire() as conn:
        still_exists = await conn.fetchval(
            "SELECT to_regclass('public.' || $1)::text",
            partition_name,
        )
    assert still_exists is not None, (
        f"Partition {partition_name} was dropped but should have been kept (unanchored)"
    )


# ---------------------------------------------------------------------------
# Test: resolved contradiction past TTL is purged
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolved_contradiction_past_ttl_is_purged(pg_pool, make_namespace):
    """A resolved contradiction older than retention_days must be deleted.

    Schema notes (verified against nce/schema.sql):
      memories: PRIMARY KEY (id, created_at); payload_ref TEXT NOT NULL with
        CHECK ck_payload_ref_objectid_format (^[a-f0-9]{24}$); agent_id NOT NULL.
      contradictions: PRIMARY KEY (id, detected_at); NOT NULL columns are
        namespace_id, memory_a_id, memory_b_id, agent_id (default 'system'),
        detection_path, signals (JSONB), confidence (REAL); detection_path and
        signals have no DB default so they must be supplied explicitly.
    """
    from nce.garbage_collector import run_contradiction_retention

    ns_id = await make_namespace()

    # ---------------------------------------------------------------------------
    # 1. Insert two minimal memories for this namespace.
    #    payload_ref must satisfy ^[a-f0-9]{24}$ — use the lower-hex of a UUID
    #    trimmed to 24 chars (hex(UUID) is 32 chars; first 24 are fine as they are
    #    pure hex).
    # ---------------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    mem_a = uuid.uuid4()
    mem_b = uuid.uuid4()
    mem_a_ref = mem_a.hex[:24]  # 24-char lowercase hex — satisfies MongoDB ObjectId constraint
    mem_b_ref = mem_b.hex[:24]

    async with pg_pool.acquire() as conn:
        await conn.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
        await conn.execute(
            """
            INSERT INTO memories
                (id, created_at, namespace_id, agent_id, payload_ref,
                 memory_type, assertion_type, change_origin)
            VALUES
                ($1, $5, $3, 'test-agent', $4, 'episodic', 'fact', 'agent'),
                ($2, $5, $3, 'test-agent', $6, 'episodic', 'fact', 'agent')
            ON CONFLICT DO NOTHING
            """,
            mem_a,
            mem_b,
            ns_id,
            mem_a_ref,
            now,
            mem_b_ref,
        )

    # ---------------------------------------------------------------------------
    # 2. Insert an OLD resolved contradiction (400 days ago — past 180-day TTL).
    # ---------------------------------------------------------------------------
    old_resolved = now - timedelta(days=400)
    contr_id = uuid.uuid4()

    async with pg_pool.acquire() as conn:
        await conn.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
        await conn.execute(
            """
            INSERT INTO contradictions
                (id, detected_at, namespace_id, memory_a_id, memory_b_id,
                 agent_id, detection_path, signals, confidence,
                 resolution, resolved_at, resolved_by)
            VALUES ($1, $2, $3, $4, $5,
                    'test-agent', 'test/path', '{}'::jsonb, 0.9,
                    'accepted', $2, 'test')
            ON CONFLICT DO NOTHING
            """,
            contr_id,
            old_resolved,
            ns_id,
            mem_a,
            mem_b,
        )

    # ---------------------------------------------------------------------------
    # 3. Insert a FRESH resolved contradiction (10 days ago — within TTL).
    # ---------------------------------------------------------------------------
    fresh_resolved = now - timedelta(days=10)
    fresh_contr_id = uuid.uuid4()

    async with pg_pool.acquire() as conn:
        await conn.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
        await conn.execute(
            """
            INSERT INTO contradictions
                (id, detected_at, namespace_id, memory_a_id, memory_b_id,
                 agent_id, detection_path, signals, confidence,
                 resolution, resolved_at, resolved_by)
            VALUES ($1, $2, $3, $4, $5,
                    'test-agent', 'test/path', '{}'::jsonb, 0.9,
                    'accepted', $2, 'test')
            ON CONFLICT DO NOTHING
            """,
            fresh_contr_id,
            fresh_resolved,
            ns_id,
            mem_a,
            mem_b,
        )

    # ---------------------------------------------------------------------------
    # 4. Run production purge and assert the old row is gone, fresh row survives.
    # ---------------------------------------------------------------------------
    deleted = await run_contradiction_retention(pg_pool, [ns_id], retention_days=180)

    assert deleted >= 1, f"Expected at least 1 contradiction purged but got {deleted}"

    # The fresh contradiction must survive.
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        fresh_count = await conn.fetchval(
            "SELECT count(*) FROM contradictions WHERE namespace_id = $1::uuid AND id = $2::uuid",
            ns_id,
            fresh_contr_id,
        )
    assert fresh_count == 1, "Fresh resolved contradiction was incorrectly purged"

    # The aged contradiction must be gone.
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        old_count = await conn.fetchval(
            "SELECT count(*) FROM contradictions WHERE namespace_id = $1::uuid AND id = $2::uuid",
            ns_id,
            contr_id,
        )
    assert old_count == 0, "Aged resolved contradiction was NOT purged — retention logic broken"


# ---------------------------------------------------------------------------
# Test: low-confidence non-sync edge is reaped; sync edge survives
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_edge_prune_respects_sync_origin(pg_pool, make_namespace):
    """Low-confidence non-sync edges are reaped; change_origin='sync' edges survive."""
    from nce.garbage_collector import run_edge_prune

    ns_id = await make_namespace()
    now = datetime.now(timezone.utc)
    old_updated = now - timedelta(days=200)

    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        # Reapable edge: low confidence + old + non-sync origin.
        reap_id = uuid.uuid4()
        try:
            await conn.execute(
                """
                INSERT INTO kg_edges
                    (id, namespace_id, subject_label, predicate, object_label,
                     confidence, change_origin, created_at, updated_at)
                VALUES ($1, $2, 'SubjectA', 'relates_to', 'ObjectA',
                        0.05, 'agent', $3, $4)
                ON CONFLICT DO NOTHING
                """,
                reap_id,
                ns_id,
                old_updated,
                old_updated,
            )
        except Exception as exc:
            pytest.skip(f"Cannot insert reapable kg_edge: {exc}")

        # Surviving edge: low confidence + old + sync origin (Batch 106 invariant).
        sync_id = uuid.uuid4()
        try:
            await conn.execute(
                """
                INSERT INTO kg_edges
                    (id, namespace_id, subject_label, predicate, object_label,
                     confidence, change_origin, created_at, updated_at)
                VALUES ($1, $2, 'SubjectB', 'relates_to', 'ObjectB',
                        0.05, 'sync', $3, $4)
                ON CONFLICT DO NOTHING
                """,
                sync_id,
                ns_id,
                old_updated,
                old_updated,
            )
        except Exception as exc:
            pytest.skip(f"Cannot insert sync kg_edge: {exc}")

    deleted = await run_edge_prune(pg_pool, [ns_id], prune_age_days=90)

    assert deleted >= 1, f"Expected at least 1 edge pruned but got {deleted}"

    # The sync edge must still be present.
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        sync_count = await conn.fetchval(
            "SELECT count(*) FROM kg_edges WHERE namespace_id = $1::uuid AND id = $2::uuid",
            ns_id,
            sync_id,
        )
    assert sync_count == 1, (
        "Low-confidence 'sync' kg_edge was incorrectly pruned — Batch 106 invariant violated"
    )

    # The reapable non-sync edge must be gone.
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        reap_count = await conn.fetchval(
            "SELECT count(*) FROM kg_edges WHERE namespace_id = $1::uuid AND id = $2::uuid",
            ns_id,
            reap_id,
        )
    assert reap_count == 0, (
        "Low-confidence non-sync kg_edge should have been pruned but still exists"
    )
