from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from nce.bridge_renewal import (
    _acquire_refresh_lock,
    _perform_oauth_refresh,
    _release_refresh_lock,
    ensure_fresh_oauth_token,
    get_token_expiry,
)
from nce.config import cfg
from nce.signing import decrypt_signing_key, encrypt_signing_key, require_master_key


def _generate_jwt(expires_at_dt: datetime) -> str:
    """Generate a valid, mock JWT string with an exp claim."""
    payload = {"exp": int(expires_at_dt.timestamp()), "sub": "mock_sub"}
    return jwt.encode(payload, "x" * 32, algorithm="HS256")


def test_get_token_expiry() -> None:
    # 1. Non-JWT strings
    assert get_token_expiry("not_a_jwt") is None
    assert get_token_expiry("eyJabc.abc") is None

    # 2. Valid JWT string with exp claim
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    token = _generate_jwt(future)
    expiry = get_token_expiry(token)
    assert expiry is not None
    assert abs((expiry - future).total_seconds()) < 2


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_env_token_override() -> None:
    pool = AsyncMock()
    row = MagicMock()
    # If env_token is provided, it should be returned immediately
    res = await ensure_fresh_oauth_token(pool, row, "env_token_override")
    assert res == "env_token_override"


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_no_token_stored() -> None:
    pool = AsyncMock()
    row = {"id": uuid.uuid4(), "provider": "sharepoint", "oauth_access_token_enc": None}
    res = await ensure_fresh_oauth_token(pool, row, "")
    assert res == ""


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_plain_string_compatibility() -> None:
    pool = AsyncMock()
    plain_token = "raw_access_token_compatibility"
    enc = encrypt_signing_key(plain_token.encode("utf-8"), require_master_key())
    row = {"id": uuid.uuid4(), "provider": "sharepoint", "oauth_access_token_enc": enc}
    res = await ensure_fresh_oauth_token(pool, row, "")
    assert res == plain_token


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_valid_and_fresh() -> None:
    pool = AsyncMock()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {
        "access_token": "fresh_access_123",
        "refresh_token": "refresh_token_456",
        "expires_at": expires_at.timestamp(),
    }
    enc = encrypt_signing_key(json.dumps(payload).encode("utf-8"), require_master_key())
    row = {"id": uuid.uuid4(), "provider": "sharepoint", "oauth_access_token_enc": enc}
    res = await ensure_fresh_oauth_token(pool, row, "")
    assert res == "fresh_access_123"


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_background_warning_refresh() -> None:
    pool = AsyncMock()
    # 3 minutes in the future (within 5 minutes warning window but still valid)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    payload = {
        "access_token": "warning_access_123",
        "refresh_token": "refresh_token_456",
        "expires_at": expires_at.timestamp(),
    }
    enc = encrypt_signing_key(json.dumps(payload).encode("utf-8"), require_master_key())
    row = {"id": uuid.uuid4(), "provider": "sharepoint", "oauth_access_token_enc": enc}

    # Use patch to check if bg refresh was scheduled
    with patch("nce.bridge_renewal._bg_refresh_token", new_callable=AsyncMock) as mock_bg:
        res = await ensure_fresh_oauth_token(pool, row, "")
        # Returns current access token immediately
        assert res == "warning_access_123"
        # Should spawn the background refresh task
        mock_bg.assert_called_once_with(pool, row, "sharepoint", "refresh_token_456")


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_expired_synchronous_refresh() -> None:
    # Completely expired token, must refresh synchronously and hold FOR UPDATE DB lock
    expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    payload = {
        "access_token": "expired_access_123",
        "refresh_token": "my_refresh_token_789",
        "expires_at": expires_at.timestamp(),
    }
    enc_original = encrypt_signing_key(json.dumps(payload).encode("utf-8"), require_master_key())
    row_id = uuid.uuid4()
    row = {
        "id": row_id,
        "provider": "sharepoint",
        "oauth_access_token_enc": enc_original,
    }

    # Mock DB Connection & Transactions
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": row_id, "oauth_access_token_enc": enc_original})
    conn.execute = AsyncMock()

    # Context manager mock for connection transaction
    tx_cm = AsyncMock()
    conn.transaction = MagicMock(return_value=tx_cm)

    pool = AsyncMock()
    # Connection acquisition context manager mock
    pool_acquire_cm = AsyncMock()
    pool_acquire_cm.__aenter__.return_value = conn
    pool.acquire = MagicMock(return_value=pool_acquire_cm)

    refreshed_payload = {
        "access_token": "brand_new_access_token_abc",
        "refresh_token": "my_refresh_token_789",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
    }

    with patch(
        "nce.bridge_renewal._perform_oauth_refresh",
        new_callable=AsyncMock,
        return_value=refreshed_payload,
    ) as mock_refresh:
        res = await ensure_fresh_oauth_token(pool, row, "")
        assert res == "brand_new_access_token_abc"
        mock_refresh.assert_called_once_with("sharepoint", "my_refresh_token_789")

        # Verify SQL row lock
        conn.fetchrow.assert_called_once()
        query_arg = conn.fetchrow.call_args[0][0]
        assert "FOR UPDATE" in query_arg
        assert "bridge_subscriptions" in query_arg

        # Verify DB update of the newly encrypted token JSON via bridge_repo.save_token
        conn.execute.assert_called_once()
        update_args = conn.execute.call_args[0]
        assert "UPDATE bridge_subscriptions" in update_args[0]
        assert "oauth_access_token_enc" in update_args[0]
        assert update_args[1] == row_id
        # Decrypt stored enc payload and verify its content matches expected updated payload
        decrypted = decrypt_signing_key(bytes(update_args[2]), require_master_key()).decode("utf-8")
        decrypted_payload = json.loads(decrypted)
        assert decrypted_payload["access_token"] == "brand_new_access_token_abc"
        assert decrypted_payload["refresh_token"] == "my_refresh_token_789"


