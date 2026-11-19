"""T_cut Watermark Backup, Verified Restore, Ephemeral Drill & Deferred Shred GC.

Provides deterministic point-in-time snapshot orchestration across PostgreSQL
(``REPEATABLE READ READ ONLY`` + ``pg_dump``), MongoDB (``mongodump`` or batched
``bson.json_util`` streaming), and MinIO object storage, bounded by disk headroom
checks, backup lease confinement, signed zero-PII shred tombstone replay, and
JCS Merkle chain validation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from bson import ObjectId, json_util

from nce.config import cfg
from nce.db_utils import (
    resolve_ops_dsn,
    resolve_pg_admin_dsn,
    scoped_mongo_session,
    unmanaged_pg_connection,
)
from nce.envelope import (
    compute_master_key_hmac_fp,
    resolve_backup_dir,
    verify_shred_tombstone,
)
from nce.ops.models import (
    BackupManifestRecord,
    OpsDiskHeadroomError,
    OpsRestorePreflightError,
    RestoreDrillReport,
)
from nce.signing import MasterKeyMissingError, canonical_json, require_master_key

log = logging.getLogger("nce-ops.backup")

BACKUP_LEASE_NAME = "t_cut_backup"
DEFERRED_SHRED_REDIS_KEY = "nce:backup:deferred_shreds"
DEFAULT_MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
DEFAULT_RETENTION_COUNT = 7
DEFAULT_MAX_SAGA_TTL_SECONDS = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Defensive configuration & utility helpers
# ---------------------------------------------------------------------------


def _cfg_value(name: str, default: Any = None) -> Any:
    cfg_dict = getattr(cfg, "__dict__", {})
    if name in cfg_dict and cfg_dict[name] is not None:
        return cfg_dict[name]
    env_val = os.getenv(name)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip()
    attr_val = getattr(cfg, name, None)
    return attr_val if attr_val is not None else default


def _resolve_min_free_bytes(override: int | None = None) -> int:
    if override is not None:
        return max(0, int(override))
    raw = _cfg_value("NCE_BACKUP_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_FREE_BYTES


def _resolve_retention_count(override: int | None = None) -> int:
    if override is not None:
        return max(1, int(override))
    raw = _cfg_value("NCE_BACKUP_RETENTION_COUNT", DEFAULT_RETENTION_COUNT)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_COUNT


def _resolve_max_saga_ttl_seconds(override: int | None = None) -> int:
    if override is not None:
        return max(1, int(override))
    raw = _cfg_value("NCE_MAX_SAGA_TTL_SECONDS", DEFAULT_MAX_SAGA_TTL_SECONDS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SAGA_TTL_SECONDS


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


def _parse_iso_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, dict) and "$date" in value:
        raw_d = value["$date"]
        if isinstance(raw_d, (int, float)):
            return datetime.fromtimestamp(raw_d / 1000.0, tz=timezone.utc)
        value = raw_d
    if isinstance(value, str):
        txt = value.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(txt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _strip_dsn_password(dsn: str) -> tuple[str, dict[str, str]]:
    """Return ``(redacted_dsn, pg_env)`` keeping credentials out of subprocess argv."""
    parsed = urlparse(dsn)
    env_extra: dict[str, str] = {}
    if not parsed.scheme:
        return dsn, env_extra
    if parsed.password:
        env_extra["PGPASSWORD"] = parsed.password
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.username:
            netloc = f"{parsed.username}@{netloc}"
        clean_dsn = urlunparse(parsed._replace(netloc=netloc))
        return clean_dsn, env_extra
    return dsn, env_extra


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _replace_pg_db(dsn: str, db_name: str) -> str:
    parsed = urlparse(dsn)
    if not parsed.scheme:
        return dsn
    return urlunparse(parsed._replace(path=f"/{db_name.lstrip('/')}"))


def _is_real_pg_dsn(dsn: str | None) -> bool:
    return bool(dsn and str(dsn).strip().lower().startswith(("postgresql://", "postgres://")))


def _coerce_uuid_candidate(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s and "/" not in s and "\\" not in s and not Path(s).exists():
            try:
                return UUID(s)
            except ValueError:
                return None
    return None


async def _resolve_backup_archive(
    archive_or_id: str | Path | UUID,
    *,
    backup_dir: str | Path | None = None,
    pg_pool: Any | None = None,
    pg_dsn: str | None = None,
) -> Path:
    bid = _coerce_uuid_candidate(archive_or_id)
    if bid is None:
        return Path(str(archive_or_id))
    effective_dsn = pg_dsn or resolve_ops_dsn()
    if pg_pool is not None or effective_dsn:
        try:

            async def _lookup(conn: Any) -> str | None:
                return await conn.fetchval(
                    "SELECT archive_path FROM backup_manifests WHERE backup_id = $1::uuid",
                    str(bid),
                )

            found = await _with_ops_conn(pg_pool, effective_dsn, _lookup)
            if found:
                return Path(str(found))
        except Exception:
            pass
    target_dir = resolve_backup_dir(backup_dir)
    if target_dir.exists():
        matches = sorted(
            p
            for p in target_dir.iterdir()
            if p.is_file() and str(bid) in p.name and _is_backup_archive_name(p.name)
        )
        if matches:
            return matches[-1]
    raise OpsRestorePreflightError(f"Backup archive not found for backup_id={bid}")


async def _with_ops_conn(pg_pool: Any | None, pg_dsn: str | None, fn: Any) -> Any:
    if pg_pool is not None and not isinstance(pg_pool, asyncpg.Pool):
        async with unmanaged_pg_connection(
            pg_pool,
            site="ops.backup.control_plane",
        ) as conn:
            return await fn(conn)
    dsn = pg_dsn or resolve_ops_dsn()
    if dsn:
        conn = await asyncpg.connect(dsn, timeout=10.0)
        try:
            return await fn(conn)
        finally:
            await conn.close()
    if pg_pool is not None:
        async with unmanaged_pg_connection(
            pg_pool,
            site="ops.backup.control_plane",
        ) as conn:
            return await fn(conn)
    raise RuntimeError("No PostgreSQL pool or ops DSN configured for backup operations")


# ---------------------------------------------------------------------------
# Disk headroom guard & archive verification / retention pruning
# ---------------------------------------------------------------------------


def check_disk_headroom(
    backup_dir: str | Path,
    estimated_dataset_bytes: int = 0,
    *,
    min_free_bytes: int | None = None,
) -> int:
    """Ensure free filesystem headroom >= max(min_free_bytes, 3.0 * dataset_bytes)."""
    target = Path(backup_dir)
    target.mkdir(parents=True, exist_ok=True)
    free_bytes = int(shutil.disk_usage(target).free)
    floor_bytes = _resolve_min_free_bytes(min_free_bytes)
    required_bytes = max(floor_bytes, int(3.0 * max(0, int(estimated_dataset_bytes))))
    if free_bytes < required_bytes:
        raise OpsDiskHeadroomError(
            f"Insufficient disk headroom in {target}: free={free_bytes} < required={required_bytes}"
        )
    return free_bytes


def _is_backup_archive_name(name: str) -> bool:
    return name.startswith("nce-backup-") and (
        name.endswith(".tar.zst") or name.endswith(".tar.gz") or name.endswith(".tar")
    )


def _manifest_sidecar_for_archive(archive_path: Path) -> Path:
    return Path(f"{archive_path}.manifest.json")


def prune_old_backups(
    backup_dir: str | Path | None = None,
    *,
    retention_count: int | None = None,
) -> list[str]:
    """Delete oldest backup archives (and sidecars) beyond ``retention_count``."""
    target_dir = resolve_backup_dir(backup_dir)
    if not target_dir.exists():
        return []
    keep = _resolve_retention_count(retention_count)
    archives = [p for p in target_dir.iterdir() if p.is_file() and _is_backup_archive_name(p.name)]
    archives.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    pruned: list[str] = []
    for stale in archives[keep:]:
        try:
            stale.unlink(missing_ok=True)
            sidecar = _manifest_sidecar_for_archive(stale)
            sidecar.unlink(missing_ok=True)
            pruned.append(str(stale))
        except Exception as exc:
            log.warning("Failed pruning stale backup archive %s: %s", stale, exc)
    return pruned


def _manifest_signing_payload(manifest_dict: dict[str, Any]) -> dict[str, Any]:
    copy = {k: v for k, v in manifest_dict.items() if k != "manifest_hmac_sha256"}
    return copy


def _sign_manifest_dict(manifest_dict: dict[str, Any]) -> str:
    payload = _manifest_signing_payload(manifest_dict)
    canonical = canonical_json(payload)
    with require_master_key() as mk:
        return hmac.new(bytes(mk.key_bytes), canonical, hashlib.sha256).hexdigest()


def _verify_manifest_hmac(record: BackupManifestRecord) -> None:
    if not record.manifest_hmac_sha256:
        raise OpsRestorePreflightError("Backup manifest is missing manifest_hmac_sha256 signature")
    try:
        serialized = record.model_dump(mode="json")
        expected = _sign_manifest_dict(serialized)
    except MasterKeyMissingError as exc:
        raise OpsRestorePreflightError(
            "NCE_MASTER_KEY is required to verify backup manifest"
        ) from exc
    if not hmac.compare_digest(record.manifest_hmac_sha256, expected):
        raise OpsRestorePreflightError("Backup manifest HMAC signature verification failed")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp_path), str(path))


def _package_archive(staging_dir: Path, archive_path: Path) -> None:
    """Create a compressed tar archive (.tar.zst when zstd/zstandard available, else .tar.gz)."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive = archive_path.with_suffix(f"{archive_path.suffix}.{uuid.uuid4().hex}.tmp")
    try:
        if archive_path.name.endswith(".tar.zst"):
            try:
                import zstandard  # type: ignore[import-not-found]

                cctx = zstandard.ZstdCompressor(level=3)
                with tmp_archive.open("wb") as raw_fh, cctx.stream_writer(raw_fh) as zw:
                    with tarfile.open(fileobj=zw, mode="w|") as tf:
                        for item in sorted(staging_dir.rglob("*")):
                            tf.add(
                                item,
                                arcname=item.relative_to(staging_dir).as_posix(),
                                recursive=False,
                            )
                os.replace(str(tmp_archive), str(archive_path))
                return
            except ImportError:
                pass
        # Fallback / explicit .tar.gz path
        with tarfile.open(tmp_archive, mode="w:gz") as tf:
            for item in sorted(staging_dir.rglob("*")):
                tf.add(item, arcname=item.relative_to(staging_dir).as_posix(), recursive=False)
        os.replace(str(tmp_archive), str(archive_path))
    finally:
        if tmp_archive.exists():
            tmp_archive.unlink(missing_ok=True)


