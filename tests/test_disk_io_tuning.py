"""Disk-I/O tuning acceptance (NCE_MASTER_PLAN VI.5c, Batch 62).

Structural assertions over the parsed ``docker-compose.yml`` plus the
``halfvec(768)`` storage-format change in ``nce/schema.sql`` and migration 019:

* D1 — Postgres ``command:`` carries the WAL/compression/checkpoint tuning
  flags, and ``synchronous_commit`` is NOT turned off (WORM ``event_log``
  durability must stay ON);
* D1 — Mongo runs the zstd WiredTiger collection block compressor;
* D2 — the fixed-dimension embedding columns are ``halfvec(768)`` in both
  ``schema.sql`` and migration 019, and the HNSW indexes use
  ``halfvec_cosine_ops`` (mirror is consistent);
* D4 — a RAM-backed tmpfs staging mount is wired and ``NCE_ARTIFACT_STAGING_DIR``
  points at it on the compute-bearing services;
* D7 — container log rotation (``max-size``/``max-file``) is set.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_SCHEMA = _REPO_ROOT / "nce" / "schema.sql"
_MIGRATION = _REPO_ROOT / "nce" / "migrations" / "019_halfvec_embeddings.sql"

# Services that stage/extract artifacts and so must get the tmpfs staging mount.
_STAGING_SERVICES = ("worker", "cron", "admin", "a2a", "webhook-receiver")
# Every service that pins a json-file log-rotation policy (D7).
_ROTATED_SERVICES = (
    "postgres",
    "mongodb",
    "worker",
    "cron",
    "admin",
    "a2a",
    "webhook-receiver",
)
_TMPFS_PATH = "/dev/shm/nce-staging"


def _load() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _command(service: dict) -> list[str]:
    cmd = service.get("command", [])
    if isinstance(cmd, str):
        return cmd.split()
    return list(cmd)


# --- D1: Postgres / Mongo datastore tuning ---------------------------------


def test_postgres_wal_and_compression_tuning() -> None:
    pg = _load()["services"]["postgres"]
    cmd = " ".join(_command(pg))
    assert "shared_buffers=" in cmd, "PG shared_buffers tuning missing"
    assert "maintenance_work_mem=" in cmd, "PG maintenance_work_mem tuning missing"
    assert "wal_compression=on" in cmd, "PG wal_compression=on missing"
    assert "checkpoint_completion_target=0.9" in cmd, "PG checkpoint target missing"
    assert "max_wal_size=" in cmd, "PG max_wal_size tuning missing"


def test_worm_synchronous_commit_not_weakened() -> None:
    """The WORM event_log requires synchronous_commit ON — never off/local/remote_*."""
    pg = _load()["services"]["postgres"]
    cmd = " ".join(_command(pg))
    # synchronous_commit, if set at all, must be exactly =on.
    for forbidden in (
        "synchronous_commit=off",
        "synchronous_commit=local",
        "synchronous_commit=remote_write",
        "synchronous_commit=remote_apply",
    ):
        assert forbidden not in cmd, f"WORM durability weakened: {forbidden}"
    assert "synchronous_commit=on" in cmd, "synchronous_commit=on must be explicit for the WORM log"


def test_mongo_zstd_block_compressor() -> None:
    mongo = _load()["services"]["mongodb"]
    cmd = _command(mongo)
    assert "--wiredTigerCollectionBlockCompressor" in cmd, "Mongo block compressor flag missing"
    idx = cmd.index("--wiredTigerCollectionBlockCompressor")
    assert cmd[idx + 1] == "zstd", f"Mongo compressor must be zstd, got {cmd[idx + 1]!r}"


# --- D4: tmpfs RAM-backed staging ------------------------------------------


def test_compute_services_have_tmpfs_staging() -> None:
    services = _load()["services"]
    for name in _STAGING_SERVICES:
        svc = services[name]
        tmpfs = svc.get("tmpfs", [])
        assert any(_TMPFS_PATH in str(m) for m in tmpfs), f"{name} missing tmpfs {_TMPFS_PATH}"
        env = svc.get("environment", {})
        assert isinstance(env, dict)
        staging = env.get("NCE_ARTIFACT_STAGING_DIR", "")
        assert _TMPFS_PATH in str(staging), (
            f"{name} NCE_ARTIFACT_STAGING_DIR must point at the tmpfs mount, got {staging!r}"
        )
        assert _TMPFS_PATH in str(env.get("TMPDIR", "")), f"{name} TMPDIR must point at tmpfs"


# --- D7: bounded log volume ------------------------------------------------


def test_log_rotation_configured() -> None:
    services = _load()["services"]
    for name in _ROTATED_SERVICES:
        logging = services[name].get("logging", {})
        opts = logging.get("options", {})
        assert opts.get("max-size"), f"{name} missing logging max-size"
        assert opts.get("max-file"), f"{name} missing logging max-file"


# --- D2: halfvec storage format (schema + migration mirror) ----------------


def test_schema_uses_halfvec_not_fp32_for_fixed_dim_columns() -> None:
    schema = _SCHEMA.read_text(encoding="utf-8")
    # The two fixed-dimension embedding columns must be halfvec(768)...
    assert len(re.findall(r"halfvec\(768\)", schema)) >= 2, "schema.sql lost a halfvec(768) column"
    # ...and no fixed-dim fp32 vector(768) columns remain.
    assert not re.search(r"\bvector\(768\)", schema, re.IGNORECASE), (
        "schema.sql still has a fp32 vector(768) column"
    )
    # HNSW indexes must use the halfvec opclass, not the fp32 vector opclass.
    assert "halfvec_cosine_ops" in schema
    assert "vector_cosine_ops" not in schema, "schema.sql HNSW index still uses vector_cosine_ops"


def test_migration_019_mirrors_halfvec_change() -> None:
    mig = _MIGRATION.read_text(encoding="utf-8")
    assert "halfvec(768)" in mig
    assert "halfvec_cosine_ops" in mig
    # Both fixed-dim tables are migrated.
    assert "memories" in mig and "kg_nodes" in mig
    # The fp32 -> fp16 cast is present (carries existing values).
    assert "::halfvec(768)" in mig
    # The HNSW indexes are rebuilt for halfvec.
    assert "idx_memories_embedding_hnsw" in mig
    assert "idx_kg_nodes_embedding_hnsw" in mig
