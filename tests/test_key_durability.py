"""Master-key durability: the boot check, and a re-key that cannot half-complete.

These tests reproduce the 2026-09-02 incident rather than asserting a mock: a
signing key is wrapped with master key A and then verified with master key B,
which is exactly "rotated without updating the deployment".

Everything runs against a **scratch** database (``rekey_probe``), created and
dropped by the fixture below.  The re-key script is never pointed at the shared
dev database: re-wrapping its live signing key is not reversible without the old
key, and the point of this wave is to stop exactly that.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from nce import orchestrator as orch_mod
from nce.config import cfg
from nce.envelope import unwrap_dek, wrap_dek
from nce.orchestrator import NCEEngine
from nce.signing import (
    MasterKey,
    SigningKeyDecryptionError,
    decrypt_signing_key,
    encrypt_signing_key,
    master_key_fingerprint,
)

KEY_A = "A" * 40
KEY_B = "B" * 40
SCRATCH_DB = "rekey_probe"

_REPO = Path(__file__).resolve().parents[1]

# The scratch schema carries only the columns the re-key touches.  It is
# deliberately NOT nce/schema.sql: that file is held by another wave, and a
# minimal table proves the script's behaviour without the live database.
_SCRATCH_DDL = """
CREATE TABLE signing_keys (
    key_id        text PRIMARY KEY,
    encrypted_key bytea       NOT NULL,
    status        text        NOT NULL DEFAULT 'active',
    created_at    timestamptz NOT NULL DEFAULT now(),
    retired_at    timestamptz
);
CREATE TABLE memories (
    id          uuid PRIMARY KEY,
    wrapped_dek bytea,
    dek_key_id  text
);
"""


def _load_rekey_module():
    """Import ``scripts/rekey_master.py`` as a module (``scripts/`` is not a package)."""
    path = _REPO / "scripts" / "rekey_master.py"
    spec = importlib.util.spec_from_file_location("rekey_master_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rekey_master = _load_rekey_module()


# ---------------------------------------------------------------------------
# Scratch database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch_dsn() -> str:
    base = os.environ.get("PG_DSN") or getattr(cfg, "PG_DSN", "")
    if not base or "/" not in base:
        pytest.skip("PG_DSN not set; scratch database unavailable")
    prefix = base.rsplit("/", 1)[0]
    admin_dsn = f"{prefix}/postgres"
    target_dsn = f"{prefix}/{SCRATCH_DB}"

    async def _create() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
            await conn.execute(f"CREATE DATABASE {SCRATCH_DB}")
        finally:
            await conn.close()
        conn = await asyncpg.connect(target_dsn)
        try:
            await conn.execute(_SCRATCH_DDL)
        finally:
            await conn.close()

    try:
        asyncio.run(_create())
    except Exception as exc:  # pragma: no cover - no cluster / no privileges
        pytest.skip(f"scratch database {SCRATCH_DB} unavailable: {exc}")

    yield target_dsn

    async def _drop() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        finally:
            await conn.close()

    try:
        asyncio.run(_drop())
    except Exception:  # pragma: no cover
        pass


async def _seed(dsn: str, *, key: str, signing: int = 1, deks: int = 1, nulls: int = 1) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE signing_keys, memories")
        with MasterKey(key.encode()) as mk:
            for i in range(signing):
                await conn.execute(
                    "INSERT INTO signing_keys (key_id, encrypted_key, status) VALUES ($1, $2, $3)",
                    f"sk-test{i:02d}",
                    encrypt_signing_key(os.urandom(32), mk),
                    "active" if i == 0 else "retired",
                )
            for _ in range(deks):
                await conn.execute(
                    "INSERT INTO memories (id, wrapped_dek) VALUES ($1, $2)",
                    uuid.uuid4(),
                    wrap_dek(os.urandom(32), mk),
                )
            for _ in range(nulls):
                await conn.execute(
                    "INSERT INTO memories (id, wrapped_dek) VALUES ($1, NULL)", uuid.uuid4()
                )
    finally:
        await conn.close()


async def _snapshot(dsn: str) -> tuple[dict, dict]:
    conn = await asyncpg.connect(dsn)
    try:
        keys = {
            r["key_id"]: bytes(r["encrypted_key"])
            for r in await conn.fetch("SELECT key_id, encrypted_key FROM signing_keys")
        }
        mems = {
            str(r["id"]): (None if r["wrapped_dek"] is None else bytes(r["wrapped_dek"]))
            for r in await conn.fetch("SELECT id, wrapped_dek FROM memories")
        }
    finally:
        await conn.close()
    return keys, mems


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE signing_keys, memories")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Boot check
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Just enough of NCEEngine for the boot check: a pool."""

    def __init__(self, pool) -> None:
        self.pg_pool = pool


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self, level: int | None = None) -> str:
        return "\n".join(
            r.getMessage() for r in self.records if level is None or r.levelno == level
        )


