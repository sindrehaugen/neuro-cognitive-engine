"""Tests for scripts/migrate_bridge_tokens.py migration helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "migrate_bridge_tokens",
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_bridge_tokens.py",
)
_migrate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_migrate)


def test_is_valid_encrypted_blob_recognizes_tc_prefixes() -> None:
    assert _migrate._is_valid_encrypted_blob(b"TC4\x01payload")
    assert not _migrate._is_valid_encrypted_blob(b"plain-token")


def test_coerce_plaintext_to_access_token_dict() -> None:
    payload = _migrate._coerce_to_json_dict(b"raw-oauth-token")
    assert payload == {"access_token": "raw-oauth-token"}


def test_coerce_json_dict_passthrough() -> None:
    raw = json.dumps({"access_token": "t", "refresh_token": "r"}).encode()
    assert _migrate._coerce_to_json_dict(raw)["access_token"] == "t"


@pytest.mark.asyncio
async def test_process_row_skips_null_blob() -> None:
    conn = AsyncMock()
    row = {"id": uuid4(), "oauth_access_token_enc": None}
    updated, reason = await _migrate._process_row(conn, row)
    assert updated is False
    assert reason == "null"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_row_migrates_plaintext_blob() -> None:
    conn = AsyncMock()
    row = {"id": uuid4(), "oauth_access_token_enc": b"legacy-plain-token"}
    with patch.object(_migrate, "encrypt_signing_key", return_value=b"TC4\x01enc"):
        with patch.object(_migrate, "require_master_key") as mk_ctx:
            mk_ctx.return_value.__enter__ = MagicMock(return_value=b"0" * 32)
            mk_ctx.return_value.__exit__ = MagicMock(return_value=False)
            updated, reason = await _migrate._process_row(conn, row)
    assert updated is True
    assert reason == "migrated"
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_uses_transaction_per_row() -> None:
    row_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"id": row_id, "oauth_access_token_enc": b"legacy-plain-token"}]
    )
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        with patch.object(_migrate, "_process_row", AsyncMock(return_value=(True, "migrated"))):
            code = await _migrate._main()
    assert code == 0
    conn.transaction.assert_called_once()
