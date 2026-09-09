"""``signing_key_rotated`` must be EMITTED, carrying the old/new fingerprint PAIR.

Why this file exists
--------------------
``signing_key_rotated`` was a declared :mod:`nce.event_types` entry with a live
replay handler and **zero producers**:
:func:`nce.admin_mcp_handlers.handle_rotate_signing_key` logged at WARNING and
returned. A log line is not an immutable record -- so a master/signing key
rotation, the single most security-relevant admin operation in the system, left
nothing in ``event_log``.

The recorded blocker ("signing keys are global, so ``append_event`` cannot be
used until a designated system namespace is provisioned") was true: both
``event_log.namespace_id`` and ``audit_log.namespace_id`` are NOT NULL. It is
resolved by migration 065, which seeds the reserved ``_system`` namespace --
the same non-tenant-row pattern ``_global_legacy`` already established.

The fingerprint pair is the POINT, not a nicety. Without it the event says only
"something changed". With it, it answers *"which key was the data written under
after time T"* -- the question that cost hours of trial-decrypting stored
signing-key blobs during the 2026-09-02 incident. A SHA-256 of a key is not
secret; the key is. No test here ever holds real key material, and the payload
carries fingerprints only.

These are unit tests (mocked pool/conn, no live services), so they are collected
by the default ``ci.yml`` pytest job -- there is no ``@pytest.mark.integration``
marker and nothing to wire into ``tests/test_ci_integration_coverage.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import admin_mcp_handlers

_ADMIN_KEY = "test-admin-api-key-for-unit-tests"
_SYSTEM_NS_ID = uuid.UUID("00000000-0000-4000-8000-0000000005ee")

# Deliberately NOT random bytes generated at import time: a fixed, obviously
# fake value keeps the expected fingerprint computable in the assertion and
# makes it impossible for real key material to reach this file.
_OLD_FAKE_KEY = b"\x11" * 32
_NEW_FAKE_KEY = b"\x22" * 32


def _fp(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


class _FakeAcquire:
    __slots__ = ("_conn",)

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _engine_and_conn() -> tuple[MagicMock, AsyncMock]:
    event_log_table: list[dict[str, Any]] = []
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    conn.is_in_transaction = MagicMock(return_value=True)

    async def fake_fetchval(q: Any, *a: Any) -> Any:
        s = str(q)
        if "clock_timestamp()" in s:
            from datetime import datetime, timezone

            return datetime.now(timezone.utc)
        if "chain_hash" in s:
            return b"\x00" * 32
        return _SYSTEM_NS_ID

    async def fake_fetchrow(q: Any, *a: Any) -> Any:
        s = str(q)
        if "event_sequences" in s:
            return {"seq": len(event_log_table) + 1}
        if "INSERT INTO event_log" in s:
            row = {
                "id": a[0],
                "namespace_id": a[1],
                "agent_id": a[2],
                "event_type": a[3],
                "event_seq": a[4],
                "occurred_at": a[5],
                "params": a[6],
                "result_summary": a[7],
            }
            event_log_table.append(row)
            return {
                "id": row["id"],
                "event_seq": row["event_seq"],
                "occurred_at": row["occurred_at"],
            }
        return None

    async def fake_fetch(q: Any, *a: Any) -> list[dict[str, Any]]:
        s = str(q)
        if "event_log" in s:
            if a and isinstance(a[0], str):
                return [r for r in event_log_table if r["event_type"] == a[0]]
            return list(event_log_table)
        return []

    conn.fetchval = AsyncMock(side_effect=fake_fetchval)
    conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
    conn.fetch = AsyncMock(side_effect=fake_fetch)
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.pg_pool.acquire = MagicMock(side_effect=lambda *_a, **_k: _FakeAcquire(conn))
    return engine, conn


@pytest.fixture
def admin_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from nce import auth as auth_mod

    monkeypatch.setenv("NCE_ADMIN_API_KEY", _ADMIN_KEY)
    monkeypatch.setattr(auth_mod.cfg, "NCE_ADMIN_API_KEY", _ADMIN_KEY)
    monkeypatch.setattr(auth_mod.cfg, "NCE_ADMIN_OVERRIDE", False)


def _admin_arguments(extra: dict | None = None) -> dict:
    return {"admin_api_key": os.environ.get("NCE_ADMIN_API_KEY", _ADMIN_KEY), **(extra or {})}


@pytest.mark.asyncio
async def test_rotate_signing_key_emits_signing_key_rotated_with_fingerprint_pair(
    admin_key_env: None,
) -> None:
    engine, conn = _engine_and_conn()

    with (
        patch("nce.signing.rotate_key", AsyncMock(return_value="sk-newnewnewnewne")),
        patch(
            "nce.signing.get_active_key",
            AsyncMock(
                side_effect=[
                    ("sk-oldoldoldoldol", _OLD_FAKE_KEY),
                    ("sk-newnewnewnewne", _NEW_FAKE_KEY),
                ]
            ),
        ),
        patch("nce.event_log.get_active_key", AsyncMock(return_value=("sk-active", _NEW_FAKE_KEY))),
        patch("nce.auth.set_namespace_context", AsyncMock()),
    ):
        raw = await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    # Assert row lands by querying event_log directly
    rows = await conn.fetch("SELECT * FROM event_log WHERE event_type = $1", "signing_key_rotated")
    assert len(rows) == 1, (
        "handle_rotate_signing_key wrote NO event_log row -- a WARNING log line "
        "is not an immutable audit record."
    )
    row = rows[0]
    assert row["event_type"] == "signing_key_rotated"
    assert row["namespace_id"] == _SYSTEM_NS_ID

    params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"])
    assert params["old_key_id"] == "sk-oldoldoldoldol"
    assert params["new_key_id"] == "sk-newnewnewnewne"
    assert params["old_key_fingerprint"] == _fp(_OLD_FAKE_KEY)
    assert params["new_key_fingerprint"] == _fp(_NEW_FAKE_KEY)
    assert params["master_key_fingerprint"] is not None
    assert params["old_key_fingerprint"] != params["new_key_fingerprint"]

    body = json.loads(raw)
    assert body["status"] == "ok"
    assert body["new_key_fingerprint"] == _fp(_NEW_FAKE_KEY)
    assert body["master_key_fingerprint"] == params["master_key_fingerprint"]

    # No key material anywhere in the emitted payload or the handler response.
    blob = json.dumps({"params": params, "body": body})
    assert _OLD_FAKE_KEY.hex() not in blob
    assert _NEW_FAKE_KEY.hex() not in blob


@pytest.mark.asyncio
async def test_audit_event_lands_in_the_same_transaction_as_the_rotation(
    admin_key_env: None,
) -> None:
    """An audit row that can commit while the rotation rolls back is worse than none."""
    engine, conn = _engine_and_conn()
    rotate = AsyncMock(return_value="sk-newnewnewnewne")

    with (
        patch("nce.signing.rotate_key", rotate),
        patch(
            "nce.signing.get_active_key",
            AsyncMock(
                side_effect=[
                    ("sk-oldoldoldoldol", _OLD_FAKE_KEY),
                    ("sk-newnewnewnewne", _NEW_FAKE_KEY),
                ]
            ),
        ),
        patch("nce.event_log.get_active_key", AsyncMock(return_value=("sk-active", _NEW_FAKE_KEY))),
        patch("nce.auth.set_namespace_context", AsyncMock()),
    ):
        await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    assert conn.transaction.call_count == 1, "exactly one handler-owned transaction expected"
    assert rotate.await_args.args[0] is conn

    rows = await conn.fetch("SELECT * FROM event_log WHERE event_type = $1", "signing_key_rotated")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_rotation_still_audits_when_the_outgoing_key_cannot_be_decrypted(
    admin_key_env: None,
) -> None:
    """The undecryptable-old-key case is exactly what an operator rotates OUT of.

    On 2026-09-02 the deployed stack could not decrypt its own active signing
    key for 26h. If fingerprinting the outgoing key were allowed to raise, the
    remediation (rotate) would be blocked by the fault it remediates.
    """
    from nce.signing import SigningKeyDecryptionError

    engine, conn = _engine_and_conn()

    with (
        patch("nce.signing.rotate_key", AsyncMock(return_value="sk-newnewnewnewne")),
        patch(
            "nce.signing.get_active_key",
            AsyncMock(
                side_effect=[
                    SigningKeyDecryptionError("boom"),
                    ("sk-newnewnewnewne", _NEW_FAKE_KEY),
                ]
            ),
        ),
        patch("nce.event_log.get_active_key", AsyncMock(return_value=("sk-active", _NEW_FAKE_KEY))),
        patch("nce.auth.set_namespace_context", AsyncMock()),
    ):
        raw = await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    assert json.loads(raw)["status"] == "ok"
    rows = await conn.fetch("SELECT * FROM event_log WHERE event_type = $1", "signing_key_rotated")
    assert len(rows) == 1
    params = (
        rows[0]["params"] if isinstance(rows[0]["params"], dict) else json.loads(rows[0]["params"])
    )
    assert params["old_key_fingerprint"] is None
    assert params["new_key_fingerprint"] == _fp(_NEW_FAKE_KEY)
    assert params["master_key_fingerprint"] is not None


def test_reserved_system_namespace_is_not_a_tenant() -> None:
    from nce.system_namespace import (
        RESERVED_NON_TENANT_SLUGS,
        SYSTEM_NAMESPACE_SLUG,
        is_tenant_namespace,
    )

    assert SYSTEM_NAMESPACE_SLUG == "_system"
    assert SYSTEM_NAMESPACE_SLUG in RESERVED_NON_TENANT_SLUGS
    assert is_tenant_namespace(SYSTEM_NAMESPACE_SLUG) is False
    assert is_tenant_namespace("acme-corp") is True


@pytest.mark.asyncio
async def test_manage_namespace_list_excludes_the_reserved_system_namespace() -> None:
    """A non-tenant row in ``namespaces`` is a tax on every enumeration.

    ``manage_namespace(command="list")`` is the tenant-facing enumeration, and
    before this wave it was a bare ``SELECT * FROM namespaces`` -- it would hand
    the reserved system namespace to an admin client as if it were a tenant.
    """
    from nce.orchestrators.namespace import NamespaceOrchestrator
    from nce.system_namespace import SYSTEM_NAMESPACE_SLUG

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda *_a, **_k: _FakeAcquire(conn))

    orch = NamespaceOrchestrator(pool)
    out = await orch._list_namespaces(SimpleNamespace())
    assert out == {"namespaces": []}

    sql = conn.fetch.await_args.args[0]
    excluded = conn.fetch.await_args.args[1]
    assert "slug" in sql, "list query does not filter on slug at all"
    assert SYSTEM_NAMESPACE_SLUG in excluded


@pytest.mark.asyncio
async def test_rotation_refuses_rather_than_recording_an_unidentified_key(
    admin_key_env: None,
) -> None:
    """An unloadable master key must FAIL the rotation, not write a fingerprint-less row.

    The first version of this handler wrapped the master-key fingerprint in
    ``except Exception`` and continued with ``master_key_fingerprint: None``. That swallow
    protected nothing: ``master_key_fingerprint`` only hashes the key -- it decrypts
    nothing -- so it fails solely when the key is absent, and ``rotate_key`` itself calls
    ``require_master_key()`` and ``encrypt_signing_key``, so an unloadable key fails the
    rotation regardless.

    The only thing the swallow could produce was an audit row saying "something changed"
    without naming the key the data was written under -- the exact question that went
    unanswered for hours during the 2026-09-02 incident, and the reason the fingerprint
    pair exists at all.

    Note the asymmetry this pins: the OUTGOING key fingerprint is deliberately
    best-effort, because an undecryptable outgoing blob is precisely what an operator
    rotates out of and must never be blocked from remediating. The MASTER key fingerprint
    is not best-effort, because no failure of it leaves the rotation viable.

    HARNESS NOTE, because two earlier versions of this test were vacuous:
    it must patch ``nce.event_log.get_active_key`` and give ``nce.signing.get_active_key``
    both of its two calls, exactly as the emit test above does. Without them the handler
    dies in ``append_event`` with ``NoActiveSigningKeyError`` before it ever reaches the
    write, so BOTH the fixed and the swallowed version raise and write nothing -- and the
    test passes either way while proving nothing.
    """

    from nce.mcp_errors import McpError
    from nce.signing import MasterKeyMissingError

    engine, conn = _engine_and_conn()

    # Patch the fingerprint call ONLY. Patching require_master_key would also break
    # append_event's own use of it -- confounding the test the same way.
    def _fingerprint_unavailable(_mk: object) -> str:
        raise MasterKeyMissingError("NCE_MASTER_KEY is missing or empty.")

    with (
        # Patched on nce.signing rather than on the handler module: the handler imports
        # these names INSIDE the function, so they resolve at call time.
        patch("nce.signing.master_key_fingerprint", _fingerprint_unavailable),
        patch("nce.signing.rotate_key", AsyncMock(return_value="sk-newnewnewnewne")),
        patch(
            "nce.signing.get_active_key",
            AsyncMock(
                side_effect=[
                    ("sk-oldoldoldoldol", _OLD_FAKE_KEY),
                    ("sk-newnewnewnewne", _NEW_FAKE_KEY),
                ]
            ),
        ),
        patch("nce.event_log.get_active_key", AsyncMock(return_value=("sk-active", _NEW_FAKE_KEY))),
        patch("nce.auth.set_namespace_context", AsyncMock()),
        pytest.raises((McpError, MasterKeyMissingError)) as excinfo,
    ):
        await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    # The exception TYPE proves nothing on its own: MCP handlers wrap every internal
    # failure in the same -32603 envelope, so `raises(McpError)` would also be satisfied
    # by unrelated harness breakage. Walk the cause chain and require that the reason is
    # the master key.
    chain: list[type[BaseException]] = []
    cause: BaseException | None = excinfo.value
    while cause is not None:
        chain.append(type(cause))
        cause = cause.__cause__ or cause.__context__
    assert MasterKeyMissingError in chain, (
        "the rotation failed, but not because of the master key -- chain was "
        f"{[c.__name__ for c in chain]}. An McpError from some other cause makes this "
        "assertion vacuous."
    )

    rows = await conn.fetch("SELECT * FROM event_log WHERE event_type = $1", "signing_key_rotated")
    assert rows == [], (
        "a rotation that cannot identify its master key must record NOTHING. An audit row "
        "without the fingerprint pair says only 'something changed', which is precisely "
        "the outcome this event exists to prevent."
    )