def _boot_check(dsn: str) -> tuple[_Capture, BaseException | None]:
    cap = _Capture()
    orch_mod.log.addHandler(cap)
    previous = orch_mod.log.level
    orch_mod.log.setLevel(logging.DEBUG)

    async def _run() -> None:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            await NCEEngine._verify_master_key_matches_data(_FakeEngine(pool))
        finally:
            await pool.close()

    raised: BaseException | None = None
    try:
        asyncio.run(_run())
    except BaseException as exc:  # noqa: BLE001 - the test inspects it
        raised = exc
    finally:
        orch_mod.log.removeHandler(cap)
        orch_mod.log.setLevel(previous)
    return cap, raised


def test_boot_check_warns_on_a_fresh_deployment(scratch_dsn, monkeypatch):
    """No active signing key is a legitimately fresh deploy: WARN, never refuse."""
    monkeypatch.setenv("NCE_MASTER_KEY", KEY_A)
    monkeypatch.delenv("NCE_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("NCE_ALLOW_MASTER_KEY_MISMATCH", raising=False)
    asyncio.run(_truncate(scratch_dsn))

    cap, raised = _boot_check(scratch_dsn)

    assert raised is None, f"a fresh deployment must not be refused: {raised!r}"
    assert "fresh deployment" in cap.text(logging.WARNING)
    assert cap.text(logging.CRITICAL) == ""
    assert KEY_A not in cap.text()


def test_boot_check_criticals_on_a_key_wrapped_under_a_different_master_key(
    scratch_dsn, monkeypatch
):
    """The September incident: wrapped with A, deployment holds B."""
    monkeypatch.delenv("NCE_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("NCE_ALLOW_MASTER_KEY_MISMATCH", raising=False)
    asyncio.run(_seed(scratch_dsn, key=KEY_A, signing=1, deks=0, nulls=0))
    monkeypatch.setenv("NCE_MASTER_KEY", KEY_B)

    cap, raised = _boot_check(scratch_dsn)

    critical = cap.text(logging.CRITICAL)
    assert raised is None, "advisory outside production, like _verify_schema_version"
    assert "master-key mismatch" in critical
    assert "rotated without updating this deployment" in critical
    assert "sk-test00" in critical
    with MasterKey(KEY_B.encode()) as mk:
        assert master_key_fingerprint(mk) in critical
    assert KEY_A not in cap.text() and KEY_B not in cap.text()


def test_boot_check_refuses_in_production_and_honours_the_ack(scratch_dsn, monkeypatch):
    """Production raises; NCE_ALLOW_MASTER_KEY_MISMATCH acknowledges it."""
    monkeypatch.delenv("NCE_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("NCE_ALLOW_MASTER_KEY_MISMATCH", raising=False)
    asyncio.run(_seed(scratch_dsn, key=KEY_A, signing=1, deks=0, nulls=0))
    monkeypatch.setenv("NCE_MASTER_KEY", KEY_B)
    monkeypatch.setattr(cfg, "IS_PROD", True, raising=False)

    _cap, raised = _boot_check(scratch_dsn)
    assert isinstance(raised, RuntimeError)
    assert str(raised).startswith("FATAL: master-key mismatch")

    monkeypatch.setenv("NCE_ALLOW_MASTER_KEY_MISMATCH", "1")
    cap2, raised2 = _boot_check(scratch_dsn)
    assert raised2 is None
    assert cap2.text(logging.CRITICAL).endswith("(acknowledged)")


def test_boot_check_logs_the_fingerprint_and_passes_on_the_right_key(scratch_dsn, monkeypatch):
    monkeypatch.delenv("NCE_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("NCE_ALLOW_MASTER_KEY_MISMATCH", raising=False)
    asyncio.run(_seed(scratch_dsn, key=KEY_A, signing=1, deks=0, nulls=0))
    monkeypatch.setenv("NCE_MASTER_KEY", KEY_A)

    cap, raised = _boot_check(scratch_dsn)

    assert raised is None
    assert cap.text(logging.CRITICAL) == ""
    with MasterKey(KEY_A.encode()) as mk:
        fingerprint = master_key_fingerprint(mk)
    info = cap.text(logging.INFO)
    assert f"fingerprint={fingerprint}" in info
    assert "unwraps the active signing key" in info
    assert KEY_A not in cap.text()


def test_connect_invokes_the_master_key_check():
    """Guard-the-guard: a verification nobody calls is not a verification.

    ``register_automation_subscribers()`` was written, tested, and never wired
    in.  This asserts the call site itself, so removing the line from
    ``connect()`` turns this test RED.
    """
    source = inspect.getsource(NCEEngine.connect)
    assert "await self._verify_master_key_matches_data()" in source, (
        "connect() no longer calls _verify_master_key_matches_data(); the boot "
        "check exists but nothing invokes it"
    )


# ---------------------------------------------------------------------------
# rekey_master
# ---------------------------------------------------------------------------


def test_rekey_happy_path_new_key_opens_everything_old_key_opens_nothing(scratch_dsn):
    asyncio.run(_seed(scratch_dsn, key=KEY_A, signing=2, deks=1, nulls=1))

    async def _run():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            with MasterKey(KEY_A.encode()) as old, MasterKey(KEY_B.encode()) as new:
                return await rekey_master.rekey_all(conn, old, new)
        finally:
            await conn.close()

    stats = asyncio.run(_run())
    assert stats["signing_keys_rewrapped"] == 2
    assert stats["signing_keys_verified"] == 2
    assert stats["deks_rewrapped"] == 1
    assert stats["deks_verified"] == 1
    assert stats["deks_null_skipped"] == 1, "NULL wrapped_dek is a skip, not an error"
    assert stats["committed"] == 1

    keys, mems = asyncio.run(_snapshot(scratch_dsn))
    with MasterKey(KEY_B.encode()) as new:
        for blob in keys.values():
            assert len(decrypt_signing_key(blob, new)) == 32
        for blob in mems.values():
            if blob is not None:
                assert len(unwrap_dek(blob, new)) == 32
    with MasterKey(KEY_A.encode()) as old:
        for blob in keys.values():
            with pytest.raises(SigningKeyDecryptionError):
                decrypt_signing_key(blob, old)


def test_rekey_with_a_wrong_old_key_aborts_before_any_write(scratch_dsn):
    """The assertion that protects real data: no write happens at all."""
    asyncio.run(_seed(scratch_dsn, key=KEY_A, signing=2, deks=1, nulls=1))
    before = asyncio.run(_snapshot(scratch_dsn))

    wrong_old = "C" * 40

    async def _run():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            with MasterKey(wrong_old.encode()) as old, MasterKey(KEY_B.encode()) as new:
                return await rekey_master.rekey_all(conn, old, new)
        finally:
            await conn.close()

    with pytest.raises(rekey_master.RekeyAborted) as excinfo:
        asyncio.run(_run())
    assert "ABORT BEFORE ANY WRITE" in str(excinfo.value)

    after = asyncio.run(_snapshot(scratch_dsn))
    assert after == before, "rows must be byte-identical after an abort"
    with MasterKey(KEY_A.encode()) as old:
        for blob in after[0].values():
            assert len(decrypt_signing_key(blob, old)) == 32


def test_rekey_rolls_back_when_step4_verification_fails(scratch_dsn, monkeypatch):
    """Inject a step-4 failure on the last row; every row must be unchanged."""
    asyncio.run(_seed(scratch_dsn, key=KEY_A, signing=2, deks=1, nulls=1))
    before = asyncio.run(_snapshot(scratch_dsn))

    def _boom(blob, new_master_key, memory_id):
        raise rekey_master.RekeyAborted(f"injected verification failure for memory {memory_id}")

    monkeypatch.setattr(rekey_master, "_verify_wrapped_dek", _boom)

    async def _run():
        conn = await asyncpg.connect(scratch_dsn)
        try:
            with MasterKey(KEY_A.encode()) as old, MasterKey(KEY_B.encode()) as new:
                return await rekey_master.rekey_all(conn, old, new)
        finally:
            await conn.close()

    with pytest.raises(rekey_master.RekeyAborted) as excinfo:
        asyncio.run(_run())
    assert "injected verification failure" in str(excinfo.value)

    after = asyncio.run(_snapshot(scratch_dsn))
    assert after == before, "a failed verification must roll every row back"
    with MasterKey(KEY_A.encode()) as old:
        for blob in after[0].values():
            assert len(decrypt_signing_key(blob, old)) == 32
        for blob in after[1].values():
            if blob is not None:
                assert len(unwrap_dek(blob, old)) == 32
