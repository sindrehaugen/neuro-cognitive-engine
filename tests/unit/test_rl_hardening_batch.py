"""RL hardening batch — RL-H19, RL-H3, RL-H12.

Three rows filed against a paused ledger on 2026-09-04 and unfixed for thirteen days.
Each is small; each is the kind an external auditor finds first.

Every test here asserts the *failing* direction, because all three defects were invisible
in the passing direction: the key was present and readable, the query returned rows, the
loader returned a model. Nothing was broken enough to notice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _mock_embeddings_globally():
    """Override the suite-wide autouse fixture for this module.

    ``tests/conftest.py`` patches ``nce.embeddings._load_sentence_transformer`` to a
    ``MagicMock(return_value=None)`` for *every* test, so the real loader is never
    exercised anywhere in the suite. Without this override the RL-H12 tests below assert
    against that mock — a MagicMock grows a ``cache_clear`` attribute on demand and always
    returns ``None``, so they would pass or fail for reasons unrelated to the code.

    Yielding without patching restores the real function for this module only.
    """
    yield


_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# RL-H19 — the master key must not sit in the process environment
# ---------------------------------------------------------------------------


def _compose() -> dict:
    return yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_every_file_secret_service_suppresses_the_env_key() -> None:
    """A service reading the key from a file must blank the env_file's copy.

    The env_file supplies ``NCE_MASTER_KEY`` to every service that includes it. A service
    that also sets ``NCE_MASTER_KEY_FILE`` reads the key from ``/run/secrets`` — but unless
    it explicitly sets ``NCE_MASTER_KEY: ""`` the env_file value still lands in the process
    environment, where it is readable from ``/proc/<pid>/environ``.

    Measured 2026-09-16: ``worker`` and ``cron`` did this correctly; **admin and a2a did
    not** — and those two are the externally-facing processes, which is the wrong way round.
    """
    services = _compose()["services"]
    offenders = []
    for name, svc in services.items():
        env = svc.get("environment") or {}
        if not isinstance(env, dict):
            continue
        if "NCE_MASTER_KEY_FILE" in env and env.get("NCE_MASTER_KEY", "<absent>") != "":
            offenders.append(name)

    assert not offenders, (
        "these services read the master key from a file but leave the env_file copy in "
        f"their process environment: {offenders}. Add `NCE_MASTER_KEY: \"\"` to each."
    )


def test_the_two_externally_facing_services_are_covered() -> None:
    """Standing positive control — admin and a2a specifically.

    Named rather than left to the sweep above, because these two are the reason the row
    exists: they terminate external traffic.
    """
    services = _compose()["services"]
    for name in ("admin", "a2a"):
        env = services[name].get("environment") or {}
        assert env.get("NCE_MASTER_KEY") == "", (
            f"{name} must set NCE_MASTER_KEY to the empty string; it is externally facing "
            "and the key would otherwise be readable in /proc/<pid>/environ"
        )


# ---------------------------------------------------------------------------
# RL-H3 — the wrapped-DEK lookup must be namespace-scoped
# ---------------------------------------------------------------------------


def test_wrapped_dek_lookup_carries_a_namespace_predicate() -> None:
    """🔴 The WHERE clause is the entire tenant control on this deployment.

    RLS is inert as deployed: the connecting role is ``rolsuper``/``rolbypassrls`` and 86
    tenant tables carry no policy targeting it. So a ``SELECT ... FROM memories`` with no
    namespace predicate reads across tenants, and nothing downstream catches it.

    The Mongo half of the same method already scopes via ``scoped_mongo_session``; the
    Postgres half did not.
    """
    src = (_ROOT / "nce" / "graph_query.py").read_text(encoding="utf-8")

    m = re.search(r'"SELECT payload_ref, wrapped_dek FROM memories\s*"?\s*"?([^"]*)"', src)
    assert m, "the wrapped_dek lookup has moved or been rewritten — re-check RL-H3 by hand"

    where = m.group(0)
    assert "namespace_id" in where, (
        "the wrapped_dek lookup has no namespace predicate. RLS will not save it: the "
        "connecting role can bypass RLS. Add `AND namespace_id = $2::uuid`."
    )


def test_dek_lookup_is_skipped_without_a_namespace() -> None:
    """No namespace => no lookup. Degraded is fine; cross-tenant is not.

    ``namespace_id`` is optional on ``_hydrate_sources``. When it is absent the lookup must
    not run at all — every excerpt then falls back to the plaintext path, which is the same
    behaviour as a legacy NULL dek.
    """
    src = (_ROOT / "nce" / "graph_query.py").read_text(encoding="utf-8")
    assert "if ep_docs and namespace_id:" in src, (
        "the DEK lookup must be guarded on namespace_id being present; without it the "
        "query either runs unscoped or binds None as a namespace"
    )


# ---------------------------------------------------------------------------
# RL-H12 — a failed model load must not be memoised
# ---------------------------------------------------------------------------


def test_failed_embedding_load_is_not_cached_for_the_process_lifetime() -> None:
    """A transient failure pinned zero vectors until someone restarted the container.

    ``@lru_cache`` caches whatever the function returns, ``None`` included. Both failure
    paths — missing import, failed load — returned ``None``, so one slow mount or momentary
    OOM meant every memory stored afterwards carried a zero vector, silently.
    """
    import nce.embeddings as emb

    assert hasattr(emb, "_load_sentence_transformer_cached"), (
        "the cached loader should be a separate function so the wrapper can evict failures"
    )
    assert hasattr(emb._load_sentence_transformer_cached, "cache_clear"), (
        "the inner loader should still be lru_cached — successes are worth caching"
    )
    assert not hasattr(emb._load_sentence_transformer, "cache_clear"), (
        "the public loader must NOT be lru_cached directly, or failures are memoised again"
    )


def test_failure_evicts_and_success_persists() -> None:
    """Behavioural proof, not a shape check: a None result must not be served twice."""
    import nce.embeddings as emb

    calls: list[str] = []
    real = emb._load_sentence_transformer_cached

    try:
        # First call fails, second succeeds — the wrapper must actually retry.
        def flaky(device: str):
            calls.append(device)
            return None if len(calls) == 1 else object()

        from functools import lru_cache

        emb._load_sentence_transformer_cached = lru_cache(maxsize=8)(flaky)

        first = emb._load_sentence_transformer("cpu")
        second = emb._load_sentence_transformer("cpu")

        assert first is None
        assert second is not None, (
            "the second call returned the cached None — a transient failure is still "
            "pinned for the process lifetime, which is the defect RL-H12 describes"
        )
        assert len(calls) == 2, f"expected a retry, got {len(calls)} call(s)"
    finally:
        emb._load_sentence_transformer_cached = real
