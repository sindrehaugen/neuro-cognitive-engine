"""Migration ledger: apply each SQL file once, and know what the DB is at.

Fix recipe #2, item 3. ``_apply_pg_migrations`` executed every file in
``nce/migrations/`` on every boot and recorded nothing, so:

* all 54 files re-ran each start (pure no-op work, growing with the count);
* nothing could answer "what version is this database at" -- the absence that
  made the 2026-08-27 image-vs-database skew invisible;
* a non-idempotent file was fatal forever rather than once (migration ``041``
  granting on a sequence a pre-BIGSERIAL database does not have).

Asserted here against a fake pool that records every statement, so the tests
pin behaviour -- which files are executed, in what order, and what is recorded
-- rather than wording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nce import migration_ledger
from nce.migration_ledger import migration_checksum, should_skip
from nce.orchestrator import NCEEngine


class _FakeConn:
    """asyncpg-shaped connection recording statements against a fake ledger."""

    def __init__(
        self,
        ledger: dict[str, str],
        *,
        ledger_readable: bool = True,
        ledger_creatable: bool = True,
    ) -> None:
        self.ledger = ledger
        self.ledger_readable = ledger_readable
        self.ledger_creatable = ledger_creatable
        self.executed: list[str] = []
        self.recorded: list[tuple[str, str]] = []

    async def execute(self, sql: str, *args):  # noqa: ANN002, ANN201
        self.executed.append(sql)
        if "CREATE TABLE IF NOT EXISTS applied_migrations" in sql and not self.ledger_creatable:
            raise RuntimeError("permission denied for schema public")
        if "INSERT INTO applied_migrations" in sql:
            filename, checksum = args[0], args[1]
            self.ledger[filename] = checksum
            self.recorded.append((filename, checksum))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args):  # noqa: ANN002, ANN201
        if "FROM applied_migrations" in sql:
            if not self.ledger_readable:
                raise RuntimeError('relation "applied_migrations" does not exist')
            return [{"filename": f, "checksum": c} for f, c in self.ledger.items()]
        return []

    async def fetchval(self, sql: str, *args):  # noqa: ANN002, ANN201
        # Citus is treated as unavailable, matching the local/dev deployment.
        return False

    def transaction(self):  # noqa: ANN201
        return _NullCtx()


class _NullCtx:
    async def __aenter__(self):  # noqa: ANN204
        return None

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class _AcquireCtx:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        self._pool.acquires += 1
        return self._pool.conn

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class _FakePool:
    def __init__(
        self,
        ledger: dict[str, str] | None = None,
        *,
        ledger_readable: bool = True,
        ledger_creatable: bool = True,
    ):
        self.conn = _FakeConn(
            ledger if ledger is not None else {},
            ledger_readable=ledger_readable,
            ledger_creatable=ledger_creatable,
        )
        self.acquires = 0

    def acquire(self, timeout: float | None = None):  # noqa: ANN201
        return _AcquireCtx(self)


def _migration_files() -> list[Path]:
    root = Path(migration_ledger.__file__).resolve().parent / "migrations"
    return sorted(root.glob("*.sql"))


def _applied_bodies(conn: _FakeConn) -> list[str]:
    """Statements that are migration bodies, not ledger/lock bookkeeping."""
    return [
        sql
        for sql in conn.executed
        if "applied_migrations" not in sql and "pg_advisory_xact_lock" not in sql
    ]


async def _run(pool: _FakePool) -> _FakeConn:
    engine = NCEEngine.__new__(NCEEngine)
    engine.pg_pool = pool  # type: ignore[assignment]
    await engine._apply_pg_migrations()
    return pool.conn


class TestChecksum:
    """Content identity must survive a checkout, or nothing is ever skipped."""

    def test_line_endings_do_not_change_the_checksum(self) -> None:
        """The repo is CRLF on Windows and LF in the blob; both are the same file."""
        lf = "CREATE TABLE t (id int);\nALTER TABLE t ADD c int;\n"
        assert migration_checksum(lf) == migration_checksum(lf.replace("\n", "\r\n"))

    def test_trailing_whitespace_does_not_change_the_checksum(self) -> None:
        body = "CREATE TABLE t (id int);"
        assert migration_checksum(body) == migration_checksum(body + "\n\n  ")

    def test_real_content_changes_do_change_the_checksum(self) -> None:
        """Guard the guard: normalisation must not flatten everything to equal."""
        assert migration_checksum("CREATE TABLE a (id int);") != migration_checksum(
            "CREATE TABLE b (id int);"
        )

    def test_should_skip_only_on_an_exact_content_match(self) -> None:
        sql = "CREATE TABLE t (id int);"
        applied = {"001_x.sql": migration_checksum(sql)}
        assert should_skip("001_x.sql", migration_checksum(sql), applied)
        assert not should_skip("001_x.sql", migration_checksum(sql + "-- edit"), applied)
        assert not should_skip("002_y.sql", migration_checksum(sql), applied)


class TestFirstBoot:
    """An empty ledger must still apply everything, exactly as before."""

    @pytest.mark.asyncio
    async def test_every_migration_file_is_applied(self) -> None:
        conn = await _run(_FakePool())
        assert len(_applied_bodies(conn)) == len(_migration_files())

    @pytest.mark.asyncio
    async def test_every_applied_file_is_recorded(self) -> None:
        conn = await _run(_FakePool())
        recorded = {name for name, _ in conn.recorded}
        expected = {p.name for p in _migration_files() if "citus" not in p.name}
        assert recorded == expected

    @pytest.mark.asyncio
    async def test_recorded_checksum_matches_the_file_on_disk(self) -> None:
        conn = await _run(_FakePool())
        by_name = dict(conn.recorded)
        for path in _migration_files():
            if "citus" in path.name:
                continue
            assert by_name[path.name] == migration_checksum(path.read_text(encoding="utf-8")), (
                path.name
            )

    @pytest.mark.asyncio
    async def test_the_ledger_table_is_created_before_it_is_read(self) -> None:
        """Nothing else creates it, so a missing CREATE means a dead ledger."""
        conn = await _run(_FakePool())
        assert any("CREATE TABLE IF NOT EXISTS applied_migrations" in s for s in conn.executed)


class TestSecondBoot:
    """The point of the ledger: a settled database does no migration work."""

    @pytest.mark.asyncio
    async def test_nothing_is_re_applied_when_the_ledger_is_current(self) -> None:
        ledger = {
            p.name: migration_checksum(p.read_text(encoding="utf-8")) for p in _migration_files()
        }
        conn = await _run(_FakePool(ledger))
        assert _applied_bodies(conn) == []

    @pytest.mark.asyncio
    async def test_a_changed_file_is_re_applied(self) -> None:
        """Migrations here get corrected in place; that must not wedge the boot."""
        files = _migration_files()
        ledger = {p.name: migration_checksum(p.read_text(encoding="utf-8")) for p in files}
        ledger[files[0].name] = "stale-checksum"
        conn = await _run(_FakePool(ledger))
        assert len(_applied_bodies(conn)) == 1

    @pytest.mark.asyncio
    async def test_a_changed_file_is_re_recorded_at_its_new_checksum(self) -> None:
        files = _migration_files()
        ledger = {p.name: migration_checksum(p.read_text(encoding="utf-8")) for p in files}
        ledger[files[0].name] = "stale-checksum"
        conn = await _run(_FakePool(ledger))
        assert dict(conn.recorded)[files[0].name] == migration_checksum(
            files[0].read_text(encoding="utf-8")
        )

    @pytest.mark.asyncio
    async def test_a_new_file_is_applied_while_the_rest_stay_skipped(self) -> None:
        files = _migration_files()
        ledger = {p.name: migration_checksum(p.read_text(encoding="utf-8")) for p in files}
        del ledger[files[-1].name]
        conn = await _run(_FakePool(ledger))
        assert len(_applied_bodies(conn)) == 1
        assert [name for name, _ in conn.recorded] == [files[-1].name]


class TestDegradedLedger:
    """A database predating the ledger must boot, not fail."""

    @pytest.mark.asyncio
    async def test_unreadable_ledger_falls_back_to_applying_everything(self) -> None:
        conn = await _run(_FakePool(ledger_readable=False))
        assert len(_applied_bodies(conn)) == len(_migration_files())

    @pytest.mark.asyncio
    async def test_uncreatable_ledger_still_applies_every_migration(self) -> None:
        """A role without DDL rights must boot, not be refused by bookkeeping."""
        conn = await _run(_FakePool(ledger_creatable=False))
        assert len(_applied_bodies(conn)) == len(_migration_files())

    @pytest.mark.asyncio
    async def test_uncreatable_ledger_does_not_attempt_to_record(self) -> None:
        """Recording into a table that does not exist would abort each migration."""
        conn = await _run(_FakePool(ledger_creatable=False))
        assert conn.recorded == []


class TestConcurrencySafety:
    """Five services boot this path at once, each one twice with the pre-flight."""

    @pytest.mark.asyncio
    async def test_ledger_creation_takes_the_advisory_lock(self) -> None:
        """CREATE TABLE IF NOT EXISTS is not safe against itself in Postgres."""
        conn = await _run(_FakePool())
        create_at = next(
            i
            for i, s in enumerate(conn.executed)
            if "CREATE TABLE IF NOT EXISTS applied_migrations" in s
        )
        assert any("pg_advisory_xact_lock" in s for s in conn.executed[:create_at]), (
            "the ledger DDL runs without the advisory lock the migrations use"
        )


class TestCitusFallback:
    """The Citus fallback is not the migration, so it must not be recorded."""

    @pytest.mark.asyncio
    async def test_citus_files_are_not_recorded_as_applied(self) -> None:
        """Recording it would mean the real migration never runs once Citus exists."""
        citus = [p.name for p in _migration_files() if "citus" in p.name]
        if not citus:
            pytest.skip("no citus migration in this checkout")
        conn = await _run(_FakePool())
        recorded = {name for name, _ in conn.recorded}
        for name in citus:
            assert name not in recorded

    @pytest.mark.asyncio
    async def test_citus_files_are_retried_on_every_boot(self) -> None:
        citus = [p.name for p in _migration_files() if "citus" in p.name]
        if not citus:
            pytest.skip("no citus migration in this checkout")
        ledger = {
            p.name: migration_checksum(p.read_text(encoding="utf-8"))
            for p in _migration_files()
            if "citus" not in p.name
        }
        conn = await _run(_FakePool(ledger))
        assert len(_applied_bodies(conn)) == len(citus)
