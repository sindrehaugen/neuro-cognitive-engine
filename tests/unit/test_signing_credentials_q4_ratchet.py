"""
tests/unit/test_signing_credentials_q4_ratchet.py
=================================================
Ratchet test suite for Wave Q-4: Operator-entered signing provider credentials.

Verifies:
  1. Write-only guarantee: Secrets are accepted on write and NEVER returned in status responses.
  2. Master key AES-256-GCM encryption at rest: Ciphertext stored as BYTEA, plaintext unrecoverable
     without valid master key.
  3. Precedence resolution: secret_env() vs DB credentials tested in both orders (db_first vs env_first).
  4. Redaction & zero secret leakage: Exceptions/logs never record plaintext secrets.
  5. Per-tenant namespace isolation: Cross-tenant queries return nothing.
  6. WORM audit event: append_event() called with actor, provider, and non-secret fingerprint.
  7. Admin API REST handlers & Starlette routing contracts.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from nce.signing import (
    MasterKey,
    SigningKeyDecryptionError,
    decrypt_signing_key,
)
from nce.signing_credentials import (
    compute_secret_fingerprint,
    delete_signing_credential,
    get_signing_credential_status,
    normalize_provider,
    resolve_signing_credential,
    save_signing_credential,
)


class FakeDb:
    """In-memory simulated PostgreSQL table for signing_credentials and event_log."""

    def __init__(self) -> None:
        # (namespace_id, provider) -> dict row
        self.rows: dict[tuple[UUID, str], dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def make_conn(self) -> AsyncMock:
        conn = AsyncMock()

        async def fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
            # save_signing_credential UPSERT query
            if "INSERT INTO signing_credentials" in query:
                ns_id, provider, client_id, encrypted_secret, fingerprint, meta_json = args
                now_str = "2026-09-07T08:00:00+00:00"
                row_id = uuid4()
                row = {
                    "id": row_id,
                    "namespace_id": ns_id,
                    "provider": provider,
                    "client_id": client_id,
                    "encrypted_secret": encrypted_secret,
                    "secret_fingerprint": fingerprint,
                    "metadata": meta_json,
                    "created_at": MagicMock(isoformat=lambda: now_str),
                    "updated_at": MagicMock(isoformat=lambda: now_str),
                }
                self.rows[(ns_id, provider)] = row
                return row

            # get_signing_credential_status by provider
            if "SELECT id, namespace_id, provider" in query and "provider = $2" in query:
                ns_id, provider = args
                return self.rows.get((ns_id, provider))

            # resolve_signing_credential query
            if "SELECT client_id, encrypted_secret" in query:
                ns_id, provider = args
                row = self.rows.get((ns_id, provider))
                if not row:
                    return None
                return {
                    "client_id": row["client_id"],
                    "encrypted_secret": row["encrypted_secret"],
                }

            # delete_signing_credential query
            if "DELETE FROM signing_credentials" in query:
                ns_id, provider = args
                if (ns_id, provider) in self.rows:
                    del self.rows[(ns_id, provider)]
                    return {"id": uuid4()}
                return None

            return None

        async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "SELECT id, namespace_id, provider" in query:
                ns_id = args[0]
                matching = [row for (k_ns, _), row in self.rows.items() if k_ns == ns_id]
                return matching
            return []

        conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        conn.fetch = AsyncMock(side_effect=fake_fetch)
        conn.transaction = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=None),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        return conn


@pytest.fixture
def master_key() -> MasterKey:
    """Provides a deterministic test master key."""
    # 32 bytes of test key material
    raw_key = b"01234567890123456789012345678901"
    return MasterKey(raw_key)


@pytest.fixture
def wrong_master_key() -> MasterKey:
    """Provides a different master key to test decryption failure."""
    raw_key = b"wrongwrongwrongwrongwrongwrong12"
    return MasterKey(raw_key)


@pytest.fixture
def fake_db() -> FakeDb:
    return FakeDb()


# ============================================================================
# 1. WRITE-ONLY CONTRACT TESTS
# ============================================================================


@pytest.mark.asyncio
async def test_signing_credentials_write_only_contract(
    fake_db: FakeDb, master_key: MasterKey
) -> None:
    """Status queries and return payloads must NEVER return plaintext or ciphertext secret."""
    conn = fake_db.make_conn()
    ns_id = uuid4()
    secret = "secret-bankid-operator-token-9988"

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
        save_result = await save_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider="criipto",
            client_id="criipto-client-prod",
            client_secret=secret,
            actor="operator-sindre",
            master_key=master_key,
        )

    # 1. Save response check
    assert save_result["status"] == "ok"
    assert save_result["provider"] == "criipto"
    assert save_result["client_id"] == "criipto-client-prod"
    assert save_result["configured"] is True
    assert save_result["secret_fingerprint"] == "••••9988"
    assert "client_secret" not in save_result
    assert "encrypted_secret" not in save_result

    # 2. Get status response check
    status = await get_signing_credential_status(conn=conn, namespace_id=ns_id, provider="criipto")
    assert isinstance(status, dict)
    assert status["configured"] is True
    assert status["provider"] == "criipto"
    assert status["client_id"] == "criipto-client-prod"
    assert status["secret_fingerprint"] == "••••9988"
    assert "client_secret" not in status
    assert "encrypted_secret" not in status

    # 3. String representation check: secret value must NOT appear anywhere in the serialized JSON
    dumped = json.dumps(status)
    assert secret not in dumped
    assert "client_secret" not in dumped


# ============================================================================
# 2. ENCRYPTION AT REST UNDER MASTER KEY
# ============================================================================


@pytest.mark.asyncio
async def test_signing_credentials_encrypted_at_rest(
    fake_db: FakeDb, master_key: MasterKey, wrong_master_key: MasterKey
) -> None:
    """Raw database inspection reveals ciphertext only; unwraps under correct master key."""
    conn = fake_db.make_conn()
    ns_id = uuid4()
    raw_secret = "criipto-bankid-secret-val-xyz77"

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
        await save_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider="criipto",
            client_id="client-123",
            client_secret=raw_secret,
            master_key=master_key,
        )

    # Inspect the raw row in the database
    raw_row = fake_db.rows.get((ns_id, "criipto"))
    assert raw_row is not None
    encrypted_blob = raw_row["encrypted_secret"]
    assert isinstance(encrypted_blob, (bytes, bytearray))

    # Ciphertext must not match or contain plaintext
    assert raw_secret.encode("utf-8") not in encrypted_blob
    # Must start with wire format prefix (Argon2id or PBKDF2)
    assert encrypted_blob.startswith(b"TC3\x01") or encrypted_blob.startswith(b"TC4\x01")

    # Correct master key unwraps byte-identically
    decrypted = decrypt_signing_key(encrypted_blob, master_key).decode("utf-8")
    assert decrypted == raw_secret

    # Wrong master key fails authentication
    with pytest.raises(SigningKeyDecryptionError):
        decrypt_signing_key(encrypted_blob, wrong_master_key)


# ============================================================================
# 3. PRECEDENCE RESOLUTION: secret_env() vs DATABASE
# ============================================================================


@pytest.mark.asyncio
async def test_precedence_resolution_both_orders(fake_db: FakeDb, master_key: MasterKey) -> None:
    """Test resolution precedence in both orders (db_first and env_first)."""
    conn = fake_db.make_conn()
    ns_id = uuid4()
    provider = "signicat"

    db_secret = "db-saved-signicat-secret-1111"
    env_secret = "env-injected-signicat-secret-2222"

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
        await save_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider=provider,
            client_id="cid-db",
            client_secret=db_secret,
            master_key=master_key,
        )

    # Case A: Both exist, precedence="db_first" -> DB wins
    with patch("nce.signing_credentials.secret_env") as mock_env:
        mock_env.side_effect = lambda k, default="": {
            "NCE_SIGNING_SIGNICAT_CLIENT_ID": "cid-env",
            "NCE_SIGNING_SIGNICAT_CLIENT_SECRET": env_secret,
        }.get(k, default)

        cid, sec, src = await resolve_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider=provider,
            precedence="db_first",
            master_key=master_key,
        )
        assert src == "db"
        assert cid == "cid-db"
        assert sec == db_secret

    # Case B: Both exist, precedence="env_first" -> ENV wins
    with patch("nce.signing_credentials.secret_env") as mock_env:
        mock_env.side_effect = lambda k, default="": {
            "NCE_SIGNING_SIGNICAT_CLIENT_ID": "cid-env",
            "NCE_SIGNING_SIGNICAT_CLIENT_SECRET": env_secret,
        }.get(k, default)

        cid, sec, src = await resolve_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider=provider,
            precedence="env_first",
            master_key=master_key,
        )
        assert src == "env"
        assert cid == "cid-env"
        assert sec == env_secret

    # Case C: DB only, env_first falls back to DB
    with patch("nce.signing_credentials.secret_env", return_value=""):
        cid, sec, src = await resolve_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider=provider,
            precedence="env_first",
            master_key=master_key,
        )
        assert src == "db"
        assert cid == "cid-db"
        assert sec == db_secret

    # Case D: Env only, db_first falls back to Env
    empty_db_conn = FakeDb().make_conn()
    with patch("nce.signing_credentials.secret_env") as mock_env:
        mock_env.side_effect = lambda k, default="": {
            "NCE_SIGNING_SIGNICAT_CLIENT_ID": "cid-env",
            "NCE_SIGNING_SIGNICAT_CLIENT_SECRET": env_secret,
        }.get(k, default)

        cid, sec, src = await resolve_signing_credential(
            conn=empty_db_conn,
            namespace_id=ns_id,
            provider=provider,
            precedence="db_first",
            master_key=master_key,
        )
        assert src == "env"
        assert cid == "cid-env"
        assert sec == env_secret

    # Case E: Neither exists -> returns none
    with patch("nce.signing_credentials.secret_env", return_value=""):
        cid, sec, src = await resolve_signing_credential(
            conn=empty_db_conn,
            namespace_id=ns_id,
            provider=provider,
            precedence="db_first",
            master_key=master_key,
        )
        assert src == "none"
        assert cid is None
        assert sec is None


# ============================================================================
# 4. REDACTION & ZERO-LOGGING ON FAILURE
# ============================================================================


@pytest.mark.asyncio
async def test_never_logged_redaction_on_failure(
    caplog: pytest.LogCaptureFixture, master_key: MasterKey, fake_db: FakeDb
) -> None:
    """A deliberately failing save operation must NEVER leak the secret in logs or error messages."""
    canary_secret = "CANARY_SECRET_SUPER_SENSITIVE_99999"
    failing_conn = fake_db.make_conn()
    # Simulate DB error during upsert
    failing_conn.fetchrow = AsyncMock(side_effect=RuntimeError("Simulated DB connection drop"))

    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError) as exc_info:
        await save_signing_credential(
            conn=failing_conn,
            namespace_id=uuid4(),
            provider="oneflow",
            client_id="oneflow-id",
            client_secret=canary_secret,
            master_key=master_key,
        )

    # Check exception text
    assert canary_secret not in str(exc_info.value)

    # Check all captured log records
    for record in caplog.records:
        assert canary_secret not in record.getMessage()


# ============================================================================
# 5. PER-TENANT NAMESPACE ISOLATION
# ============================================================================


@pytest.mark.asyncio
async def test_tenant_namespace_isolation(fake_db: FakeDb, master_key: MasterKey) -> None:
    """Credentials saved for Tenant A must not be accessible to Tenant B."""
    conn = fake_db.make_conn()
    ns_a = uuid4()
    ns_b = uuid4()

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
        await save_signing_credential(
            conn=conn,
            namespace_id=ns_a,
            provider="criipto",
            client_id="tenant-a-client",
            client_secret="tenant-a-secret-1234",
            master_key=master_key,
        )

    # Tenant A status is configured
    status_a = await get_signing_credential_status(conn=conn, namespace_id=ns_a, provider="criipto")
    assert isinstance(status_a, dict)
    assert status_a["configured"] is True
    assert status_a["client_id"] == "tenant-a-client"

    # Tenant B status is NOT configured
    status_b = await get_signing_credential_status(conn=conn, namespace_id=ns_b, provider="criipto")
    assert isinstance(status_b, dict)
    assert status_b["configured"] is False

    # Tenant B resolving returns none
    with patch("nce.signing_credentials.secret_env", return_value=""):
        cid, sec, src = await resolve_signing_credential(
            conn=conn,
            namespace_id=ns_b,
            provider="criipto",
            precedence="db_first",
            master_key=master_key,
        )
        assert src == "none"
        assert cid is None
        assert sec is None


# ============================================================================
# 6. WORM AUDIT EVENT ON WRITE
# ============================================================================


@pytest.mark.asyncio
async def test_worm_audit_event_appended_on_write(fake_db: FakeDb, master_key: MasterKey) -> None:
    """An immutable config_changed WORM event is appended with actor and fingerprint, without secret."""
    conn = fake_db.make_conn()
    ns_id = uuid4()
    secret = "my-super-secret-bankid-key-7788"

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock) as mock_append:
        await save_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider="criipto",
            client_id="cid-prod-88",
            client_secret=secret,
            actor="admin@enterprise.no",
            master_key=master_key,
        )

        mock_append.assert_called_once()
        call_kwargs = mock_append.call_args.kwargs

        assert call_kwargs["namespace_id"] == ns_id
        assert call_kwargs["agent_id"] == "admin@enterprise.no"
        assert call_kwargs["event_type"] == "config_changed"

        params = call_kwargs["params"]
        assert params["actor"] == "admin@enterprise.no"
        changes = params["changes"]["signing_credentials"]
        assert changes["provider"] == "criipto"
        assert changes["client_id"] == "cid-prod-88"
        assert changes["secret_fingerprint"] == "••••7788"
        assert changes["action"] == "saved"

        # Plaintext secret MUST NOT be present in params
        params_str = json.dumps(params)
        assert secret not in params_str


@pytest.mark.asyncio
async def test_delete_signing_credential(fake_db: FakeDb, master_key: MasterKey) -> None:
    """Deleting signing credentials removes the row and records a deletion audit event."""
    conn = fake_db.make_conn()
    ns_id = uuid4()
    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
        await save_signing_credential(
            conn=conn,
            namespace_id=ns_id,
            provider="criipto",
            client_id="cid-to-delete",
            client_secret="secret-to-delete",
            master_key=master_key,
        )

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock) as mock_append:
        del_res = await delete_signing_credential(
            conn=conn, namespace_id=ns_id, provider="criipto", actor="admin"
        )
        assert del_res["status"] == "ok"
        mock_append.assert_called_once()
        changes = mock_append.call_args.kwargs["params"]["changes"]["signing_credentials"]
        assert changes["action"] == "deleted"


# ============================================================================
# 7. FINGERPRINT & PROVIDER HELPER TESTS
# ============================================================================


def test_compute_secret_fingerprint() -> None:
    assert compute_secret_fingerprint("12345678") == "••••5678"
    # A short secret gets a bare mask -- revealing it here would persist it to the
    # WORM event_log. This line used to assert "••••abc", pinning that leak.
    assert compute_secret_fingerprint("abc") == "••••"
    assert compute_secret_fingerprint("") == "••••none"
    assert compute_secret_fingerprint("    ") == "••••none"


def test_normalize_provider() -> None:
    assert normalize_provider("Criipto") == "criipto"
    assert normalize_provider(" SIGNICAT  ") == "signicat"
    assert normalize_provider("OneFlow") == "oneflow"
    with pytest.raises(ValueError):
        normalize_provider("")


# ============================================================================
# 8. ADMIN HTTP HANDLERS TEST
# ============================================================================


@pytest.mark.asyncio
async def test_admin_handlers_save_and_status(fake_db: FakeDb, master_key: MasterKey) -> None:
    """Verify HTTP handlers for save and status."""
    from nce import admin_state
    from nce.admin_handlers.fleet import (
        api_admin_signing_credentials_delete,
        api_admin_signing_credentials_save,
        api_admin_signing_credentials_status,
    )

    mock_pool = MagicMock()
    mock_conn = fake_db.make_conn()
    mock_pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool
    admin_state.engine = mock_engine

    ns_id = uuid4()

    # 1. Test POST save
    save_req = MagicMock(spec=Request)
    save_req.json = AsyncMock(
        return_value={
            "namespace_id": str(ns_id),
            "provider": "criipto",
            "client_id": "test-client-id",
            "client_secret": "my-secret-4321",
            "actor": "sindre-admin",
        }
    )
    save_req.path_params = {}

    with patch("nce.signing_credentials.require_master_key") as mock_rmk:
        mock_rmk.return_value.__enter__.return_value = master_key
        with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
            save_resp = await api_admin_signing_credentials_save(save_req)

    assert isinstance(save_resp, JSONResponse)
    assert save_resp.status_code == 200
    save_body = json.loads(save_resp.body.decode("utf-8"))
    assert save_body["status"] == "ok"
    assert save_body["provider"] == "criipto"
    assert save_body["secret_fingerprint"] == "••••4321"
    assert "client_secret" not in save_body

    # 2. Test GET status
    status_req = MagicMock(spec=Request)
    status_req.path_params = {"namespace_id": str(ns_id)}
    status_req.query_params = {"provider": "criipto"}

    status_resp = await api_admin_signing_credentials_status(status_req)
    assert isinstance(status_resp, JSONResponse)
    assert status_resp.status_code == 200
    status_body = json.loads(status_resp.body.decode("utf-8"))
    assert status_body["configured"] is True
    assert status_body["client_id"] == "test-client-id"
    assert status_body["secret_fingerprint"] == "••••4321"

    # 3. Test DELETE
    del_req = MagicMock(spec=Request)
    del_req.path_params = {"namespace_id": str(ns_id), "provider": "criipto"}
    del_req.query_params = {}

    with patch("nce.signing_credentials.append_event", new_callable=AsyncMock):
        del_resp = await api_admin_signing_credentials_delete(del_req)

    assert isinstance(del_resp, JSONResponse)
    assert del_resp.status_code == 200
    del_body = json.loads(del_resp.body.decode("utf-8"))
    assert del_body["status"] == "ok"


def test_short_secret_is_never_revealed_by_its_fingerprint() -> None:
    """A short secret must not survive into the fingerprint.

    The fingerprint is persisted into ``event_log`` as ``secret_fingerprint`` and
    ``event_log`` is WORM, so anything revealed here is revealed permanently.
    The earlier implementation returned the whole value for inputs of 4 characters
    or fewer.
    """
    from nce.signing_credentials import compute_secret_fingerprint

    for secret in ("a", "ab", "abc", "abcd", "abcde", "abcdef", "abcdefg"):
        fp = compute_secret_fingerprint(secret)
        assert secret not in fp, (
            f"fingerprint {fp!r} contains the whole secret {secret!r} -- this value is "
            "written to the WORM event_log and cannot be removed"
        )
        assert fp == "••••", f"short secrets must get a bare mask, got {fp!r}"

    # Long enough: exactly the last four, and nothing more.
    assert compute_secret_fingerprint("supersecrettail1234") == "••••1234"
    assert compute_secret_fingerprint("") == "••••none"
