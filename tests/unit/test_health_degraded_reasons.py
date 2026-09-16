"""Wave I-13 — a degraded verdict must say why, and a blocking one must leave rotation.

The defect this pins, measured on the running stack 2026-09-16:

    docker exec nce-a2a  GET /health   ->  HTTP 200, body {"status": "degraded", ...}
    docker inspect                     ->  healthy x13, five days uptime

``check_health`` assigned ``status = "degraded"`` at 18 separate sites and recorded nothing
about which one fired, so an operator saw ``degraded`` with every visible sub-key reading
healthy. The compose healthchecks called ``urlopen()`` and ignored the body, so a 200 with a
degraded payload passed. Three layers, each deaf in the same way.

These tests are the positive controls. Each one asserts the *failing* direction, because a
health check that cannot go red is the thing being fixed.
"""

from __future__ import annotations

import pytest

from nce.orchestrator import _degrade, health_is_blocking


def _fresh() -> dict:
    return {"status": "ok", "degraded_reasons": []}


# ---------------------------------------------------------------------------
# The reason is recorded, not just the verdict
# ---------------------------------------------------------------------------


def test_degrade_records_the_reason_not_only_the_status() -> None:
    """The whole point: 'degraded' with no reason is what made this invisible."""
    h = _fresh()
    _degrade(h, "signing_key_decrypt_failed", blocking=True)

    assert h["status"] == "degraded"
    assert h["degraded_reasons"] == [{"reason": "signing_key_decrypt_failed", "blocking": True}]


def test_multiple_degradations_all_survive() -> None:
    """A second degradation must not overwrite the first — an operator needs both."""
    h = _fresh()
    _degrade(h, "redis_unreachable", blocking=True)
    _degrade(h, "rls_role_posture", blocking=False)

    reasons = [r["reason"] for r in h["degraded_reasons"]]
    assert reasons == ["redis_unreachable", "rls_role_posture"]


def test_degrade_works_when_the_key_is_absent() -> None:
    """``setdefault`` path — a payload built elsewhere must not raise."""
    h: dict = {"status": "ok"}
    _degrade(h, "mongo_unreachable", blocking=True)
    assert h["degraded_reasons"] == [{"reason": "mongo_unreachable", "blocking": True}]


# ---------------------------------------------------------------------------
# blocking vs non-blocking — this is what decides 503
# ---------------------------------------------------------------------------


def test_healthy_payload_is_not_blocking() -> None:
    assert health_is_blocking(_fresh()) is False


def test_blocking_degradation_is_blocking() -> None:
    h = _fresh()
    _degrade(h, "postgres_unreachable", blocking=True)
    assert health_is_blocking(h) is True


def test_non_blocking_degradation_does_not_take_the_container_out_of_rotation() -> None:
    """🔴 Standing positive control.

    ``rls_role_posture`` is degraded on this estate **right now** — the connecting role can
    bypass RLS and 86 tenant tables have no policy targeting it. It is a real finding and it
    is tracked as an estate row, but it is a posture warning, not a liveness failure.

    If this ever becomes blocking, every container goes unhealthy the moment the wave lands
    and the deploy gate starts failing on a condition that has been true for weeks. That is a
    product decision, not a refactor — so it is pinned here.
    """
    h = _fresh()
    _degrade(h, "rls_role_posture", blocking=False)

    assert h["status"] == "degraded"
    assert health_is_blocking(h) is False


def test_one_blocking_reason_among_many_still_blocks() -> None:
    """A blocking reason must not be masked by non-blocking company."""
    h = _fresh()
    _degrade(h, "rls_role_posture", blocking=False)
    _degrade(h, "embeddings_backend_unreachable", blocking=False)
    _degrade(h, "event_chain_corrupted", blocking=True)

    assert health_is_blocking(h) is True


# ---------------------------------------------------------------------------
# The severity split is a decision, so pin the decision itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "mongo_unreachable",
        "postgres_unreachable",
        "redis_unreachable",
        "signing_key_absent",
        "signing_key_decrypt_failed",
        "event_chain_corrupted",
        "chain_verify_failed",
        "event_signature_tampered",
        "signature_verify_failed",
        "rls_read_failed",
        "no_postgres_pool",
    ],
)
def test_integrity_and_liveness_failures_are_blocking(reason: str) -> None:
    """These mean the process cannot do its job safely. It should leave rotation."""
    h = _fresh()
    _degrade(h, reason, blocking=True)
    assert health_is_blocking(h) is True, f"{reason} must be blocking"


@pytest.mark.parametrize(
    "reason",
    ["rls_role_posture", "rls_role_posture_probe_failed", "embeddings_backend_unreachable"],
)
def test_posture_and_capability_warnings_are_not_blocking(reason: str) -> None:
    """Report these loudly; do not restart the stack over them.

    ``embeddings_backend_unreachable`` is the stub-embeddings state: every memory stored today
    carries a zero vector. That is worth seeing in the payload and is not a reason to take the
    container out of rotation while the decision on real embeddings is still open.
    """
    h = _fresh()
    _degrade(h, reason, blocking=False)
    assert health_is_blocking(h) is False, f"{reason} must not be blocking"


# ---------------------------------------------------------------------------
# Guard the guard — prove check_health is actually wired to the helper
# ---------------------------------------------------------------------------


def test_check_health_has_no_bare_status_assignments_left() -> None:
    """🔴 The ratchet.

    Every degradation must go through ``_degrade`` so it carries a reason. A bare
    ``health["status"] = "degraded"`` is a silent one, which is the original defect. This is
    shrink-only in spirit: the count may fall to zero, never rise.
    """
    import inspect

    from nce import orchestrator

    src = inspect.getsource(orchestrator.NCEEngine.check_health)
    bare = [line.strip() for line in src.splitlines() if 'health["status"] = "degraded"' in line]
    assert not bare, (
        f"{len(bare)} bare status assignment(s) left in check_health — route them through "
        "_degrade() so the payload records why it degraded:\n  " + "\n  ".join(bare)
    )


def test_check_health_initialises_the_reason_list() -> None:
    """A consumer reading ``degraded_reasons`` must never meet a missing key."""
    import inspect

    from nce import orchestrator

    src = inspect.getsource(orchestrator.NCEEngine.check_health)
    assert '"degraded_reasons": []' in src