def _validate_tar_member(member: tarfile.TarInfo, resolved_dest: Path) -> Path:
    if (
        member.issym()
        or member.islnk()
        or member.isdev()
        or member.ischr()
        or member.isblk()
        or member.isfifo()
    ):
        raise OpsRestorePreflightError(f"Unsafe archive member type rejected: {member.name!r}")
    rel = Path(member.name)
    if rel.is_absolute() or ".." in rel.parts:
        raise OpsRestorePreflightError(
            f"Path traversal rejected in archive member: {member.name!r}"
        )
    target = (resolved_dest / rel).resolve()
    if resolved_dest not in target.parents and target != resolved_dest:
        raise OpsRestorePreflightError(f"Escaping archive member rejected: {member.name!r}")
    return target


def _validate_tar_members(tf: tarfile.TarFile, dest_dir: Path) -> None:
    resolved_dest = dest_dir.resolve()
    for member in tf.getmembers():
        _validate_tar_member(member, resolved_dest)


def _extract_validated_stream(tf: tarfile.TarFile, dest_dir: Path) -> None:
    resolved_dest = dest_dir.resolve()
    for member in tf:
        out_path = _validate_tar_member(member, resolved_dest)
        if member.isdir():
            out_path.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            src_f = tf.extractfile(member)
            if src_f is not None:
                with src_f:
                    out_path.write_bytes(src_f.read())


