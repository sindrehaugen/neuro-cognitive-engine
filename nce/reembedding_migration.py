"""
Phase 2.1 — Automated re-embedding migration (Strategy A, dimension-compatible).

Provides deterministic vector math helpers, queue-driven backfill, quality-gate
overlap checks, and *active embedding* resolution so semantic search keeps using the
currently authoritative column while ``embedding_v2`` is filled in the background.

Postgres/asyncpg adapters can wrap this orchestration later; callers pass a ``Store``
that satisfies :class:`ReembeddingStorePort`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from strenum import StrEnum  # type: ignore[import-untyped]
from typing import Any, Protocol

log = logging.getLogger("nce-reembedding-migration")


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity with L2 normalization; clamps to [-1, 1] for FP noise."""
    if len(a) != len(b):
        raise ValueError("dimension mismatch")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    raw = dot / (na * nb)
    return max(-1.0, min(1.0, raw))


def neighbor_overlap_fraction(
    old_neighbors: Iterable[str],
    new_neighbors: Iterable[str],
) -> float:
    """
    Jaccard similarity of two neighbour-ID sets (roadmap §2.1 quality gate).

    Interpretation:
        overlap >= threshold (e.g. 0.7) ⇒ gate pass for that sample pair.
        overlap < threshold ⇒ migration commit should be blocked until resolved.
    """
    a = frozenset(old_neighbors)
    b = frozenset(new_neighbors)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def deterministic_unit_embedding(
    text: str,
    *,
    model_version: str,
    dimension: int,
    rotation: Sequence[float] | None = None,
) -> list[float]:
    """
    Deterministic mock embedding: SHA-256–seeded repeatable noise, **L2 = 1**.

    Passing a different ``model_version`` rotates the derivation so vectors differ
    (model bump) but remain stable for identical text (TEST-2.1-01).
    """
    if dimension < 1:
        raise ValueError("dimension must be >= 1")
    vec: list[float] = []
    counter = 0
    while len(vec) < dimension:
        blob = hashlib.sha256(f"{model_version}\0{text}\0{counter}".encode()).digest()
        for i in range(0, len(blob) - 1, 2):
            coord = int.from_bytes(blob[i : i + 2], "big") / 65535.0 - 0.5
            vec.append(coord)
            if len(vec) >= dimension:
                break
        counter += 1
    vec = vec[:dimension]
    if rotation is not None:
        if len(rotation) != dimension:
            raise ValueError("rotation length must match dimension")
        vec = [v + r for v, r in zip(vec, rotation)]
    s = math.sqrt(sum(x * x for x in vec))
    if s == 0.0:
        return [1.0 if i == 0 else 0.0 for i in range(dimension)]
    return [x / s for x in vec]


@dataclass(frozen=True)
class MemoryEmbeddingRow:
    memory_id: str
    canonical_text: str
    embedding_v1: list[float]
    embedding_v2: list[float] | None = None
    embedding_v2_target_model_id: str | None = None


class MigrationPhase(StrEnum):
    IDLE = "idle"
    BACKFILLING = "backfilling"
    COMMITTED = "committed"
    ABORTED = "aborted"


class ReembeddingStorePort(Protocol):
    """Abstract store boundary — production uses asyncpg; tests use an in-memory impl."""

    async def pop_pending_ids(self, limit: int) -> list[str]: ...

    async def load_row(self, memory_id: str) -> MemoryEmbeddingRow | None: ...

    async def write_embedding_v2(
        self,
        memory_id: str,
        *,
        embedding: Sequence[float],
        model_id: str,
    ) -> None: ...


