import hashlib
import math
from datetime import datetime, timezone
from typing import Any

import asyncpg

# Maximum safe exponent argument for math.exp(-x).  exp(-709) ≈ 5e-308 (near
# float64 underflow); exp(-710) raises OverflowError on some platforms.
# Clamping the exponent at this value means heavily-decayed scores floor at
# ~5e-308 * s_last rather than crashing.  In practice any score that low is
# indistinguishable from 0.0 for ranking purposes.
_MAX_DECAY_EXPONENT: float = 709.0

# Deterministic jitter range applied to half_life_days to spread decay curves
# across memories and prevent GC thundering-herd lock contention.
_JITTER_RANGE: float = 0.10  # +/- 5%


def _jitter_factor(memory_id: str) -> float:
    """Deterministic jitter in [-0.05, +0.05] derived from ``memory_id``.

    Uses SHA-256 of the memory ID to produce a stable, repeatable offset.
    The same ``memory_id`` always yields the same jitter factor across
    processes and runs.
    """
    digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
    # First 8 hex chars → uint32 → normalise to [0, 1)
    seed_val = int(digest[:8], 16) / 0xFFFFFFFF
    # Map to [-JITTER_RANGE/2, +JITTER_RANGE/2] — i.e. +/- 5% by default
    return (seed_val - 0.5) * _JITTER_RANGE


def compute_decayed_score(
    s_last: float,
    updated_at: datetime,
    half_life_days: float,
    *,
    now: datetime | None = None,
    memory_id: str | None = None,
) -> float:
    """
    Computes the decayed salience score using the Ebbinghaus forgetting curve.
    s(t) = s_last * exp(-lambda * delta_t_days)
    where lambda = ln(2) / half_life_days

    Pass ``now`` in tests for deterministic evaluation; production callers omit it.

    When ``memory_id`` is provided, a deterministic jitter (+/- 5% by default)
    is applied to ``half_life_days`` so that memories injected simultaneously
    do not all hit the GC threshold at the same millisecond.  The jitter is
    derived from a SHA-256 hash of the memory ID and is stable across runs.

    Resilience guarantees
    ---------------------
    * ``half_life_days <= 0``  — returns ``s_last`` unchanged (no-op decay).
    * ``delta_t < 0``          — clock skew / future timestamp; clamped to 0.0
                                 so the score is returned unmodified.
    * ``delta_t == 0``         — exp(0) == 1.0; score returned unchanged.
    * Very large ``delta_t``   — exponent clamped to ``_MAX_DECAY_EXPONENT``
                                 to prevent ``OverflowError`` from ``math.exp``.
    """
    if half_life_days <= 0:
        return s_last

    # Apply deterministic jitter to spread decay curves and prevent GC
    # thundering-herd lock contention when many memories share the same
    # updated_at timestamp.
    effective_half_life = half_life_days
    if memory_id is not None:
        jitter = _jitter_factor(memory_id)
        effective_half_life = half_life_days * (1.0 + jitter)
        # Guard against pathological jitter pushing half-life to zero or negative
        if effective_half_life <= 0:
            effective_half_life = half_life_days * 0.01

    ref = now if now is not None else datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    delta_t_raw = (ref - updated_at).total_seconds() / 86400.0
    # Clamp to zero: negative values mean updated_at is in the future (clock
    # skew across microservices).  Clamping returns the unmodified score rather
    # than a boosted one, which is the safe default.
    delta_t = max(0.0, delta_t_raw)

    decay_constant = math.log(2) / effective_half_life
    # Clamp the exponent to avoid OverflowError when delta_t is astronomically
    # large (e.g. bad timestamps, year-2038 overflow, test data with epoch=0).
    exponent = min(decay_constant * delta_t, _MAX_DECAY_EXPONENT)
    return s_last * math.exp(-exponent)


def ranking_score(cosine_sim: float, salience: float, alpha: float) -> float:
    """
    Computes the final ranking score combining cosine similarity and salience.
    final_score = cosine_similarity * (alpha + (1 - alpha) * salience_score)
    """
    # Ensure values are within bounds
    cosine_sim = max(0.0, min(1.0, float(cosine_sim)))
    salience = max(0.0, min(1.0, float(salience)))
    alpha = max(0.0, min(1.0, float(alpha)))

    return cosine_sim * (alpha + (1.0 - alpha) * salience)


async def fetch_llm_payload(uri: str) -> dict[str, Any]:
    """Fetch ``{prompt, response}`` JSON from MinIO using ``bucket/object_key`` URIs."""

    # Import here — ``replay`` pulls heavy deps; keep ``salience`` import surface light at load time.
    from nce.replay import fetch_llm_payload as _replay_fetch_llm_payload

    return await _replay_fetch_llm_payload(uri)


async def reinforce(
    conn: asyncpg.Connection,
    memory_id: str,
    agent_id: str,
    namespace_id: str,
    delta: float = 0.05,
) -> None:
    """
    Reinforces a memory's salience score on retrieval.
    s_new = min(1.0, s_current + reinforcement_delta)
    """
    await conn.execute(
        """
        INSERT INTO memory_salience (memory_id, agent_id, namespace_id, salience_score, updated_at, access_count)
        VALUES ($1::uuid, $2, $3::uuid, LEAST(1.0, $4::real), NOW(), 1)
        ON CONFLICT (memory_id, agent_id) DO UPDATE
            SET salience_score = LEAST(1.0, memory_salience.salience_score + $4::real),
                updated_at = NOW(),
                access_count = memory_salience.access_count + 1
        """,
        memory_id,
        agent_id,
        namespace_id,
        delta,
    )
