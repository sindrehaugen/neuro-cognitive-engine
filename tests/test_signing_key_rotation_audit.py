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
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(return_value=_SYSTEM_NS_ID)
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
        patch("nce.auth.set_namespace_context", AsyncMock()),
        patch("nce.event_log.append_event", AsyncMock()) as appended,
    ):
        raw = await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    assert appended.await_count == 1, (
        "handle_rotate_signing_key wrote NO event_log row -- a WARNING log line "
        "is not an immutable audit record."
    )
    kwargs = appended.await_args.kwargs
    assert kwargs["event_type"] == "signing_key_rotated"
    assert kwargs["namespace_id"] == _SYSTEM_NS_ID

    params = kwargs["params"]
    assert params["old_key_id"] == "sk-oldoldoldoldol"
    assert params["new_key_id"] == "sk-newnewnewnewne"
    assert params["old_key_fingerprint"] == _fp(_OLD_FAKE_KEY)
    assert params["new_key_fingerprint"] == _fp(_NEW_FAKE_KEY)
    assert params["old_key_fingerprint"] != params["new_key_fingerprint"]

    body = json.loads(raw)
    assert body["status"] == "ok"
    assert body["new_key_fingerprint"] == _fp(_NEW_FAKE_KEY)

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
        patch("nce.auth.set_namespace_context", AsyncMock()),
        patch("nce.event_log.append_event", AsyncMock()) as appended,
    ):
        await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    assert conn.transaction.call_count == 1, "exactly one handler-owned transaction expected"
    assert rotate.await_args.args[0] is conn
    assert appended.await_args.kwargs["conn"] is conn


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

    engine, _conn = _engine_and_conn()

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
        patch("nce.auth.set_namespace_context", AsyncMock()),
        patch("nce.event_log.append_event", AsyncMock()) as appended,
    ):
        raw = await admin_mcp_handlers.handle_rotate_signing_key(engine, _admin_arguments())

    assert json.loads(raw)["status"] == "ok"
    params = appended.await_args.kwargs["params"]
    assert params["old_key_fingerprint"] is None
    assert params["new_key_fingerprint"] == _fp(_NEW_FAKE_KEY)


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