class ReembeddingMigrationOrchestrator:
    """
    Drives backlog processing after a target model bump.

    Concurrent **reads** remain valid as long as the store delegates
    ``active_embedding()`` to embedding_v1 until :meth:`commit` is called —
    callers must not erase v1 prematurely.
    """

    def __init__(
        self,
        *,
        store: ReembeddingStorePort,
        embed_fn_v2: EmbeddingFn,
        target_model_id: str,
        dimension: int,
    ):
        self._store = store
        self.embed_fn_v2 = embed_fn_v2
        self.target_model_id = target_model_id
        self.dimension = dimension
        self.phase = MigrationPhase.BACKFILLING
        self._cv = asyncio.Condition()

    async def process_batch(self, batch_size: int) -> int:
        """Dequeue up to *batch_size* IDs, embed with v2, persist without touching v1."""
        if self.phase != MigrationPhase.BACKFILLING:
            return 0
        batch_size = min(batch_size, _MAX_BATCH_SIZE)
        ids = await self._store.pop_pending_ids(batch_size)
        if not ids:
            return 0
        valid_rows: list[MemoryEmbeddingRow] = []
        for mid in ids:
            row = await self._store.load_row(mid)
            if row is None:
                continue
            if row.embedding_v2 is not None:
                continue
            text_bytes = len(row.canonical_text.encode("utf-8"))
            if not row.canonical_text or text_bytes > _MAX_CANONICAL_TEXT_BYTES:
                log.warning(
                    "Skipping memory_id=%s: canonical_text is %s",
                    mid,
                    "empty"
                    if not row.canonical_text
                    else f"{text_bytes} bytes (limit {_MAX_CANONICAL_TEXT_BYTES})",
                )
                continue
            if len(row.embedding_v1) != self.dimension:
                raise ValueError(
                    f"memory_id={mid}: embedding_v1 has dim {len(row.embedding_v1)}, "
                    f"expected {self.dimension}"
                )
            valid_rows.append(row)

        _sem = asyncio.Semaphore(_EMBED_MAX_CONCURRENT)

        async def _embed_one(row: MemoryEmbeddingRow) -> tuple[str, list[float]]:
            async with _sem:
                last_exc: BaseException | None = None
                for attempt in range(_EMBED_MAX_RETRIES):
                    try:
                        vec = await asyncio.wait_for(
                            asyncio.to_thread(
                                lambda: self.embed_fn_v2(
                                    row.canonical_text,
                                    dimension=self.dimension,
                                )
                            ),
                            timeout=_EMBED_TIMEOUT_SECONDS,
                        )
                        break
                    except (TimeoutError, asyncio.TimeoutError, Exception) as exc:
                        last_exc = exc
                        if attempt < _EMBED_MAX_RETRIES - 1:
                            await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise last_exc  # type: ignore[misc]
            if len(vec) != self.dimension:
                raise ValueError(f"embed_fn_v2 returned dim {len(vec)}, expected {self.dimension}")
            return row.memory_id, vec

        results = await asyncio.gather(
            *[_embed_one(r) for r in valid_rows],
            return_exceptions=True,
        )

        written = 0
        for res in results:
            if isinstance(res, BaseException):
                log.error(
                    "Embedding failed for a row in batch: %s: %s",
                    type(res).__name__,
                    res,
                )
                continue
            mid, vec = res
            await self._store.write_embedding_v2(mid, embedding=vec, model_id=self.target_model_id)
            written += 1

        async with self._cv:
            self._cv.notify_all()
        return written

    async def mark_aborted(self) -> None:
        """Align orchestrator state when the store aborts a migration."""
        self.phase = MigrationPhase.ABORTED
        async with self._cv:
            self._cv.notify_all()

    def mark_committed(self) -> None:
        """Call after :meth:`InMemoryReembeddingStore.commit_primary_to_v2`."""
        self.phase = MigrationPhase.COMMITTED


