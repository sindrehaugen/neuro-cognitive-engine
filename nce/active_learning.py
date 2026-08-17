from __future__ import annotations

import hashlib
import json
import logging
from uuid import UUID

import asyncpg

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.models import StoreMemoryRequest

# Salience prior injected on operator confirmation.
# Batch 113: this is now the *base* prior; call site multiplies by actor trust.
_QUARANTINE_CONFIRMED_SALIENCE: float = 0.65

log = logging.getLogger("nce.active_learning")


async def get_actor_trust(
    pg_pool: asyncpg.Pool,
    namespace_id: UUID,
    actor_id: str,
) -> float:
    """Return the Laplace-smoothed trust score for *actor_id* in *namespace_id*.

    Falls back to ``cfg.NCE_TRUST_DEFAULT`` when the actor has no row yet.
    Never raises — trust resolution failures degrade gracefully to the default.
    """
    try:
        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT trust FROM actor_trust
                WHERE namespace_id = $1::uuid
                  AND actor_id     = $2
                  AND actor_kind   = 'agent'
                """,
                namespace_id,
                actor_id,
            )
        if row is not None:
            return float(row["trust"])
    except Exception:
        log.warning(
            "[ACTOR-TRUST] Failed to fetch trust for actor=%s ns=%s — using default",
            actor_id,
            namespace_id,
        )
    return cfg.NCE_TRUST_DEFAULT


def quarantine_threshold(operator_trust: float) -> float:
    """Dynamic quarantine threshold: ``0.5 + 0.3 * operator_trust``.

    - High-trust actor (trust → 0.95): threshold ≈ 0.785  (mid-confidence bypasses)
    - Low-trust  actor (trust → 0.1):  threshold ≈ 0.53   (quarantined more aggressively)
    - Default    actor (trust = 0.65): threshold = 0.695

    The result is clamped to [0.5, cfg.NCE_TRUST_QUARANTINE_BYPASS] so the
    ``NCE_TRUST_QUARANTINE_BYPASS`` knob controls the upper bound — its name
    implies the trust score at or above which an actor can bypass quarantine,
    which maps directly to the ceiling of this function.
    """
    return max(0.5, min(cfg.NCE_TRUST_QUARANTINE_BYPASS, 0.5 + 0.3 * operator_trust))


class ActiveLearningManager:
    """
    Manages the Active Learning loop (BATCH-P3-005).
    Stashes low-confidence assertions in active_learning_queue, enables operator
    micro-confirmation / rejection, and provides gamified state tracking statistics.
    """

    def __init__(self, pg_pool: asyncpg.Pool):
        self.pg_pool = pg_pool

    async def quarantine_memory(
        self,
        conn: asyncpg.Connection,
        payload: StoreMemoryRequest,
        confidence_score: float,
    ) -> UUID:
        """
        Quarantines a memory request by stashing the serialized payload in active_learning_queue.
        Returns the stashed queue item ID (UUID).
        """
        # Serialize the StoreMemoryRequest payload
        serialized_payload = payload.model_dump_json()

        queue_id = await conn.fetchval(
            """
            INSERT INTO active_learning_queue (namespace_id, agent_id, payload, confidence_score, status, created_at)
            VALUES ($1::uuid, $2, $3::jsonb, $4::real, 'pending', NOW())
            RETURNING id
            """,
            payload.namespace_id,
            payload.agent_id,
            serialized_payload,
            confidence_score,
        )
        log.info(
            "[ACTIVE-LEARNING] Quarantined memory request with confidence %f. queue_id=%s namespace=%s",
            confidence_score,
            queue_id,
            payload.namespace_id,
        )
        return queue_id

    async def confirm_memory(
        self,
        namespace_id: UUID | str,
        queue_item_id: UUID | str,
        operator_id: str,
        memory_orchestrator,
    ) -> dict:
        """
        Promotes a quarantined memory by loading its stashed payload, modifying its metadata
        to bypass quarantine, and calling the memory orchestrator's store_memory method.
        Marks the queue item as confirmed.
        """
        ns_uuid = UUID(str(namespace_id))
        item_uuid = UUID(str(queue_item_id))

        # 1. Fetch stashed payload and mark resolved, then release database connection A
        async with scoped_pg_session(self.pg_pool, ns_uuid) as conn:
            # Check queue item
            row = await conn.fetchrow(
                """
                SELECT payload, status, agent_id FROM active_learning_queue
                WHERE id = $1::uuid AND namespace_id = $2::uuid
                FOR UPDATE
                """,
                item_uuid,
                ns_uuid,
            )
            if not row:
                raise ValueError(
                    f"Queue item {queue_item_id} not found in namespace {namespace_id}"
                )
            if row["status"] != "pending":
                raise ValueError(f"Queue item {queue_item_id} is already in state: {row['status']}")

            # Self-confirm guard: operator must differ from the authoring agent so a
            # compromised agent cannot ratchet its own trust by confirming its own items.
            authoring_agent_id: str = row["agent_id"] or ""
            if operator_id == authoring_agent_id:
                raise ValueError(
                    "Self-confirm denied: operator must differ from the authoring agent"
                )

            await conn.execute(
                """
                UPDATE active_learning_queue
                SET status = 'confirmed', resolved_at = NOW(), resolved_by = $1
                WHERE id = $2::uuid AND namespace_id = $3::uuid
                """,
                operator_id,
                item_uuid,
                ns_uuid,
            )

        # 2. De-serialize and reconstruct request payload
        payload_data = (
            json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        )

        # Add bypass flag to metadata
        if "metadata" not in payload_data or payload_data["metadata"] is None:
            payload_data["metadata"] = {}
        payload_data["metadata"]["bypass_quarantine"] = True

        # Reconstruct StoreMemoryRequest
        req = StoreMemoryRequest(**payload_data)

        # 3. Store memory (acquires connection B)
        try:
            store_res = await memory_orchestrator.store_memory(req)
            log.info(
                "[ACTIVE-LEARNING] Confirmed memory in queue_item=%s by operator=%s. Result memory_ref=%s",
                queue_item_id,
                operator_id,
                store_res.get("payload_ref"),
            )
        except Exception as e:
            # Revert queue item back to pending on failure
            async with scoped_pg_session(self.pg_pool, ns_uuid) as conn:
                await conn.execute(
                    """
                    UPDATE active_learning_queue
                    SET status = 'pending', resolved_at = NULL, resolved_by = NULL
                    WHERE id = $1::uuid AND namespace_id = $2::uuid
                    """,
                    item_uuid,
                    ns_uuid,
                )
            raise e

        # 4. Set initial salience prior and emit WORM quarantine_confirmed event.
        #    Both writes share a single connection/transaction so they are atomic.
        payload_ref: str | None = store_res.get("payload_ref")
        async with scoped_pg_session(self.pg_pool, ns_uuid) as conn:
            async with conn.transaction():
                # 4a. Salience upsert — look up the postgres memory UUID from the
                #     payload_ref (MongoDB ObjectId) then set the confirmed prior.
                if payload_ref:
                    pg_memory_id = await conn.fetchval(
                        "SELECT id FROM memories WHERE payload_ref = $1 AND namespace_id = $2::uuid LIMIT 1",
                        payload_ref,
                        str(ns_uuid),
                    )
                    if pg_memory_id:
                        await conn.execute(
                            """
                            INSERT INTO memory_salience
                                (memory_id, agent_id, namespace_id, salience_score, updated_at, access_count)
                            VALUES ($1::uuid, $2, $3::uuid, $4::real, NOW(), 1)
                            ON CONFLICT (memory_id, agent_id) DO UPDATE
                                SET salience_score = GREATEST(memory_salience.salience_score,
                                                              EXCLUDED.salience_score),
                                    updated_at = NOW()
                            """,
                            pg_memory_id,
                            req.agent_id,
                            str(ns_uuid),
                            _QUARANTINE_CONFIRMED_SALIENCE,
                        )

                # 4b. WORM event — quarantine_confirmed.
                from nce import event_log as _el  # local import avoids circular dep at module load

                await _el.append_event(
                    conn=conn,
                    namespace_id=ns_uuid,
                    agent_id=operator_id,
                    event_type="quarantine_confirmed",
                    params={
                        "queue_item_id": str(item_uuid),
                        "agent_id": req.agent_id,
                        "operator_id": operator_id,
                    },
                )

        return store_res

    async def reject_memory(
        self,
        namespace_id: UUID | str,
        queue_item_id: UUID | str,
        operator_id: str,
    ) -> None:
        """
        Discards a quarantined memory by marking it as rejected in the queue.

        Appends a WORM ``quarantine_rejected`` event carrying the SHA-256 hash
        of the stashed payload.  The raw payload is never written to the event log.
        """
        ns_uuid = UUID(str(namespace_id))
        item_uuid = UUID(str(queue_item_id))

        async with scoped_pg_session(self.pg_pool, ns_uuid) as conn:
            row = await conn.fetchrow(
                """
                SELECT status, payload, agent_id FROM active_learning_queue
                WHERE id = $1::uuid AND namespace_id = $2::uuid
                FOR UPDATE
                """,
                item_uuid,
                ns_uuid,
            )
            if not row:
                raise ValueError(
                    f"Queue item {queue_item_id} not found in namespace {namespace_id}"
                )
            if row["status"] != "pending":
                raise ValueError(f"Queue item {queue_item_id} is already in state: {row['status']}")

            # Self-reject guard: operator must differ from the authoring agent so an
            # agent cannot inflate rivals' rejection counts by rejecting its own items.
            reject_authoring_agent_id: str = row["agent_id"] or ""
            if operator_id == reject_authoring_agent_id:
                raise ValueError(
                    "Self-reject denied: operator must differ from the authoring agent"
                )

            # Hash the stashed payload before discarding — raw bytes never leave this scope.
            raw_payload_bytes = (
                row["payload"].encode("utf-8")
                if isinstance(row["payload"], str)
                else json.dumps(row["payload"]).encode("utf-8")
            )
            payload_sha256 = hashlib.sha256(raw_payload_bytes).hexdigest()
            stored_agent_id: str = row["agent_id"] or ""

            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE active_learning_queue
                    SET status = 'rejected', resolved_at = NOW(), resolved_by = $1
                    WHERE id = $2::uuid AND namespace_id = $3::uuid
                    """,
                    operator_id,
                    item_uuid,
                    ns_uuid,
                )

                # WORM event — carries hash only; raw payload never persisted.
                from nce import event_log as _el  # local import avoids circular dep at module load

                await _el.append_event(
                    conn=conn,
                    namespace_id=ns_uuid,
                    agent_id=operator_id,
                    event_type="quarantine_rejected",
                    params={
                        "queue_item_id": str(item_uuid),
                        "agent_id": stored_agent_id,
                        "operator_id": operator_id,
                        "payload_sha256": payload_sha256,
                    },
                )

            log.info(
                "[ACTIVE-LEARNING] Rejected memory in queue_item=%s by operator=%s (sha256=%s)",
                queue_item_id,
                operator_id,
                payload_sha256,
            )

    async def get_pending_queue(self, namespace_id: UUID | str) -> list[dict]:
        """
        Returns a list of all pending items in the confirmation queue for a namespace.
        """
        ns_uuid = UUID(str(namespace_id))
        async with scoped_pg_session(self.pg_pool, ns_uuid) as conn:
            rows = await conn.fetch(
                """
                SELECT id, agent_id, payload, confidence_score, created_at
                FROM active_learning_queue
                WHERE namespace_id = $1::uuid AND status = 'pending'
                ORDER BY created_at ASC
                """,
                ns_uuid,
            )
            results = []
            for r in rows:
                try:
                    payload_parsed = (
                        json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
                    )
                except Exception as err:
                    log.error(
                        "[ACTIVE-LEARNING] Failed to parse payload for queue item %s: %s",
                        r["id"],
                        err,
                    )
                    payload_parsed = {}

                results.append(
                    {
                        "id": str(r["id"]),
                        "agent_id": r["agent_id"],
                        "payload": payload_parsed,
                        "confidence_score": float(r["confidence_score"]),
                        "created_at": r["created_at"].isoformat()
                        if hasattr(r["created_at"], "isoformat")
                        else str(r["created_at"]),
                    }
                )
            return results

    async def get_gamified_stats(self, namespace_id: UUID | str, operator_id: str) -> dict:
        """
        Provides state tracking payloads suitable for ingestion by gamified frontend components.
        Calculates counts, accuracy rate, XP points, and confirmation streak.
        """
        ns_uuid = UUID(str(namespace_id))
        async with scoped_pg_session(self.pg_pool, ns_uuid) as conn:
            # Counts
            pending_count = await conn.fetchval(
                "SELECT COUNT(*)::int FROM active_learning_queue WHERE namespace_id = $1::uuid AND status = 'pending'",
                ns_uuid,
            )
            confirmed_count = await conn.fetchval(
                "SELECT COUNT(*)::int FROM active_learning_queue WHERE namespace_id = $1::uuid AND status = 'confirmed'",
                ns_uuid,
            )
            rejected_count = await conn.fetchval(
                "SELECT COUNT(*)::int FROM active_learning_queue WHERE namespace_id = $1::uuid AND status = 'rejected'",
                ns_uuid,
            )

            # Streak calculation: count consecutive resolved items sorted by resolved_at desc
            resolved_rows = await conn.fetch(
                """
                SELECT status, resolved_by FROM active_learning_queue
                WHERE namespace_id = $1::uuid AND status IN ('confirmed', 'rejected')
                ORDER BY resolved_at DESC
                LIMIT 50
                """,
                ns_uuid,
            )

            streak = 0
            # Let's count consecutive confirmations/rejections by this operator
            for r in resolved_rows:
                if r["resolved_by"] == operator_id:
                    streak += 1
                else:
                    break

            # Calculate Experience Points (XP)
            # Confirming a memory gives +10 XP, rejecting gives +5 XP
            # We can query specific operator count or total resolved
            op_confirmed = await conn.fetchval(
                "SELECT COUNT(*)::int FROM active_learning_queue WHERE namespace_id = $1::uuid AND status = 'confirmed' AND resolved_by = $2",
                ns_uuid,
                operator_id,
            )
            op_rejected = await conn.fetchval(
                "SELECT COUNT(*)::int FROM active_learning_queue WHERE namespace_id = $1::uuid AND status = 'rejected' AND resolved_by = $2",
                ns_uuid,
                operator_id,
            )
            xp = (op_confirmed * cfg.NCE_ACTIVE_LEARNING_CONFIRM_XP) + (
                op_rejected * cfg.NCE_ACTIVE_LEARNING_REJECT_XP
            )

            # Levels: 100 XP per level
            level = 1 + (xp // 100)
            next_level_xp = 100 - (xp % 100)

            accuracy = 0.0
            total_resolved = confirmed_count + rejected_count
            if total_resolved > 0:
                accuracy = round(confirmed_count / total_resolved, 4)

            return {
                "pending_count": pending_count,
                "confirmed_count": confirmed_count,
                "rejected_count": rejected_count,
                "operator_stats": {
                    "operator_id": operator_id,
                    "confirmed_count": op_confirmed,
                    "rejected_count": op_rejected,
                    "xp": xp,
                    "level": level,
                    "xp_to_next_level": next_level_xp,
                    "streak": streak,
                },
                "accuracy_rate": accuracy,
            }