@pytest.mark.asyncio
async def test_perform_oauth_refresh_sharepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "AZURE_CLIENT_ID", "sp_cid")
    monkeypatch.setattr(cfg, "AZURE_CLIENT_SECRET", "sp_secret")

    class MockResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.is_success = True
            self.headers: dict[str, str] = {}
            self.text = ""

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "access_token": "new_sp_access",
                "refresh_token": "new_sp_refresh",
                "expires_in": 3600,
            }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=MockResponse())
    mock_client.__aenter__.return_value = mock_client

    with patch("nce.bridge_renewal.httpx.AsyncClient", return_value=mock_client):
        res = await _perform_oauth_refresh("sharepoint", "old_refresh")
        assert res["access_token"] == "new_sp_access"
        assert res["refresh_token"] == "new_sp_refresh"
        assert "expires_at" in res


@pytest.mark.asyncio
async def test_perform_oauth_refresh_gdrive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "GDRIVE_OAUTH_CLIENT_ID", "gd_cid")
    monkeypatch.setattr(cfg, "GDRIVE_OAUTH_CLIENT_SECRET", "gd_secret")

    class MockResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.is_success = True
            self.headers: dict[str, str] = {}
            self.text = ""

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "access_token": "new_gd_access",
                "expires_in": 1800,
            }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=MockResponse())
    mock_client.__aenter__.return_value = mock_client

    with patch("nce.bridge_renewal.httpx.AsyncClient", return_value=mock_client):
        res = await _perform_oauth_refresh("gdrive", "old_refresh")
        assert res["access_token"] == "new_gd_access"
        # Since Google doesn't always return a new refresh token unless prompted, fallback to original refresh
        assert res["refresh_token"] is None
        assert "expires_at" in res


@pytest.mark.asyncio
async def test_perform_oauth_refresh_dropbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "DROPBOX_OAUTH_CLIENT_ID", "db_cid")

    class MockResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.is_success = True
            self.headers: dict[str, str] = {}
            self.text = ""

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "access_token": "new_db_access",
                "expires_in": 7200,
            }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=MockResponse())
    mock_client.__aenter__.return_value = mock_client

    with patch("nce.bridge_renewal.httpx.AsyncClient", return_value=mock_client):
        res = await _perform_oauth_refresh("dropbox", "old_refresh")
        assert res["access_token"] == "new_db_access"
        assert res["refresh_token"] is None
        assert "expires_at" in res