@dataclass
class InMemoryReembeddingStore:
    """
    Test/dev store: dual embeddings + pending queue + explicit commit/abort.

    ``active_embedding`` always returns v1 until :meth:`commit`; then returns v2
    where present else v1.
    """

    _rows: dict[str, MemoryEmbeddingRow]
    _pending: asyncio.Queue[str]
    phase: MigrationPhase = MigrationPhase.IDLE
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def from_records(
        cls,
        records: Mapping[str, MemoryEmbeddingRow],
        *,
        initial_pending: Iterable[str],
    ) -> InMemoryReembeddingStore:
        q: asyncio.Queue[str] = asyncio.Queue()
        store = cls(_rows=dict(records), _pending=q, phase=MigrationPhase.BACKFILLING)
        for mid in initial_pending:
            store._pending.put_nowait(mid)
        return store

    def pending_qsize(self) -> int:
        return self._pending.qsize()

    async def pop_pending_ids(self, limit: int) -> list[str]:
        ids: list[str] = []
        for _ in range(limit):
            try:
                ids.append(self._pending.get_nowait())
            except asyncio.QueueEmpty:
                break
        return ids

    async def load_row(self, memory_id: str) -> MemoryEmbeddingRow | None:
        async with self._lock:
            row = self._rows.get(memory_id)
            return row

    async def write_embedding_v2(
        self,
        memory_id: str,
        *,
        embedding: Sequence[float],
        model_id: str,
    ) -> None:
        async with self._lock:
            old = self._rows.get(memory_id)
            if old is None:
                return
            self._rows[memory_id] = MemoryEmbeddingRow(
                memory_id=old.memory_id,
                canonical_text=old.canonical_text,
                embedding_v1=old.embedding_v1,
                embedding_v2=list(embedding),
                embedding_v2_target_model_id=model_id,
            )

    def active_embedding(self, memory_id: str) -> list[float] | None:
        """
        Simulate live search/recall reads against the **authoritative** column.

        Strategy A: ``embedding_v1`` is always the serving vector.  The worker fills
        ``embedding_v2`` in the shadow slot; after :meth:`commit_primary_to_v2`, the
        promoted vectors live in ``embedding_v1`` again — reads never observe a hole.
        """
        row = self._rows.get(memory_id)
        if row is None:
            return None
        return row.embedding_v1

    def commit_primary_to_v2(
        self,
        *,
        quality_gate_fn: Callable[[], float] | None = None,
        quality_threshold: float = 0.7,
    ) -> None:
        """Atomic logical swap — only after quality gate passes (roadmap §2.1)."""
        if self.phase == MigrationPhase.ABORTED:
            raise RuntimeError("cannot commit an aborted migration")
        if self._pending.qsize() > 0:
            raise RuntimeError(
                f"Cannot commit migration: {self._pending.qsize()} items are still pending. "
                "Drain the queue with process_batch() before committing."
            )
        if quality_gate_fn is not None:
            score = quality_gate_fn()
            if score < quality_threshold:
                raise RuntimeError(
                    f"Quality gate failed: neighbor overlap {score:.3f} < "
                    f"threshold {quality_threshold:.3f}. Run abort_and_clear_pending_v2() "
                    "to revert or investigate the model change."
                )
        for mid, row in list(self._rows.items()):
            if row.embedding_v2 is None:
                continue
            self._rows[mid] = MemoryEmbeddingRow(
                memory_id=row.memory_id,
                canonical_text=row.canonical_text,
                embedding_v1=list(row.embedding_v2),
                embedding_v2=None,
                embedding_v2_target_model_id=None,
            )
        self.phase = MigrationPhase.COMMITTED

    def abort_and_clear_pending_v2(self) -> None:
        """Revert pending v2 fills; restores v1-only rows (TEST-2.1-04 spirit)."""
        self.phase = MigrationPhase.ABORTED
        for mid, row in list(self._rows.items()):
            self._rows[mid] = MemoryEmbeddingRow(
                memory_id=row.memory_id,
                canonical_text=row.canonical_text,
                embedding_v1=row.embedding_v1,
                embedding_v2=None,
                embedding_v2_target_model_id=None,
            )
        while not self._pending.empty():
            try:
                self._pending.get_nowait()
            except asyncio.QueueEmpty:
                break


EmbeddingFn = Callable[..., list[float]]
"""Embedder callable: accepts text as positional arg; dimension passed as keyword."""

_MAX_BATCH_SIZE: int = 1_000


class GateSampleTooSmall(ValueError):
    """Raised when the neighbor-overlap gate cannot evaluate enough comparable pairs.

    A migration with an *active* model that yields fewer than a sane minimum of
    scored (old-vs-new) neighbour pairs is a degenerate sample — it carries no
    statistical signal about retrieval quality.  Treating it as a vacuous 1.0
    PASS would let a bad model swap through unchecked, so we fail closed instead.

    Distinct from the legitimate *first-time-setup* case (no active model), which
    :func:`compute_neighbor_overlap` still allows by returning ``1.0``.
    """