def _extract_archive(archive_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.name.endswith(".tar.zst"):
        try:
            import zstandard  # type: ignore[import-not-found]

            dctx = zstandard.ZstdDecompressor()
            with archive_path.open("rb") as raw_fh, dctx.stream_reader(raw_fh) as zr:
                with tarfile.open(fileobj=zr, mode="r|*") as tf:
                    _extract_validated_stream(tf, dest_dir)
            return
        except ImportError:
            pass
    with tarfile.open(archive_path, mode="r:*") as tf:
        _extract_validated_stream(tf, dest_dir)


def _compute_file_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            digests[rel] = _sha256_file(p)
    return digests


# ---------------------------------------------------------------------------
# Deferred Shred GC (Post-Lease Drainage)
# ---------------------------------------------------------------------------


async def drain_deferred_shred_gc(
    pg_pool: Any | None,
    mongo_client: Any | None,
    minio_client: Any | None,
    redis_client: Any | None = None,
    *,
    pg_dsn: str | None = None,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Drain deferred Mongo/MinIO ciphertext erasures queued during backup lease."""
    # Check active lease; never delete ciphertext while a T_cut snapshot lease runs.
    if pg_pool is not None or pg_dsn:
        try:

            async def _check_lease(conn: Any) -> bool:
                res = await conn.fetchval(
                    "SELECT nce_is_backup_lease_active($1)",
                    BACKUP_LEASE_NAME,
                )
                return res is True

            if await _with_ops_conn(pg_pool, pg_dsn, _check_lease):
                return {"drained": 0, "skipped_lease_active": True, "errors": []}
        except Exception as exc:
            log.debug("Deferred shred lease check fallback (continuing): %s", exc)

    jobs: list[dict[str, Any]] = []
    if redis_client is not None:
        lpop_fn = getattr(redis_client, "lpop", None)
        if callable(lpop_fn):
            while True:
                raw_item = await _maybe_await(lpop_fn(DEFERRED_SHRED_REDIS_KEY))
                if not raw_item:
                    break
                text = raw_item.decode("utf-8") if isinstance(raw_item, bytes) else str(raw_item)
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        jobs.append(parsed)
                except json.JSONDecodeError:
                    log.warning("Skipping malformed deferred shred Redis entry")

    deferred_file = resolve_backup_dir(backup_dir) / "deferred_shreds.jsonl"
    if deferred_file.exists():
        lines = deferred_file.read_text(encoding="utf-8").splitlines()
        deferred_file.unlink(missing_ok=True)
        for line in lines:
            txt = line.strip()
            if not txt:
                continue
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, dict):
                    jobs.append(parsed)
            except json.JSONDecodeError:
                log.warning("Skipping malformed deferred_shreds.jsonl line")

    drained = 0
    mongo_tombstoned = 0
    minio_removed = 0
    errors: list[str] = []

    for job in jobs:
        ns_id = str(job.get("namespace_id") or "").strip()
        if not ns_id:
            continue
        ns_prefix = f"{ns_id}/"
        job_ok = True

        for obj in job.get("minio_objects") or []:
            if not isinstance(obj, dict):
                continue
            bucket = str(obj.get("bucket") or "").strip()
            object_name = str(obj.get("object_name") or "").strip()
            if not bucket or not object_name or not object_name.startswith(ns_prefix):
                continue
            if minio_client is not None:
                try:
                    await asyncio.to_thread(minio_client.remove_object, bucket, object_name)
                    minio_removed += 1
                except Exception as exc:
                    job_ok = False
                    errors.append(f"minio:{bucket}/{object_name}:{exc}")

        if mongo_client is not None:
            for mid in job.get("mongo_ids") or []:
                try:
                    oid = ObjectId(str(mid))
                    async with scoped_mongo_session(mongo_client, ns_id) as s_db:
                        await s_db.episodes.update_one(
                            {"_id": oid},
                            {
                                "$set": {
                                    "raw_data": None,
                                    "shredded": True,
                                    "shredded_at": datetime.now(timezone.utc),
                                },
                                "$unset": {"metadata": ""},
                            },
                        )
                        mongo_tombstoned += 1
                except Exception as exc:
                    job_ok = False
                    errors.append(f"mongo:{mid}:{exc}")

        if job_ok:
            drained += 1

    return {
        "drained": drained,
        "skipped_lease_active": False,
        "mongo_tombstoned": mongo_tombstoned,
        "minio_removed": minio_removed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Subprocess pg_dump / pg_restore & Mongo/MinIO dump/restore helpers
# ---------------------------------------------------------------------------


async def _default_pg_dump(dsn: str, output_file: Path) -> None:
    pg_dump_bin = shutil.which("pg_dump")
    if not pg_dump_bin:
        raise FileNotFoundError(
            "pg_dump executable not found in PATH; install postgresql-client in container."
        )
    clean_dsn, env_extra = _strip_dsn_password(dsn)
    env = {**os.environ, **env_extra}
    cmd = [
        pg_dump_bin,
        "-Fc",
        "-Z",
        "zstd:3",
        "--lock-wait-timeout=10000",
        "--file",
        str(output_file),
        clean_dsn,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_txt = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pg_dump failed (rc={proc.returncode}): {err_txt}")


async def _default_pg_restore(dsn: str, dump_file: Path) -> None:
    pg_restore_bin = shutil.which("pg_restore")
    if not pg_restore_bin:
        raise OpsRestorePreflightError(
            "pg_restore executable not found in PATH; install postgresql-client in container."
        )
    clean_dsn, env_extra = _strip_dsn_password(dsn)
    env = {**os.environ, **env_extra}
    cmd = [
        pg_restore_bin,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        clean_dsn,
        str(dump_file),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_txt = stderr.decode("utf-8", errors="replace").strip()
        raise OpsRestorePreflightError(f"pg_restore failed (rc={proc.returncode}): {err_txt}")


async def _dump_mongo(
    *,
    mongo_client: Any | None,
    mongo_uri: str | None,
    staging_dir: Path,
) -> dict[str, Any]:
    mongo_dir = staging_dir / "mongo"
    mongo_dir.mkdir(parents=True, exist_ok=True)
    mongodump_bin = shutil.which("mongodump")
    effective_uri = mongo_uri or _cfg_value("MONGO_URI")

    if mongodump_bin and effective_uri:
        archive_file = mongo_dir / "mongodump.archive.gz"
        cmd = [mongodump_bin, f"--uri={effective_uri}", f"--archive={archive_file}", "--gzip"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_txt = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"mongodump failed (rc={proc.returncode}): {err_txt}")
        return {"mode": "mongodump", "archive": "mongo/mongodump.archive.gz"}

    if mongo_client is None:
        return {"mode": "none", "collections": []}

    db_name = str(_cfg_value("MONGO_DB", "memory_archive"))
    db = mongo_client[db_name]
    coll_names = await _maybe_await(db.list_collection_names())
    exported: list[str] = []
    for cname in sorted(str(c) for c in (coll_names or [])):
        if cname.startswith("system."):
            continue
        coll = db[cname]
        out_jsonl = mongo_dir / f"{cname}.jsonl"
        cursor = coll.find({})
        docs: list[Any]
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=None)
        elif hasattr(cursor, "__aiter__"):
            docs = [d async for d in cursor]
        else:
            docs = list(cursor)
        with out_jsonl.open("w", encoding="utf-8") as fh:
            for doc in docs:
                fh.write(json_util.dumps(doc) + "\n")
        exported.append(cname)
    return {"mode": "json_util", "db": db_name, "collections": exported}


async def _restore_mongo(
    *,
    mongo_client: Any | None,
    mongo_uri: str | None,
    extract_dir: Path,
    target_db_name: str | None = None,
) -> None:
    mongo_dir = extract_dir / "mongo"
    if not mongo_dir.exists():
        return
    archive_file = mongo_dir / "mongodump.archive.gz"
    mongorestore_bin = shutil.which("mongorestore")
    effective_uri = mongo_uri or _cfg_value("MONGO_URI")
    if archive_file.exists() and mongorestore_bin and effective_uri:
        cmd = [
            mongorestore_bin,
            f"--uri={effective_uri}",
            f"--archive={archive_file}",
            "--gzip",
            "--drop",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_txt = stderr.decode("utf-8", errors="replace").strip()
            raise OpsRestorePreflightError(f"mongorestore failed (rc={proc.returncode}): {err_txt}")
        return

    if mongo_client is None:
        return
    db_name = target_db_name or str(_cfg_value("MONGO_DB", "memory_archive"))
    db = mongo_client[db_name]
    for jsonl_file in sorted(mongo_dir.glob("*.jsonl")):
        cname = jsonl_file.stem
        coll = db[cname]
        if hasattr(coll, "delete_many"):
            await _maybe_await(coll.delete_many({}))
        docs = [
            json_util.loads(line)
            for line in jsonl_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if docs:
            if hasattr(coll, "insert_many"):
                await _maybe_await(coll.insert_many(docs))
            elif hasattr(coll, "insert_one"):
                for doc in docs:
                    await _maybe_await(coll.insert_one(doc))


async def _dump_minio(
    *,
    minio_client: Any | None,
    staging_dir: Path,
    extra_buckets: list[str] | tuple[str, ...] = (),
) -> list[dict[str, str]]:
    if minio_client is None:
        return []
    minio_dir = staging_dir / "minio"
    blobs_dir = minio_dir / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    buckets: set[str] = {str(_cfg_value("MINIO_BUCKET", "nce-artifacts")), "nce-artifacts"}
    buckets.update(str(b) for b in extra_buckets if b)

    def _collect() -> list[dict[str, str]]:
        listed: list[dict[str, str]] = []
        index_entries: list[dict[str, Any]] = []
        list_buckets_fn = getattr(minio_client, "list_buckets", None)
        if callable(list_buckets_fn):
            try:
                for b in list_buckets_fn() or []:
                    bname = getattr(b, "name", b)
                    if bname:
                        bstr = str(bname)
                        if bstr.startswith("nce-drill-"):
                            continue
                        buckets.add(bstr)
            except Exception as exc:
                log.debug("MinIO list_buckets fallback to default buckets: %s", exc)

        seq = 0
        for bucket in sorted(buckets):
            try:
                if not minio_client.bucket_exists(bucket):
                    continue
            except Exception:
                continue
            for obj in minio_client.list_objects(bucket, recursive=True) or []:
                obj_name = str(getattr(obj, "object_name", "") or "")
                if not obj_name or obj_name.endswith("/"):
                    continue
                resp = minio_client.get_object(bucket, obj_name)
                try:
                    data = resp.read()
                finally:
                    close_fn = getattr(resp, "close", None)
                    rel_fn = getattr(resp, "release_conn", None)
                    if callable(close_fn):
                        close_fn()
                    if callable(rel_fn):
                        rel_fn()
                meta_info: dict[str, Any] = {"bucket": bucket, "object_name": obj_name}
                stat_fn = getattr(minio_client, "stat_object", None)
                if callable(stat_fn):
                    try:
                        st = stat_fn(bucket, obj_name)
                        meta_info["content_type"] = getattr(
                            st, "content_type", "application/octet-stream"
                        )
                        raw_meta = getattr(st, "metadata", None) or {}
                        meta_info["metadata"] = {str(k): str(v) for k, v in dict(raw_meta).items()}
                    except Exception:
                        pass
                seq += 1
                key_digest = hashlib.sha256(f"{bucket}/{obj_name}".encode()).hexdigest()[:16]
                blob_rel = f"blobs/{seq:06d}_{key_digest}.bin"
                (minio_dir / blob_rel).write_bytes(data)
                meta_info["blob_rel"] = blob_rel
                index_entries.append(meta_info)
                listed.append({"bucket": bucket, "object_name": obj_name})

        (minio_dir / "index.json").write_text(
            json.dumps(index_entries, sort_keys=True),
            encoding="utf-8",
        )
        return listed

    return await asyncio.to_thread(_collect)


def _target_restore_bucket(orig_bucket: str, bucket_prefix: str | None) -> str:
    if not bucket_prefix:
        return orig_bucket
    return f"{bucket_prefix}{orig_bucket}".lower().replace("_", "-")[:63].strip("-")


_MINIO_SYSTEM_META_PREFIXES = (
    "x-amz-object-lock-",
    "x-amz-request-",
    "x-amz-id-",
    "x-amz-replication-",
)
_MINIO_SYSTEM_META_KEYS = frozenset(
    {
        "accept-ranges",
        "connection",
        "content-length",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "server",
        "strict-transport-security",
        "vary",
        "x-amz-delete-marker",
        "x-amz-version-id",
    }
)


def _extract_safe_user_metadata(raw_meta: Any) -> dict[str, str] | None:
    if not isinstance(raw_meta, dict):
        return None
    cleaned: dict[str, str] = {}
    for k, v in raw_meta.items():
        kl = str(k).lower()
        if kl.startswith(_MINIO_SYSTEM_META_PREFIXES) or kl in _MINIO_SYSTEM_META_KEYS:
            continue
        cleaned[str(k)] = str(v)
    return cleaned or None


async def _restore_minio(
    *,
    minio_client: Any | None,
    extract_dir: Path,
    bucket_prefix: str | None = None,
) -> dict[str, str]:
    """Restore MinIO objects into ``minio_client``; returns ``{orig_bucket: target_bucket}``."""
    minio_dir = extract_dir / "minio"
    if minio_client is None or not minio_dir.exists():
        return {}
    bucket_map: dict[str, str] = {}

    def _upload() -> dict[str, str]:
        index_file = minio_dir / "index.json"
        if index_file.exists():
            entries = json.loads(index_file.read_text(encoding="utf-8"))
            for entry in entries:
                orig_bucket = str(entry["bucket"])
                obj_name = str(entry["object_name"])
                target_bucket = _target_restore_bucket(orig_bucket, bucket_prefix)
                bucket_map[orig_bucket] = target_bucket
                if not minio_client.bucket_exists(target_bucket):
                    minio_client.make_bucket(target_bucket)
                raw_bytes = (minio_dir / str(entry["blob_rel"])).read_bytes()
                content_type = str(entry.get("content_type") or "application/octet-stream")
                user_meta = _extract_safe_user_metadata(entry.get("metadata"))
                minio_client.put_object(
                    target_bucket,
                    obj_name,
                    io.BytesIO(raw_bytes),
                    len(raw_bytes),
                    content_type=content_type,
                    metadata=user_meta,
                )
            return bucket_map

        for bucket_dir in sorted(minio_dir.iterdir()):
            if not bucket_dir.is_dir():
                continue
            orig_bucket = bucket_dir.name
            target_bucket = _target_restore_bucket(orig_bucket, bucket_prefix)
            bucket_map[orig_bucket] = target_bucket
            if not minio_client.bucket_exists(target_bucket):
                minio_client.make_bucket(target_bucket)
            for item in sorted(bucket_dir.rglob("*")):
                if not item.is_file() or item.name.endswith(".meta.json"):
                    continue
                rel_key = item.relative_to(bucket_dir).as_posix()
                meta_file = item.with_name(item.name + ".meta.json")
                content_type = "application/octet-stream"
                user_meta = None
                if meta_file.exists():
                    loaded = json.loads(meta_file.read_text(encoding="utf-8"))
                    content_type = str(loaded.get("content_type") or content_type)
                    user_meta = _extract_safe_user_metadata(loaded.get("metadata"))
                raw_bytes = item.read_bytes()
                minio_client.put_object(
                    target_bucket,
                    rel_key,
                    io.BytesIO(raw_bytes),
                    len(raw_bytes),
                    content_type=content_type,
                    metadata=user_meta,
                )
        return bucket_map

    return await asyncio.to_thread(_upload)


# ---------------------------------------------------------------------------
# Snapshot metadata collectors (PG)
# ---------------------------------------------------------------------------


async def _collect_pg_watermark_metadata(
    conn: Any,
    *,
    pg_dump_task: asyncio.Task[Any] | None = None,
) -> tuple[datetime, dict[str, Any], dict[str, Any]]:
    """Capture ``(t_cut, schema_heads, worm_heads)`` inside a REPEATABLE READ READ ONLY tx."""
    is_real = isinstance(conn, asyncpg.Connection)
    if is_real:
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        tx_done = False
        try:
            res = await _query_snapshot_metadata(conn, is_real=True)
            if pg_dump_task is not None:
                await pg_dump_task
            await tx.commit()
            tx_done = True
            return res
        except Exception:
            if not tx_done:
                await tx.rollback()
            raise
    else:
        res = await _query_snapshot_metadata(conn, is_real=False)
        if pg_dump_task is not None:
            await pg_dump_task
        return res


async def _query_snapshot_metadata(
    conn: Any,
    *,
    is_real: bool,
) -> tuple[datetime, dict[str, Any], dict[str, Any]]:
    ts = await conn.fetchval("SELECT transaction_timestamp()")
    t_cut = _parse_iso_dt(ts) or datetime.now(timezone.utc)

    worm_heads: dict[str, Any] = {}
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (namespace_id)
                   namespace_id::text AS namespace_id,
                   event_seq AS max_event_seq,
                   chain_hash AS head_hmac
            FROM event_log
            ORDER BY namespace_id, event_seq DESC
            """
        )
        merkle_verify_fn: Any | None = None
        if is_real:
            from nce.event_log import verify_merkle_chain

            merkle_verify_fn = verify_merkle_chain

        for r in rows or []:
            ns = str(r["namespace_id"])
            raw_hash = r["head_hmac"]
            if isinstance(raw_hash, (bytes, bytearray, memoryview)):
                head_hex = bytes(raw_hash).hex()
            else:
                head_hex = str(raw_hash or "")
            max_seq = int(r["max_event_seq"])
            entry: dict[str, Any] = {
                "max_event_seq": max_seq,
                "head_hmac": head_hex,
            }
            if merkle_verify_fn is not None and max_seq > 0:
                try:
                    vr = await merkle_verify_fn(
                        conn,
                        namespace_id=UUID(ns),
                        end_seq=max_seq,
                    )
                    entry["merkle_valid_at_cut"] = bool(vr.get("valid", False))
                except Exception:
                    entry["merkle_valid_at_cut"] = True
            worm_heads[ns] = entry
    except Exception as exc:
        log.debug("worm_heads collection skipped/empty: %s", exc)

    schema_heads = await _read_schema_heads(conn, is_real=is_real)
    return t_cut, schema_heads, worm_heads


async def _read_schema_heads(conn: Any, *, is_real: bool = False) -> dict[str, Any]:
    """Read Core and optional plugin migration heads from ``applied_migrations``."""
    schema_heads: dict[str, Any] = {"core_head": None, "core_count": 0, "plugins": {}}
    try:
        rows = await conn.fetch("SELECT version FROM applied_migrations ORDER BY version ASC")
        versions = [str(r["version"]) for r in (rows or []) if r.get("version") is not None]
        if versions:
            schema_heads["core_head"] = versions[-1]
            schema_heads["core_count"] = len(versions)
    except Exception as exc:
        log.debug("applied_migrations read skipped: %s", exc)

    try:
        if is_real:
            async with conn.transaction():
                p_rows = await conn.fetch(
                    "SELECT plugin_id, version FROM plugin_applied_migrations ORDER BY plugin_id, version ASC"
                )
        else:
            p_rows = await conn.fetch(
                "SELECT plugin_id, version FROM plugin_applied_migrations ORDER BY plugin_id, version ASC"
            )
        plugins: dict[str, str] = {}
        for pr in p_rows or []:
            pid = str(pr.get("plugin_id") or "")
            ver = str(pr.get("version") or "")
            if pid and ver:
                plugins[pid] = ver
        schema_heads["plugins"] = plugins
    except Exception:
        pass

    return schema_heads


# ---------------------------------------------------------------------------
# Create Watermark Backup
# ---------------------------------------------------------------------------


async def create_watermark_backup(
    *,
    pg_pool: Any | None = None,
    mongo_client: Any | None = None,
    minio_client: Any | None = None,
    redis_client: Any | None = None,
    pg_dsn: str | None = None,
    mongo_uri: str | None = None,
    backup_dir: str | Path | None = None,
    holder_id: str | None = None,
    estimated_dataset_bytes: int | None = None,
    min_free_bytes: int | None = None,
    retention_count: int | None = None,
    pg_dump_fn: Any | None = None,
    require_pg_dump: bool = False,
    extra_buckets: list[str] | tuple[str, ...] = (),
) -> BackupManifestRecord:
    """Create a ``T_cut`` watermarked Tri-Stack + MinIO backup with signed manifest."""
    target_dir = resolve_backup_dir(backup_dir)
    effective_dsn = pg_dsn or resolve_ops_dsn()
    eff_holder = holder_id or f"backup-{uuid.uuid4().hex[:12]}"
    backup_id = uuid.uuid4()

    # 1. Estimate dataset size (if omitted) and verify disk headroom BEFORE staging.
    est_bytes = estimated_dataset_bytes
    if est_bytes is None:
        est_bytes = 0
        if pg_pool is not None or effective_dsn:
            try:

                async def _pg_size(conn: Any) -> int:
                    val = await conn.fetchval("SELECT pg_database_size(current_database())")
                    return int(val or 0)

                est_bytes = await _with_ops_conn(pg_pool, effective_dsn, _pg_size)
            except Exception:
                est_bytes = 0

    check_disk_headroom(target_dir, est_bytes, min_free_bytes=min_free_bytes)

    # 2. Acquire 30-min T_cut lease on backup_leases.
    lease_acquired = False
    if pg_pool is not None or effective_dsn:

        async def _acquire_lease(conn: Any) -> None:
            await conn.execute(
                """
                INSERT INTO backup_leases (lease_name, holder_id, acquired_at, expires_at, metadata)
                VALUES ($1, $2, now(), now() + interval '30 minutes', $3::jsonb)
                ON CONFLICT (lease_name) DO UPDATE
                    SET holder_id = EXCLUDED.holder_id,
                        acquired_at = EXCLUDED.acquired_at,
                        expires_at = EXCLUDED.expires_at,
                        metadata = EXCLUDED.metadata
                """,
                BACKUP_LEASE_NAME,
                eff_holder,
                json.dumps({"backup_id": str(backup_id)}),
            )

        await _with_ops_conn(pg_pool, effective_dsn, _acquire_lease)
        lease_acquired = True

    staging_dir = Path(
        tempfile.mkdtemp(prefix=f"nce-backup-stage-{backup_id.hex[:8]}-", dir=str(target_dir))
    )
    try:
        # 3. Flush salience_buffer to Postgres before opening T_cut snapshot.
        if redis_client is not None and pg_pool is not None:
            from nce.salience_buffer import flush_salience_buffer

            await flush_salience_buffer(redis_client, pg_pool)

        # 4. Open REPEATABLE READ READ ONLY snapshot, collect metadata + run pg_dump.
        pg_dump_out = staging_dir / "pg.dump"
        admin_dsn = pg_dsn or resolve_pg_admin_dsn() or effective_dsn
        pg_dump_task: asyncio.Task[Any] | None = None
        if pg_dump_fn is not None:
            pg_dump_task = asyncio.create_task(_maybe_await(pg_dump_fn(admin_dsn, pg_dump_out)))
        elif admin_dsn and shutil.which("pg_dump"):
            pg_dump_task = asyncio.create_task(_default_pg_dump(admin_dsn, pg_dump_out))
        elif (
            require_pg_dump
            or pg_pool is None
            or str(_cfg_value("NCE_ENV", "")).lower() in {"prod", "production"}
            or bool(_cfg_value("IS_PROD", False))
        ):
            # Explicit real-container / production path where pg_dump binary is required.
            await _default_pg_dump(admin_dsn, pg_dump_out)

        if pg_pool is not None or effective_dsn:

            async def _snapshot(conn: Any) -> tuple[datetime, dict[str, Any], dict[str, Any]]:
                return await _collect_pg_watermark_metadata(conn, pg_dump_task=pg_dump_task)

            t_cut, schema_heads, worm_heads = await _with_ops_conn(
                pg_pool, effective_dsn, _snapshot
            )
        else:
            t_cut = datetime.now(timezone.utc)
            schema_heads = {"core_head": None, "core_count": 0, "plugins": {}}
            worm_heads = {}
            if pg_dump_task is not None:
                await pg_dump_task

        # 5. Dump MongoDB & MinIO blobs.
        await _dump_mongo(mongo_client=mongo_client, mongo_uri=mongo_uri, staging_dir=staging_dir)
        await _dump_minio(
            minio_client=minio_client, staging_dir=staging_dir, extra_buckets=extra_buckets
        )
        t_blob_completed = datetime.now(timezone.utc)

        # 6. Snapshot persistent shred_tombstones.jsonl into the archive.
        tombstone_src = target_dir / "shred_tombstones.jsonl"
        tombstone_count = 0
        if tombstone_src.exists():
            tomb_dst = staging_dir / "shred_tombstones.jsonl"
            shutil.copy2(tombstone_src, tomb_dst)
            tombstone_count = sum(
                1 for ln in tomb_dst.read_text(encoding="utf-8").splitlines() if ln.strip()
            )

        master_key_hmac_fp = compute_master_key_hmac_fp()
        file_digests = _compute_file_digests(staging_dir)

        try:
            import zstandard  # type: ignore[import-not-found] # noqa: F401

            ext = "tar.zst"
        except ImportError:
            ext = "tar.gz"

        ts_slug = t_cut.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = target_dir / f"nce-backup-{ts_slug}-{backup_id}.{ext}"

        _package_archive(staging_dir, archive_path)
        archive_sha256 = _sha256_file(archive_path)
        size_bytes = archive_path.stat().st_size

        # Mandatory post-write SHA-256 verification BEFORE manifest signing / retention prune.
        verify_sha256 = _sha256_file(archive_path)
        if not hmac.compare_digest(archive_sha256, verify_sha256):
            raise RuntimeError(f"Post-write SHA-256 verification failed for {archive_path}")

        now_created = datetime.now(timezone.utc)
        manifest_obj = BackupManifestRecord(
            backup_id=backup_id,
            t_cut=t_cut,
            t_blob_completed=t_blob_completed,
            archive_path=str(archive_path),
            archive_sha256=archive_sha256,
            size_bytes=size_bytes,
            master_key_hmac_fp=master_key_hmac_fp,
            schema_heads=schema_heads,
            worm_heads=worm_heads,
            tombstone_count=tombstone_count,
            status="verified",
            created_at=now_created,
            file_digests=file_digests,
        )
        manifest_dict = manifest_obj.model_dump(mode="json")
        manifest_sig = _sign_manifest_dict(manifest_dict)
        manifest_obj = manifest_obj.model_copy(update={"manifest_hmac_sha256": manifest_sig})
        _atomic_write_json(
            _manifest_sidecar_for_archive(archive_path),
            manifest_obj.model_dump(mode="json"),
        )

        # 7. Persist manifest record to PostgreSQL backup_manifests.
        if pg_pool is not None or effective_dsn:

            async def _save_manifest(conn: Any) -> None:
                await conn.execute(
                    """
                    INSERT INTO backup_manifests (
                        backup_id, t_cut, t_blob_completed, archive_path,
                        archive_sha256, size_bytes, master_key_hmac_fp,
                        schema_heads, worm_heads, tombstone_count, status, created_at
                    )
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11, now())
                    """,
                    str(manifest_obj.backup_id),
                    manifest_obj.t_cut,
                    manifest_obj.t_blob_completed,
                    manifest_obj.archive_path,
                    manifest_obj.archive_sha256,
                    manifest_obj.size_bytes,
                    manifest_obj.master_key_hmac_fp,
                    json.dumps(manifest_obj.schema_heads, sort_keys=True),
                    json.dumps(manifest_obj.worm_heads, sort_keys=True),
                    manifest_obj.tombstone_count,
                    manifest_obj.status,
                )

            await _with_ops_conn(pg_pool, effective_dsn, _save_manifest)

        # 8. Prune old archives ONLY AFTER new archive SHA-256 verification succeeded.
        prune_old_backups(target_dir, retention_count=retention_count)
        return manifest_obj
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if lease_acquired and (pg_pool is not None or effective_dsn):
            try:

                async def _release_lease(conn: Any) -> None:
                    await conn.execute(
                        "DELETE FROM backup_leases WHERE lease_name = $1 AND holder_id = $2",
                        BACKUP_LEASE_NAME,
                        eff_holder,
                    )

                await _with_ops_conn(pg_pool, effective_dsn, _release_lease)
            except Exception as exc:
                log.warning("Failed releasing backup lease %s: %s", eff_holder, exc)

        # 9. Always drain post-lease deferred shreds on lease release.
        try:
            await drain_deferred_shred_gc(
                pg_pool=pg_pool,
                mongo_client=mongo_client,
                minio_client=minio_client,
                redis_client=redis_client,
                pg_dsn=effective_dsn,
                backup_dir=target_dir,
            )
        except Exception as exc:
            log.warning("Post-backup deferred shred GC drain encountered errors: %s", exc)


# ---------------------------------------------------------------------------
# Restore Preflight, Replay, Reconciliation & Audit
# ---------------------------------------------------------------------------


def _load_manifest(
    archive: Path,
    manifest: BackupManifestRecord | dict[str, Any] | None,
    manifest_path: str | Path | None,
) -> BackupManifestRecord:
    if isinstance(manifest, BackupManifestRecord):
        return manifest
    if isinstance(manifest, dict):
        try:
            return BackupManifestRecord.model_validate(manifest)
        except Exception as exc:
            raise OpsRestorePreflightError(f"Invalid backup manifest payload: {exc}") from exc

    candidate = Path(manifest_path) if manifest_path else _manifest_sidecar_for_archive(archive)
    if not candidate.exists():
        raise OpsRestorePreflightError(f"Backup manifest sidecar not found: {candidate}")
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        return BackupManifestRecord.model_validate(raw)
    except Exception as exc:
        raise OpsRestorePreflightError(f"Corrupt backup manifest {candidate}: {exc}") from exc


def _check_schema_compatibility(
    manifest_heads: dict[str, Any],
    target_heads: dict[str, Any],
) -> None:
    m_core = manifest_heads.get("core_head")
    t_core = target_heads.get("core_head")
    if m_core and t_core and str(t_core) < str(m_core):
        raise OpsRestorePreflightError(
            f"Target schema core_head ({t_core}) is older than backup core_head ({m_core})"
        )
    m_plugins = manifest_heads.get("plugins") or {}
    t_plugins = target_heads.get("plugins") or {}
    for pid, p_ver in m_plugins.items():
        if pid in t_plugins and str(t_plugins[pid]) < str(p_ver):
            raise OpsRestorePreflightError(
                f"Target schema for plugin {pid!r} ({t_plugins[pid]}) is behind backup ({p_ver})"
            )


def _load_and_verify_tombstones(
    paths: list[Path],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    for path in paths:
        if not path.exists():
            continue
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            txt = line.strip()
            if not txt:
                continue
            try:
                rec = json.loads(txt)
            except json.JSONDecodeError as exc:
                raise OpsRestorePreflightError(
                    f"Corrupt shred tombstone JSONL at {path}:{idx}"
                ) from exc
            if not verify_shred_tombstone(rec):
                raise OpsRestorePreflightError(
                    f"HMAC verification failed for shred tombstone at {path}:{idx}"
                )
            key = (
                str(rec["namespace_id"]),
                str(rec.get("dek_id") or ""),
                tuple(str(x) for x in (rec.get("mongo_ids") or [])),
                tuple(str(x) for x in (rec.get("minio_keys") or [])),
            )
            if key not in seen:
                seen.add(key)
                records.append(rec)
    return records


async def _replay_tombstones_on_targets(
    *,
    tombstones: list[dict[str, Any]],
    pg_pool: Any | None,
    pg_dsn: str | None,
    mongo_client: Any | None,
    minio_client: Any | None,
    minio_bucket_map: dict[str, str],
    mongo_db_name: str | None = None,
) -> int:
    replayed = 0
    for rec in tombstones:
        ns_id = str(rec["namespace_id"])
        dek_id = str(rec.get("dek_id") or "")
        mongo_ids = [str(m) for m in (rec.get("mongo_ids") or []) if m]
        minio_keys = [str(k) for k in (rec.get("minio_keys") or []) if k]

        if pg_pool is not None or pg_dsn:

            async def _enforce_pg(conn: Any) -> None:
                is_real = isinstance(conn, asyncpg.Connection)
                if dek_id:
                    try:
                        if is_real:
                            async with conn.transaction():
                                await conn.execute(
                                    """
                                    UPDATE encryption_deks
                                    SET wrapped_key = decode(repeat('00', 32), 'hex'),
                                        shredded_at = COALESCE(shredded_at, now())
                                    WHERE dek_id::text = $1 AND namespace_id = $2::uuid
                                    """,
                                    dek_id,
                                    UUID(ns_id),
                                )
                        else:
                            await conn.execute(
                                """
                                UPDATE encryption_deks
                                SET wrapped_key = decode(repeat('00', 32), 'hex'),
                                    shredded_at = COALESCE(shredded_at, now())
                                WHERE dek_id::text = $1 AND namespace_id = $2::uuid
                                """,
                                dek_id,
                                UUID(ns_id),
                            )
                    except Exception:
                        pass

                await conn.execute(
                    """
                    UPDATE memories
                    SET wrapped_dek = NULL,
                        dek_key_id = NULL,
                        content_fts = NULL,
                        embedding = NULL,
                        valid_to = COALESCE(valid_to, now())
                    WHERE namespace_id = $1::uuid
                      AND (
                          ($2 <> '' AND dek_key_id = $2)
                          OR (cardinality($3::text[]) > 0 AND payload_ref = ANY($3::text[]))
                      )
                    """,
                    UUID(ns_id),
                    dek_id,
                    mongo_ids,
                )

            await _with_ops_conn(pg_pool, pg_dsn, _enforce_pg)

        if mongo_client is not None and mongo_ids:
            target_db = mongo_client[mongo_db_name] if mongo_db_name else None
            if target_db is not None:
                coll = target_db["episodes"]
                for mid in mongo_ids:
                    await _maybe_await(
                        coll.update_one(
                            {"_id": ObjectId(mid), "namespace_id": ns_id},
                            {
                                "$set": {
                                    "raw_data": None,
                                    "shredded": True,
                                    "shredded_at": datetime.now(timezone.utc),
                                },
                                "$unset": {"metadata": ""},
                            },
                        )
                    )
            else:
                async with scoped_mongo_session(mongo_client, ns_id) as s_db:
                    for mid in mongo_ids:
                        await s_db.episodes.update_one(
                            {"_id": ObjectId(mid)},
                            {
                                "$set": {
                                    "raw_data": None,
                                    "shredded": True,
                                    "shredded_at": datetime.now(timezone.utc),
                                },
                                "$unset": {"metadata": ""},
                            },
                        )

        if minio_client is not None and minio_keys:
            default_bucket = str(_cfg_value("MINIO_BUCKET", "nce-artifacts"))
            for mk_entry in minio_keys:
                if mk_entry.startswith(f"{ns_id}/"):
                    orig_bucket = default_bucket
                    object_name = mk_entry
                elif "/" in mk_entry:
                    orig_bucket, object_name = mk_entry.split("/", 1)
                else:
                    continue
                if not object_name.startswith(f"{ns_id}/"):
                    continue
                target_bucket = minio_bucket_map.get(orig_bucket, orig_bucket)
                try:
                    await asyncio.to_thread(minio_client.remove_object, target_bucket, object_name)
                except Exception:
                    pass

        replayed += 1
    return replayed


async def _reconcile_post_restore(
    *,
    manifest: BackupManifestRecord,
    tombstones: list[dict[str, Any]],
    pg_pool: Any | None,
    pg_dsn: str | None,
    mongo_client: Any | None,
    minio_client: Any | None,
    minio_bucket_map: dict[str, str],
    mongo_db_name: str | None,
    max_saga_ttl_seconds: int,
) -> dict[str, Any]:
    """Verify pointer integrity, sweep orphan post-T_cut blobs, check DEK/WORM invariants."""
    t0 = time.monotonic()
    t_cut = manifest.t_cut if manifest.t_cut.tzinfo else manifest.t_cut.replace(tzinfo=timezone.utc)
    _window_start = t_cut - timedelta(seconds=max_saga_ttl_seconds)

    checked_pointers = 0
    missing_pre_cut_pointers = 0
    resurrected_dek_count = 0
    committed_mongo_refs: set[str] = set()
    committed_minio_keys: set[tuple[str, str]] = set()
    worm_chain_valid = True

    if pg_pool is not None or pg_dsn:

        async def _inspect_pg(conn: Any) -> dict[str, Any]:
            is_real_conn = isinstance(conn, asyncpg.Connection)
            rows = await conn.fetch(
                """
                SELECT id::text AS memory_id, namespace_id::text AS namespace_id,
                       payload_ref, dek_key_id, (wrapped_dek IS NOT NULL) AS has_dek,
                       (content_fts IS NOT NULL) AS has_fts,
                       metadata, valid_from, valid_to, created_at
                FROM memories
                """
            )
            tomb_deks = {str(t.get("dek_id") or "") for t in tombstones if t.get("dek_id")}
            tomb_mids = {str(m) for t in tombstones for m in (t.get("mongo_ids") or [])}

            resurrected = 0
            pre_cut_rows: list[dict[str, Any]] = []
            for r in rows or []:
                m_ref = str(r.get("payload_ref") or "")
                d_id = str(r.get("dek_key_id") or "")
                has_dek = bool(r.get("has_dek"))
                has_fts = bool(r.get("has_fts"))
                if has_dek and ((d_id and d_id in tomb_deks) or (m_ref and m_ref in tomb_mids)):
                    resurrected += 1

                if m_ref:
                    committed_mongo_refs.add(m_ref)
                raw_meta = r.get("metadata")
                if isinstance(raw_meta, str):
                    try:
                        raw_meta = json.loads(raw_meta)
                    except Exception:
                        raw_meta = {}
                if isinstance(raw_meta, dict):
                    bkt = raw_meta.get("bucket")
                    obj_k = raw_meta.get("object_name")
                    if bkt and obj_k:
                        committed_minio_keys.add((str(bkt), str(obj_k)))

                vf = _parse_iso_dt(r.get("valid_from") or r.get("created_at"))
                is_shredded = (not has_dek) and ((d_id in tomb_deks) or (m_ref in tomb_mids))
                if not is_shredded and (vf is None or vf <= t_cut):
                    pre_cut_rows.append(
                        {
                            "payload_ref": m_ref,
                            "check_mongo": (not is_real_conn) or has_dek or has_fts,
                            "metadata": raw_meta or {},
                            "namespace_id": str(r["namespace_id"]),
                        }
                    )

            worm_ok = True
            if manifest.worm_heads:
                from nce.event_log import verify_merkle_chain

                for ns_id, head_info in manifest.worm_heads.items():
                    max_seq = int((head_info or {}).get("max_event_seq") or 0)
                    head_hex = str((head_info or {}).get("head_hmac") or "")
                    if max_seq <= 0:
                        continue
                    anchor_bytes = bytes.fromhex(head_hex) if head_hex else None
                    if (head_info or {}).get("merkle_valid_at_cut", True):
                        vr = await verify_merkle_chain(
                            conn,
                            namespace_id=UUID(ns_id),
                            end_seq=max_seq,
                            anchor_chain_hash=anchor_bytes,
                            anchor_seq=max_seq if anchor_bytes else None,
                        )
                        if not vr.get("valid", False):
                            worm_ok = False
                        if anchor_bytes is not None and not vr.get("anchor_match", False):
                            worm_ok = False
                    else:
                        head_row = await conn.fetchrow(
                            "SELECT chain_hash FROM event_log WHERE namespace_id = $1::uuid AND event_seq = $2",
                            UUID(ns_id),
                            max_seq,
                        )
                        restored_hash = head_row["chain_hash"] if head_row else None
                        if isinstance(restored_hash, (bytes, bytearray, memoryview)):
                            restored_hex = bytes(restored_hash).hex()
                        else:
                            restored_hex = str(restored_hash or "")
                        if not restored_hex or restored_hex != head_hex:
                            worm_ok = False

            return {
                "resurrected": resurrected,
                "pre_cut_rows": pre_cut_rows,
                "worm_ok": worm_ok,
            }

        pg_stats = await _with_ops_conn(pg_pool, pg_dsn, _inspect_pg)
        resurrected_dek_count = int(pg_stats["resurrected"])
        worm_chain_valid = bool(pg_stats["worm_ok"])

        for row in pg_stats["pre_cut_rows"]:
            m_ref = row["payload_ref"]
            meta = row["metadata"]
            checked_pointers += 1
            if m_ref and row.get("check_mongo", True) and mongo_client is not None:
                db_n = mongo_db_name or str(_cfg_value("MONGO_DB", "memory_archive"))
                doc = await _maybe_await(
                    mongo_client[db_n]["episodes"].find_one({"_id": ObjectId(m_ref)})
                )
                if doc is None:
                    missing_pre_cut_pointers += 1
            if (
                isinstance(meta, dict)
                and meta.get("bucket")
                and meta.get("object_name")
                and minio_client is not None
            ):
                tb = minio_bucket_map.get(str(meta["bucket"]), str(meta["bucket"]))
                try:
                    await asyncio.to_thread(minio_client.stat_object, tb, str(meta["object_name"]))
                except Exception:
                    missing_pre_cut_pointers += 1

    # Sweep strictly orphan post-T_cut blobs created in [T_cut, ...], preserving
    # ALL historical blobs where created_at <= T_cut (CoW / as_of history).
    swept_post_cut_blobs = 0
    if mongo_client is not None:
        db_n = mongo_db_name or str(_cfg_value("MONGO_DB", "memory_archive"))
        coll = mongo_client[db_n]["episodes"]
        cursor = coll.find({"created_at": {"$gt": t_cut}})
        if hasattr(cursor, "to_list"):
            candidates = await cursor.to_list(length=None)
        elif hasattr(cursor, "__aiter__"):
            candidates = [d async for d in cursor]
        else:
            candidates = list(cursor)
        for doc in candidates:
            doc_created = _parse_iso_dt(doc.get("created_at"))
            if doc_created is None or doc_created <= t_cut:
                continue
            oid_str = str(doc.get("_id") or "")
            if oid_str and oid_str not in committed_mongo_refs:
                await _maybe_await(coll.delete_one({"_id": doc["_id"]}))
                swept_post_cut_blobs += 1

    if minio_client is not None:

        def _sweep_minio() -> int:
            swept = 0
            checked_buckets = list(minio_bucket_map.values()) or [
                str(_cfg_value("MINIO_BUCKET", "nce-artifacts"))
            ]
            for orig_b, tb in (
                minio_bucket_map.items() if minio_bucket_map else [(b, b) for b in checked_buckets]
            ):
                try:
                    if not minio_client.bucket_exists(tb):
                        continue
                except Exception:
                    continue
                for obj in minio_client.list_objects(tb, recursive=True) or []:
                    obj_name = str(getattr(obj, "object_name", "") or "")
                    if not obj_name or obj_name.endswith("/"):
                        continue
                    try:
                        st = minio_client.stat_object(tb, obj_name)
                    except Exception:
                        continue
                    raw_meta = getattr(st, "metadata", None) or {}
                    created_val = None
                    for mk_k, mk_v in dict(raw_meta).items():
                        if str(mk_k).lower().endswith("created-at"):
                            created_val = _parse_iso_dt(mk_v)
                            break
                    if created_val is None or created_val <= t_cut:
                        continue
                    if (orig_b, obj_name) not in committed_minio_keys and (
                        tb,
                        obj_name,
                    ) not in committed_minio_keys:
                        minio_client.remove_object(tb, obj_name)
                        swept += 1
            return swept

        swept_post_cut_blobs += await asyncio.to_thread(_sweep_minio)

    reconciliation_ms = round((time.monotonic() - t0) * 1000.0, 3)
    return {
        "reconciliation_ms": reconciliation_ms,
        "checked_pointers": checked_pointers,
        "missing_pre_cut_pointers": missing_pre_cut_pointers,
        "resurrected_dek_count": resurrected_dek_count,
        "swept_post_cut_blobs": swept_post_cut_blobs,
        "worm_chain_valid": worm_chain_valid,
    }


async def restore_watermark_backup(
    archive_path: str | Path | UUID,
    *,
    manifest_path: str | Path | None = None,
    manifest: BackupManifestRecord | dict[str, Any] | None = None,
    pg_pool: Any | None = None,
    mongo_client: Any | None = None,
    minio_client: Any | None = None,
    pg_dsn: str | None = None,
    mongo_uri: str | None = None,
    backup_dir: str | Path | None = None,
    tombstones_path: str | Path | None = None,
    current_schema_heads: dict[str, Any] | None = None,
    pg_restore_fn: Any | None = None,
    ephemeral: bool = False,
    max_saga_ttl_seconds: int | None = None,
    minio_bucket_prefix: str | None = None,
    mongo_db_name: str | None = None,
) -> dict[str, Any]:
    """Verify, extract, restore, replay tombstones, reconcile, and audit a backup."""
    archive = await _resolve_backup_archive(
        archive_path,
        backup_dir=backup_dir,
        pg_pool=pg_pool,
        pg_dsn=pg_dsn,
    )
    if not await asyncio.to_thread(archive.is_file):
        raise OpsRestorePreflightError(f"Backup archive not found: {archive}")

    target_dir = resolve_backup_dir(backup_dir) if backup_dir else archive.parent
    manifest_rec = _load_manifest(archive, manifest, manifest_path)

    # 1. Strict cryptographic preflight: manifest HMAC, archive SHA-256, master key FP.
    _verify_manifest_hmac(manifest_rec)
    actual_sha = _sha256_file(archive)
    if not hmac.compare_digest(actual_sha, manifest_rec.archive_sha256):
        raise OpsRestorePreflightError(
            f"Archive SHA-256 mismatch for {archive}: expected={manifest_rec.archive_sha256}, actual={actual_sha}"
        )

    try:
        current_key_fp = compute_master_key_hmac_fp()
    except MasterKeyMissingError as exc:
        raise OpsRestorePreflightError("NCE_MASTER_KEY is missing for restore preflight") from exc
    if not hmac.compare_digest(current_key_fp, manifest_rec.master_key_hmac_fp):
        raise OpsRestorePreflightError(
            "Master key HMAC fingerprint mismatch — refusing to restore unrecoverable DEKs"
        )

    effective_dsn = pg_dsn or resolve_ops_dsn()
    if current_schema_heads is not None:
        _check_schema_compatibility(manifest_rec.schema_heads, current_schema_heads)
    elif pg_pool is not None or effective_dsn:
        try:

            async def _fetch_target_heads(conn: Any) -> dict[str, Any]:
                return await _read_schema_heads(conn, is_real=isinstance(conn, asyncpg.Connection))

            target_heads = await _with_ops_conn(pg_pool, effective_dsn, _fetch_target_heads)
            if target_heads.get("core_head"):
                _check_schema_compatibility(manifest_rec.schema_heads, target_heads)
        except OpsRestorePreflightError:
            raise
        except Exception as exc:
            log.debug("Target schema head preflight check skipped (empty target DB?): %s", exc)

    check_disk_headroom(target_dir, manifest_rec.size_bytes, min_free_bytes=0)

    extract_dir = Path(
        tempfile.mkdtemp(
            prefix=f"nce-restore-{manifest_rec.backup_id.hex[:8]}-", dir=str(target_dir)
        )
    )
    try:
        _extract_archive(archive, extract_dir)

        if manifest_rec.file_digests:
            actual_digests = _compute_file_digests(extract_dir)
            for rel, expected_digest in manifest_rec.file_digests.items():
                if actual_digests.get(rel) != expected_digest:
                    raise OpsRestorePreflightError(f"Archive member digest mismatch for {rel!r}")

        tombstone_files: list[Path] = [extract_dir / "shred_tombstones.jsonl"]
        if tombstones_path is not None:
            tombstone_files.append(Path(tombstones_path))
        elif (target_dir / "shred_tombstones.jsonl").exists():
            tombstone_files.append(target_dir / "shred_tombstones.jsonl")
        tombstones = _load_and_verify_tombstones(tombstone_files)

        # 2. Restore PostgreSQL, MongoDB, and MinIO.
        pg_dump_file = extract_dir / "pg.dump"
        if pg_restore_fn is not None and pg_dump_file.exists():
            await _maybe_await(pg_restore_fn(effective_dsn, pg_dump_file))
        elif pg_dump_file.exists() and effective_dsn:
            await _default_pg_restore(effective_dsn, pg_dump_file)

        await _restore_mongo(
            mongo_client=mongo_client,
            mongo_uri=mongo_uri,
            extract_dir=extract_dir,
            target_db_name=mongo_db_name,
        )
        minio_bucket_map = await _restore_minio(
            minio_client=minio_client,
            extract_dir=extract_dir,
            bucket_prefix=minio_bucket_prefix,
        )

        # 3. Replay persistent shred tombstones (zero-resurrection guarantee).
        tombstones_replayed = await _replay_tombstones_on_targets(
            tombstones=tombstones,
            pg_pool=pg_pool,
            pg_dsn=effective_dsn,
            mongo_client=mongo_client,
            minio_client=minio_client,
            minio_bucket_map=minio_bucket_map,
            mongo_db_name=mongo_db_name,
        )

        # 4. Bounded reconciliation + post-T_cut orphan blob sweep + Merkle check.
        recon = await _reconcile_post_restore(
            manifest=manifest_rec,
            tombstones=tombstones,
            pg_pool=pg_pool,
            pg_dsn=effective_dsn,
            mongo_client=mongo_client,
            minio_client=minio_client,
            minio_bucket_map=minio_bucket_map,
            mongo_db_name=mongo_db_name,
            max_saga_ttl_seconds=_resolve_max_saga_ttl_seconds(max_saga_ttl_seconds),
        )

        # 5. Record WORM config_changed audit event on _system for non-ephemeral restores.
        audit_event_id: str | None = None
        if not ephemeral and (pg_pool is not None or effective_dsn):
            try:
                from nce.event_log import append_event
                from nce.system_namespace import get_system_namespace_id

                async def _audit(conn: Any) -> str:
                    sys_ns = await get_system_namespace_id(conn)
                    res = await append_event(
                        conn=conn,
                        namespace_id=sys_ns,
                        agent_id="system",
                        event_type="config_changed",
                        params={
                            "actor": "ops.backup.restore",
                            "changes": {
                                "action": "watermark_restore",
                                "backup_id": str(manifest_rec.backup_id),
                                "t_cut": manifest_rec.t_cut.isoformat(),
                                "tombstones_replayed": tombstones_replayed,
                                "swept_post_cut_blobs": recon["swept_post_cut_blobs"],
                            },
                        },
                        result_summary={"status": "success"},
                    )
                    return str(res.event_id)

                audit_event_id = await _with_ops_conn(pg_pool, effective_dsn, _audit)
            except Exception as exc:
                log.debug("System WORM config_changed event skipped on restore: %s", exc)

        return {
            "status": "restored",
            "backup_id": str(manifest_rec.backup_id),
            "t_cut": manifest_rec.t_cut.isoformat(),
            "tombstones_replayed": tombstones_replayed,
            "audit_event_id": audit_event_id,
            **recon,
        }
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Ephemeral Single-Host Drill & Stale Drill Sweeper
# ---------------------------------------------------------------------------


async def sweep_stale_drill_targets(
    *,
    pg_pool: Any | None = None,
    mongo_client: Any | None = None,
    minio_client: Any | None = None,
    pg_dsn: str | None = None,
    backup_dir: str | Path | None = None,
    max_age_seconds: int = 3600,
) -> dict[str, int]:
    """Clean up leftover ``nce_drill_*`` databases, buckets, and temp dirs."""
    removed_pg = 0
    removed_mongo = 0
    removed_minio = 0
    removed_dirs = 0

    admin_pg_dsn = pg_dsn or resolve_pg_admin_dsn()
    if (pg_pool is not None and not isinstance(pg_pool, asyncpg.Pool)) or _is_real_pg_dsn(
        admin_pg_dsn
    ):
        try:
            if pg_pool is not None and not isinstance(pg_pool, asyncpg.Pool):

                async def _drop_pg_drills_fake(conn: Any) -> int:
                    rows = await conn.fetch(
                        "SELECT datname FROM pg_database WHERE datname LIKE 'nce_drill_%'"
                    )
                    count = 0
                    for r in rows or []:
                        dname = str(r["datname"])
                        if dname.startswith("nce_drill_") and dname.replace("_", "").isalnum():
                            await conn.execute(f'DROP DATABASE IF EXISTS "{dname}"')
                            count += 1
                    return count

                removed_pg = await _with_ops_conn(pg_pool, admin_pg_dsn, _drop_pg_drills_fake)
            else:
                maint_dsn = _replace_pg_db(admin_pg_dsn, "postgres")
                conn = await asyncpg.connect(maint_dsn, timeout=15.0)
                try:
                    rows = await conn.fetch(
                        "SELECT datname FROM pg_database WHERE datname LIKE 'nce_drill_%'"
                    )
                    for r in rows or []:
                        dname = str(r["datname"])
                        if dname.startswith("nce_drill_") and dname.replace("_", "").isalnum():
                            await conn.execute(
                                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                                dname,
                            )
                            await conn.execute(f'DROP DATABASE IF EXISTS "{dname}" WITH (FORCE)')
                            removed_pg += 1
                finally:
                    await conn.close()
        except Exception as exc:
            log.debug("PG stale drill sweep skipped: %s", exc)

    if mongo_client is not None:
        try:
            db_names = await _maybe_await(mongo_client.list_database_names())
            for dname in db_names or []:
                if str(dname).startswith("nce_drill_"):
                    await _maybe_await(mongo_client.drop_database(str(dname)))
                    removed_mongo += 1
        except Exception as exc:
            log.debug("Mongo stale drill sweep skipped: %s", exc)

    if minio_client is not None:

        def _sweep_buckets() -> int:
            cnt = 0
            for b in minio_client.list_buckets() or []:
                bname = str(getattr(b, "name", b))
                if bname.startswith("nce-drill-"):
                    for obj in list(minio_client.list_objects(bname, recursive=True) or []):
                        minio_client.remove_object(bname, str(obj.object_name))
                    minio_client.remove_bucket(bname)
                    cnt += 1
            return cnt

        try:
            removed_minio = await asyncio.to_thread(_sweep_buckets)
        except Exception as exc:
            log.debug("MinIO stale drill sweep skipped: %s", exc)

    target_dir = resolve_backup_dir(backup_dir)
    if target_dir.exists():
        cutoff = time.time() - max(0, max_age_seconds)
        for p in target_dir.iterdir():
            if p.is_dir() and (
                p.name.startswith("nce-drill-") or p.name.startswith("nce-restore-")
            ):
                if p.stat().st_mtime <= cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    removed_dirs += 1

    return {
        "removed_pg": removed_pg,
        "removed_mongo": removed_mongo,
        "removed_minio": removed_minio,
        "removed_dirs": removed_dirs,
    }


async def run_ephemeral_restore_drill(
    archive_path: str | Path | UUID | None = None,
    *,
    backup_id: str | UUID | None = None,
    manifest_path: str | Path | None = None,
    manifest: BackupManifestRecord | dict[str, Any] | None = None,
    backup_dir: str | Path | None = None,
    pg_pool: Any | None = None,
    mongo_client: Any | None = None,
    minio_client: Any | None = None,
    pg_dsn: str | None = None,
    mongo_uri: str | None = None,
    tombstones_path: str | Path | None = None,
    drill_pg_restore_fn: Any | None = None,
    drill_pg_pool: Any | None = None,
) -> RestoreDrillReport:
    """Restore a backup into isolated ephemeral targets, validate invariants, and clean up."""
    t_start = time.monotonic()
    target_dir = resolve_backup_dir(backup_dir)
    effective_dsn = pg_dsn or resolve_ops_dsn()
    admin_dsn = pg_dsn or resolve_pg_admin_dsn() or effective_dsn
    drill_hex = uuid.uuid4().hex[:12]
    drill_id = f"drill_{drill_hex}"
    drill_pg_db = f"nce_drill_{drill_hex}"
    drill_mongo_db = f"nce_drill_{drill_hex}"
    drill_minio_prefix = f"nce-drill-{drill_hex}-"

    if backup_id is None and archive_path is not None:
        coerced = _coerce_uuid_candidate(archive_path)
        if coerced is not None:
            backup_id = coerced
            archive_path = None

    resolved_archive: Path | None = Path(str(archive_path)) if archive_path else None
    if resolved_archive is None and (pg_pool is not None or effective_dsn):

        async def _find_manifest(conn: Any) -> str | None:
            if backup_id is not None:
                return await conn.fetchval(
                    "SELECT archive_path FROM backup_manifests WHERE backup_id = $1::uuid",
                    str(backup_id),
                )
            return await conn.fetchval(
                "SELECT archive_path FROM backup_manifests WHERE status = 'verified' ORDER BY t_cut DESC LIMIT 1"
            )

        db_archive = await _with_ops_conn(pg_pool, effective_dsn, _find_manifest)
        if db_archive:
            resolved_archive = Path(str(db_archive))

    if resolved_archive is None and target_dir.exists():
        candidates = [
            p for p in target_dir.iterdir() if p.is_file() and _is_backup_archive_name(p.name)
        ]
        candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        if candidates:
            resolved_archive = candidates[0]

    if resolved_archive is None:
        raise OpsRestorePreflightError("No verified backup archive available for ephemeral drill")

    manifest_rec = _load_manifest(resolved_archive, manifest, manifest_path)
    cleanup_verified = False
    error_msg: str | None = None
    recon_result: dict[str, Any] = {}

    use_isolated_real_pg = (
        drill_pg_pool is None
        and _is_real_pg_dsn(admin_dsn)
        and (pg_pool is None or isinstance(pg_pool, asyncpg.Pool))
    )
    created_drill_pg_db = False
    drill_pg_dsn: str | None = None
    maint_dsn = _replace_pg_db(admin_dsn, "postgres") if use_isolated_real_pg else ""

    try:
        if use_isolated_real_pg:
            m_conn = await asyncpg.connect(maint_dsn, timeout=15.0)
            try:
                await m_conn.execute(f'CREATE DATABASE "{drill_pg_db}"')
                created_drill_pg_db = True
            finally:
                await m_conn.close()
            drill_pg_dsn = _replace_pg_db(admin_dsn, drill_pg_db)

        recon_result = await restore_watermark_backup(
            resolved_archive,
            manifest_path=manifest_path,
            manifest=manifest_rec,
            pg_pool=drill_pg_pool
            if drill_pg_pool is not None
            else (None if use_isolated_real_pg else pg_pool),
            mongo_client=mongo_client,
            minio_client=minio_client,
            pg_dsn=drill_pg_dsn if use_isolated_real_pg else effective_dsn,
            mongo_uri=mongo_uri,
            backup_dir=target_dir,
            tombstones_path=tombstones_path,
            pg_restore_fn=drill_pg_restore_fn,
            ephemeral=True,
            minio_bucket_prefix=drill_minio_prefix,
            mongo_db_name=drill_mongo_db,
        )
    except Exception as exc:
        error_msg = str(exc)
    finally:
        cleanup_errors: list[str] = []
        if created_drill_pg_db and maint_dsn:
            try:
                m_conn = await asyncpg.connect(maint_dsn, timeout=15.0)
                try:
                    await m_conn.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = $1 AND pid <> pg_backend_pid()",
                        drill_pg_db,
                    )
                    await m_conn.execute(f'DROP DATABASE IF EXISTS "{drill_pg_db}" WITH (FORCE)')
                finally:
                    await m_conn.close()
            except Exception as exc:
                cleanup_errors.append(f"pg_drop:{exc}")
        if mongo_client is not None:
            try:
                await _maybe_await(mongo_client.drop_database(drill_mongo_db))
            except Exception as exc:
                cleanup_errors.append(f"mongo_drop:{exc}")
        if minio_client is not None:

            def _drop_drill_buckets() -> None:
                for b in minio_client.list_buckets() or []:
                    bname = str(getattr(b, "name", b))
                    if bname.startswith(drill_minio_prefix):
                        for obj in list(minio_client.list_objects(bname, recursive=True) or []):
                            minio_client.remove_object(bname, str(obj.object_name))
                        minio_client.remove_bucket(bname)

            try:
                await asyncio.to_thread(_drop_drill_buckets)
            except Exception as exc:
                cleanup_errors.append(f"minio_drop:{exc}")
        cleanup_verified = len(cleanup_errors) == 0
        if cleanup_errors and error_msg is None:
            error_msg = "; ".join(cleanup_errors)

    passed = (
        error_msg is None
        and int(recon_result.get("missing_pre_cut_pointers", 0)) == 0
        and int(recon_result.get("resurrected_dek_count", 0)) == 0
        and bool(recon_result.get("worm_chain_valid", True))
        and cleanup_verified
    )

    report = RestoreDrillReport(
        backup_id=manifest_rec.backup_id,
        drill_id=drill_id,
        status="passed" if passed else "failed",
        t_cut=manifest_rec.t_cut,
        duration_seconds=round(time.monotonic() - t_start, 4),
        reconciliation_ms=float(recon_result.get("reconciliation_ms", 0.0)),
        checked_pointers=int(recon_result.get("checked_pointers", 0)),
        missing_pre_cut_pointers=int(recon_result.get("missing_pre_cut_pointers", 0)),
        resurrected_dek_count=int(recon_result.get("resurrected_dek_count", 0)),
        swept_post_cut_blobs=int(recon_result.get("swept_post_cut_blobs", 0)),
        worm_chain_valid=bool(recon_result.get("worm_chain_valid", False)),
        cleanup_verified=cleanup_verified,
        details=recon_result,
        error=error_msg,
    )

    if pg_pool is not None or effective_dsn:
        try:

            async def _record_drill(conn: Any) -> None:
                await conn.execute(
                    """
                    UPDATE backup_manifests
                    SET last_drill_at = now(),
                        last_drill_status = $2,
                        last_drill_report = $3::jsonb
                    WHERE backup_id = $1::uuid
                    """,
                    str(manifest_rec.backup_id),
                    report.status,
                    json.dumps(report.model_dump(mode="json"), sort_keys=True),
                )
                try:
                    from nce.event_log import append_event
                    from nce.system_namespace import get_system_namespace_id

                    sys_ns = await get_system_namespace_id(conn)
                    await append_event(
                        conn=conn,
                        namespace_id=sys_ns,
                        agent_id="system",
                        event_type="config_changed",
                        params={
                            "actor": "ops.backup.drill",
                            "changes": {
                                "action": "ephemeral_restore_drill",
                                "backup_id": str(manifest_rec.backup_id),
                                "drill_id": report.drill_id,
                                "status": report.status,
                                "worm_chain_valid": report.worm_chain_valid,
                            },
                        },
                        result_summary={"status": report.status},
                    )
                except Exception as exc:
                    log.debug("System WORM drill audit skipped: %s", exc)

            await _with_ops_conn(pg_pool, effective_dsn, _record_drill)
        except Exception as exc:
            log.debug("Failed recording drill report to backup_manifests: %s", exc)

    return report


async def get_backup_status(
    *,
    pg_pool: Any | None = None,
    pg_dsn: str | None = None,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return backup inventory, active leases, quarantine counts, and disk headroom."""
    target_dir = resolve_backup_dir(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target_dir)
    local_archives = sorted(
        p.name for p in target_dir.iterdir() if p.is_file() and _is_backup_archive_name(p.name)
    )
    tombstone_file = target_dir / "shred_tombstones.jsonl"
    tombstone_lines = (
        sum(1 for ln in tombstone_file.read_text(encoding="utf-8").splitlines() if ln.strip())
        if tombstone_file.exists()
        else 0
    )

    status_out: dict[str, Any] = {
        "backup_dir": str(target_dir),
        "disk_free_bytes": int(usage.free),
        "disk_total_bytes": int(usage.total),
        "local_archives": local_archives,
        "tombstone_count": tombstone_lines,
        "latest_manifest": None,
        "active_lease": False,
        "quarantined_jobs": 0,
    }

    effective_dsn = pg_dsn or resolve_ops_dsn()
    if pg_pool is not None or effective_dsn:
        try:

            async def _fetch_db_status(conn: Any) -> dict[str, Any]:
                lease = await conn.fetchval(
                    "SELECT nce_is_backup_lease_active($1)",
                    BACKUP_LEASE_NAME,
                )
                latest = await conn.fetchrow(
                    """
                    SELECT backup_id, t_cut, archive_path, status,
                           last_drill_at, last_drill_status
                    FROM backup_manifests
                    ORDER BY t_cut DESC
                    LIMIT 1
                    """
                )
                q_cnt = await conn.fetchval(
                    "SELECT count(*) FROM ops_quarantine WHERE status = 'quarantined'"
                )
                return {
                    "active_lease": lease is True,
                    "latest_manifest": (
                        {
                            "backup_id": str(latest["backup_id"]),
                            "t_cut": latest["t_cut"].isoformat() if latest.get("t_cut") else None,
                            "archive_path": latest.get("archive_path"),
                            "status": latest.get("status"),
                            "last_drill_at": (
                                latest["last_drill_at"].isoformat()
                                if latest.get("last_drill_at")
                                else None
                            ),
                            "last_drill_status": latest.get("last_drill_status"),
                        }
                        if latest
                        else None
                    ),
                    "quarantined_jobs": int(q_cnt or 0),
                }

            status_out.update(await _with_ops_conn(pg_pool, effective_dsn, _fetch_db_status))
        except Exception as exc:
            status_out["db_error"] = str(exc)

    return status_out