# ---------------------------------------------------------------------------
# Redis mutex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_refresh_lock_success():
    """Lock acquisition returns a Redis client when the key is free."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.close = AsyncMock()

    mock_cls = MagicMock()
    mock_cls.from_url = MagicMock(return_value=mock_redis)
    with patch("nce.bridge_renewal.AsyncRedis", mock_cls):
        client = await _acquire_refresh_lock("sharepoint", "bridge-123")
        assert client is mock_redis
        mock_redis.set.assert_awaited_once()
        key = mock_redis.set.call_args[1].get("key") or mock_redis.set.call_args[0][0]
        assert "bridge_refresh:sharepoint:bridge-123" in str(key)


@pytest.mark.asyncio
async def test_acquire_refresh_lock_already_held():
    """Lock acquisition returns None when the key is already held."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=None)
    mock_redis.close = AsyncMock()

    mock_cls = MagicMock()
    mock_cls.from_url = MagicMock(return_value=mock_redis)
    with patch("nce.bridge_renewal.AsyncRedis", mock_cls):
        client = await _acquire_refresh_lock("gdrive", "bridge-456")
        assert client is None
        mock_redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_refresh_lock_closes_client():
    """Releasing the lock deletes the key and closes the client."""
    mock_redis = AsyncMock()
    mock_redis.delete = AsyncMock()
    mock_redis.close = AsyncMock()

    await _release_refresh_lock(mock_redis, "dropbox", "bridge-789")
    mock_redis.delete.assert_awaited_once()
    mock_redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_refresh_lock_none_client_is_noop():
    """Releasing with None client is a safe no-op."""
    await _release_refresh_lock(None, "sharepoint", "bridge-000")


@pytest.mark.asyncio
async def test_ensure_fresh_oauth_token_rq_worker_warning_window() -> None:
    pool = AsyncMock()
    # 3 minutes in the future (within warning window)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    payload = {
        "access_token": "worker_access_123",
        "refresh_token": "refresh_token_456",
        "expires_at": expires_at.timestamp(),
    }
    enc = encrypt_signing_key(json.dumps(payload).encode("utf-8"), require_master_key())
    row = {"id": uuid.uuid4(), "provider": "sharepoint", "oauth_access_token_enc": enc}

    # Mock get_current_job to return a job (meaning we are inside an RQ worker)
    mock_job = MagicMock()
    with (
        patch("rq.get_current_job", return_value=mock_job),
        patch("nce.bridge_renewal._bg_refresh_token", new_callable=AsyncMock) as mock_bg,
    ):
        res = await ensure_fresh_oauth_token(pool, row, "")
        # Returns current token
        assert res == "worker_access_123"
        # Should NOT spawn background task inside RQ worker context
        mock_bg.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_refresh_lock_redis_connection_error() -> None:
    """If Redis is down, _acquire_refresh_lock catches the exception and returns None."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=Exception("Connection refused"))
    mock_redis.close = AsyncMock()

    mock_cls = MagicMock()
    mock_cls.from_url = MagicMock(return_value=mock_redis)

    with (
        patch("nce.bridge_renewal.AsyncRedis", mock_cls),
        patch("nce.bridge_renewal.cfg.REDIS_URL", "redis://localhost:6379"),
    ):
        client = await _acquire_refresh_lock("sharepoint", "bridge-123")
        assert client is None


@pytest.mark.asyncio
async def test_acquire_refresh_lock_empty_redis_url_non_prod() -> None:
    """If REDIS_URL is empty in non-prod environment, return _DummyRedis."""
    with (
        patch("nce.bridge_renewal.cfg.REDIS_URL", ""),
        patch("nce.bridge_renewal.cfg.IS_PROD", False),
    ):
        client = await _acquire_refresh_lock("sharepoint", "bridge-123")
        from nce.bridge_renewal import _DummyRedis

        assert isinstance(client, _DummyRedis)