async def compute_neighbor_overlap(
    pg_pool: Any,
    *,
    migration_id: str,
    sample: int,
    k: int,
) -> float:
    """Compute mean Jaccard neighbor-overlap for a random sample of migrated memories.

    Queries ``memory_embeddings`` for the old (active) and new (target) model, fetches
    the k-nearest-neighbour IDs for each sampled memory using the pgvector ``<=>``
    cosine operator, and returns the mean ``neighbor_overlap_fraction`` across all
    sampled pairs.

    First-time setup (no active model) legitimately returns ``1.0`` — there is no
    prior retrieval to corrupt.  Once an active model exists, an empty or degenerate
    sample (fewer than ``max(1, sample // 2)`` comparable pairs scored, including the
    zero-scored-pairs case) is **not** a vacuous PASS: it raises
    :class:`GateSampleTooSmall` so the commit gate fails closed.

    Raises ``ValueError`` when the migration is not found or has no target model,
    and :class:`GateSampleTooSmall` on a degenerate sample (see above).

    This function is integration-path only (requires a live asyncpg pool).
    Pure-unit callers must monkeypatch / mock it.
    """
    async with pg_pool.acquire(timeout=10.0) as conn:
        mig_row = await conn.fetchrow(
            "SELECT target_model_id FROM embedding_migrations WHERE id = $1::uuid",
            migration_id,
        )
        if not mig_row:
            raise ValueError(f"Migration {migration_id!r} not found")
        target_model_id = str(mig_row["target_model_id"])

        active_model_id_val = await conn.fetchval(
            "SELECT id FROM embedding_models WHERE status = 'active' LIMIT 1"
        )
        if active_model_id_val is None:
            # First-time-setup: no active model means there is no prior embedding
            # space to compare against, so the gate is vacuously (and legitimately)
            # satisfied.  This is the ONLY path that returns 1.0 without scoring.
            return 1.0
        active_model_id = str(active_model_id_val)

        # Minimum number of comparable (old-vs-new) neighbour pairs we must be able
        # to score before the mean overlap carries any signal.  An active model with
        # fewer than this is a degenerate sample → fail closed.
        min_pairs = max(1, sample // 2)

        sample_ids: list[str] = [
            str(r["memory_id"])
            for r in await conn.fetch(
                """
                SELECT me.memory_id
                FROM memory_embeddings me
                WHERE me.model_id = $1::uuid
                  AND me.embedding IS NOT NULL
                ORDER BY random()
                LIMIT $2
                """,
                target_model_id,
                sample,
            )
        ]

        if len(sample_ids) < min_pairs:
            raise GateSampleTooSmall(
                f"Neighbor-overlap gate sample too small: {len(sample_ids)} target-model "
                f"embeddings available, need at least {min_pairs} (sample={sample}). "
                "An active model exists, so this is NOT a vacuous pass — refusing the "
                "commit gate. Re-run the backfill or pass force=true to override."
            )

        scores: list[float] = []
        for mid in sample_ids:
            old_vec = await conn.fetchval(
                "SELECT embedding FROM memory_embeddings WHERE memory_id = $1::uuid AND model_id = $2::uuid",
                mid,
                active_model_id,
            )
            new_vec = await conn.fetchval(
                "SELECT embedding FROM memory_embeddings WHERE memory_id = $1::uuid AND model_id = $2::uuid",
                mid,
                target_model_id,
            )
            if old_vec is None or new_vec is None:
                continue

            old_neighbors: list[str] = [
                str(r["memory_id"])
                for r in await conn.fetch(
                    """
                    SELECT me2.memory_id
                    FROM memory_embeddings me2
                    WHERE me2.model_id = $1::uuid
                      AND me2.embedding IS NOT NULL
                      AND me2.memory_id != $2::uuid
                    ORDER BY me2.embedding <=> $3::vector
                    LIMIT $4
                    """,
                    active_model_id,
                    mid,
                    old_vec,
                    k,
                )
            ]
            new_neighbors: list[str] = [
                str(r["memory_id"])
                for r in await conn.fetch(
                    """
                    SELECT me2.memory_id
                    FROM memory_embeddings me2
                    WHERE me2.model_id = $1::uuid
                      AND me2.embedding IS NOT NULL
                      AND me2.memory_id != $2::uuid
                    ORDER BY me2.embedding <=> $3::vector
                    LIMIT $4
                    """,
                    target_model_id,
                    mid,
                    new_vec,
                    k,
                )
            ]
            scores.append(neighbor_overlap_fraction(old_neighbors, new_neighbors))

        if len(scores) < min_pairs:
            # We sampled enough target embeddings but too few had a comparable
            # old-vs-new pair to score (e.g. the active model lacks embeddings for
            # the sampled memories).  Zero or sparse scored pairs ⇒ no signal ⇒
            # fail closed rather than vacuously averaging to (or defaulting to) 1.0.
            raise GateSampleTooSmall(
                f"Neighbor-overlap gate scored only {len(scores)} comparable pairs, "
                f"need at least {min_pairs} (sample={sample}). An active model exists, "
                "so this is NOT a vacuous pass — refusing the commit gate. Re-run the "
                "backfill or pass force=true to override."
            )

        return sum(scores) / len(scores)


_EMBED_MAX_CONCURRENT: int = 8
_EMBED_TIMEOUT_SECONDS: float = 30.0
_MAX_CANONICAL_TEXT_BYTES: int = 32_768
_EMBED_MAX_RETRIES: int = 3


class PostgresAspectReembeddingStore:
    """Postgres aspect store implementing ReembeddingStorePort for aspect backfilling."""

    def __init__(
        self,
        pool: Any,
        aspect: str,
        mongo_client: Any = None,
    ) -> None:
        self.pool = pool
        self.aspect = aspect
        self.mongo_client = mongo_client

    async def pop_pending_ids(self, limit: int) -> list[str]:
        """Fetch code_chunk memories that do not have the target aspect in embedding_aspects."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.id::text
                FROM memories m
                LEFT JOIN embedding_aspects ea ON m.id = ea.memory_id AND ea.aspect = $1
                WHERE m.memory_type = 'code_chunk'
                  AND ea.memory_id IS NULL
                LIMIT $2
                """,
                self.aspect,
                limit,
            )
            return [str(r["id"]) for r in rows]

    async def load_row(self, memory_id: str) -> MemoryEmbeddingRow | None:
        """Load code_chunk memory and resolve canonical text based on aspect type."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, payload_ref, name, filepath, embedding::vector as embedding_vector, namespace_id
                FROM memories
                WHERE id = $1::uuid
                """,
                memory_id,
            )
            if not row:
                return None

            raw_emb = row["embedding_vector"]
            embedding_v1: list[float] = []
            if isinstance(raw_emb, str):
                embedding_v1 = [float(x) for x in raw_emb.strip("[]").split(",") if x.strip()]
            elif isinstance(raw_emb, list):
                embedding_v1 = [float(x) for x in raw_emb]
            elif raw_emb is not None:
                embedding_v1 = [float(x) for x in str(raw_emb).strip("[]").split(",") if x.strip()]

            canonical_text = ""
            if self.aspect == "nl_intent":
                canonical_text = row["name"] or ""
            elif self.aspect == "code_intent":
                ref = row["payload_ref"]
                ns_id = row["namespace_id"]
                if ref and len(ref) == 24 and ns_id and self.mongo_client is not None:
                    from bson import ObjectId

                    from nce.db_utils import scoped_mongo_session

                    try:
                        async with scoped_mongo_session(self.mongo_client, ns_id) as s_db:
                            doc = await s_db.code_files.find_one(
                                {"_id": ObjectId(ref)}, {"raw_code": 1}
                            )
                            if doc:
                                canonical_text = doc.get("raw_code", "")
                    except Exception as exc:
                        log.warning(
                            "Aspect backfill: MongoDB query failed for %s: %s", memory_id, exc
                        )

                if not canonical_text:
                    canonical_text = row["filepath"] or ""

            return MemoryEmbeddingRow(
                memory_id=memory_id,
                canonical_text=canonical_text,
                embedding_v1=embedding_v1,
            )

    async def write_embedding_v2(
        self,
        memory_id: str,
        *,
        embedding: Sequence[float],
        model_id: str,
    ) -> None:
        """Write the aspect embedding to embedding_aspects companion table."""
        async with self.pool.acquire() as conn:
            namespace_id = await conn.fetchval(
                "SELECT namespace_id FROM memories WHERE id = $1::uuid",
                memory_id,
            )
            from nce.db_utils import scoped_pg_session, unmanaged_pg_connection

            async with (
                scoped_pg_session(self.pool, str(namespace_id))
                if namespace_id
                else unmanaged_pg_connection(self.pool, site="reembedding.aspects.backfill")
            ) as session_conn:
                await session_conn.execute(
                    """
                    INSERT INTO embedding_aspects (memory_id, aspect, embedding, namespace_id)
                    VALUES ($1::uuid, $2, $3::vector, $4::uuid)
                    ON CONFLICT (memory_id, aspect)
                    DO UPDATE SET embedding = $3::vector
                    """,
                    memory_id,
                    self.aspect,
                    json.dumps(list(embedding)),
                    namespace_id,
                )
