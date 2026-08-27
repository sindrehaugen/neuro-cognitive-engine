"""Version skew must be named, not inferred from an allowlist dump.

Fix recipe #2, item 4. On 2026-08-27 two containers crash-looped because their
image was built one day behind the checkout that had migrated the live database.
Its ``EXPECTED_TENANT_RLS_TABLES`` predated 13 tables that now existed, so the
RLS catalog check failed closed -- printing 13 lines telling the operator to add
tables to an allowlist. That reads like a code bug. The actual problem was a
stale deploy, and nothing said so.

``NCEEngine._verify_schema_version`` compares the migrations *in the image*
against the migrations *recorded in the database* and reports the difference in
those terms. It runs before the enforcement checks, so the named diagnosis
appears above the symptom.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from pathlib import Path

import pytest
import yaml

import nce.orchestrator as orchestrator_module
from nce import build_info
from nce.migration_ledger import highest_version, migration_version, missing_from_image
from nce.orchestrator import NCEEngine

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "deploy" / "multiuser" / "Dockerfile"
_COMPOSE_FILES = (
    _REPO_ROOT / "docker-compose.yml",
    _REPO_ROOT / "deploy" / "multiuser" / "docker-compose.yml",
)


class _FakeConn:
    def __init__(self, recorded: list[str]) -> None:
        self._recorded = recorded

    async def fetch(self, sql: str, *args):  # noqa: ANN002, ANN201
        return [{"filename": f, "checksum": "x"} for f in self._recorded]


class _AcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class _FakePool:
    def __init__(self, recorded: list[str]) -> None:
        self.conn = _FakeConn(recorded)

    def acquire(self, timeout: float | None = None):  # noqa: ANN201
        return _AcquireCtx(self.conn)


def _image_migrations() -> list[str]:
    root = Path(orchestrator_module.__file__).resolve().parent / "migrations"
    return sorted(p.name for p in root.glob("*.sql"))


async def _check(recorded: list[str]) -> None:
    engine = NCEEngine.__new__(NCEEngine)
    engine.pg_pool = _FakePool(recorded)  # type: ignore[assignment]
    await engine._verify_schema_version()


class TestVersionArithmetic:
    def test_migration_version_reads_the_numeric_prefix(self) -> None:
        assert migration_version("054_assets.sql") == 54
        assert migration_version("007_x.sql") == 7

    def test_unnumbered_filenames_have_no_version(self) -> None:
        assert migration_version("fixup.sql") is None

    def test_highest_version_ignores_unnumbered_files(self) -> None:
        assert highest_version(["001_a.sql", "fixup.sql", "042_b.sql"]) == 42

    def test_highest_version_of_nothing_is_none(self) -> None:
        assert highest_version([]) is None
        assert highest_version(["fixup.sql"]) is None

    def test_missing_from_image_finds_only_the_breaking_direction(self) -> None:
        """An image ahead of the DB applies its own migrations; only behind breaks."""
        image = ["001_a.sql", "002_b.sql", "003_c.sql"]
        db = ["001_a.sql", "002_b.sql"]
        assert missing_from_image(image, db) == []
        assert missing_from_image(db, image) == ["003_c.sql"]

    def test_missing_from_image_is_sorted_and_deduplicated_by_name(self) -> None:
        assert missing_from_image(["001_a.sql"], ["003_c.sql", "002_b.sql"]) == [
            "002_b.sql",
            "003_c.sql",
        ]


class TestSkewDetection:
    @pytest.mark.asyncio
    async def test_a_matching_database_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="nce-orchestrator"):
            await _check(_image_migrations())
        assert "schema skew" not in caplog.text

    @pytest.mark.asyncio
    async def test_an_empty_ledger_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """A database predating the ledger has nothing to compare yet."""
        with caplog.at_level(logging.WARNING, logger="nce-orchestrator"):
            await _check([])
        assert "schema skew" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_database_ahead_of_the_image_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])
        assert "schema skew" in caplog.text

    @pytest.mark.asyncio
    async def test_an_image_ahead_of_the_database_is_not_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Startup applies those itself, so it is not skew."""
        with caplog.at_level(logging.WARNING, logger="nce-orchestrator"):
            await _check(_image_migrations()[:-3])
        assert "schema skew" not in caplog.text


class TestActionableMessage:
    """The message has to be readable by whoever is paged, not just parseable."""

    @pytest.mark.asyncio
    async def test_it_names_the_missing_migrations(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])
        assert "099_from_a_newer_image.sql" in caplog.text

    @pytest.mark.asyncio
    async def test_it_reports_both_versions(self, caplog: pytest.LogCaptureFixture) -> None:
        image_at = highest_version(_image_migrations())
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])
        assert f"up to {image_at}" in caplog.text
        assert "is at 99" in caplog.text

    @pytest.mark.asyncio
    async def test_it_says_what_to_do(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])
        assert "Rebuild" in caplog.text

    @pytest.mark.asyncio
    async def test_it_says_the_rls_failure_is_a_symptom(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole point: stop the allowlist dump reading like the root cause."""
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])
        assert "symptom" in caplog.text

    @pytest.mark.asyncio
    async def test_a_long_list_is_truncated_rather_than_dumped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        extra = [f"{n:03d}_newer.sql" for n in range(90, 108)]
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), *extra])
        assert f"missing {len(extra)} migration(s)" in caplog.text
        assert "..." in caplog.text


class TestProductionFailsClosed:
    """Advisory in dev (branch images share a database); fatal in production."""

    @pytest.mark.asyncio
    async def test_production_refuses_to_boot_on_skew(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(orchestrator_module.cfg, "IS_PROD", True)
        monkeypatch.delenv("NCE_ALLOW_SCHEMA_SKEW", raising=False)
        with pytest.raises(RuntimeError, match="schema skew"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])

    @pytest.mark.asyncio
    async def test_production_boots_when_the_skew_is_acknowledged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(orchestrator_module.cfg, "IS_PROD", True)
        monkeypatch.setenv("NCE_ALLOW_SCHEMA_SKEW", "true")
        await _check([*_image_migrations(), "099_from_a_newer_image.sql"])

    @pytest.mark.asyncio
    async def test_production_does_not_refuse_a_matching_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard the guard: the prod path must not raise on every boot."""
        monkeypatch.setattr(orchestrator_module.cfg, "IS_PROD", True)
        monkeypatch.delenv("NCE_ALLOW_SCHEMA_SKEW", raising=False)
        await _check(_image_migrations())

    @pytest.mark.asyncio
    async def test_dev_only_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(orchestrator_module.cfg, "IS_PROD", False)
        await _check([*_image_migrations(), "099_from_a_newer_image.sql"])


class TestCheckOrder:
    """The diagnosis is worthless below the symptom it explains."""

    def test_schema_version_check_runs_before_the_rls_check(self) -> None:
        source = textwrap.dedent(inspect.getsource(NCEEngine.connect))
        (func,) = [n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)]
        order = [
            n.value.func.attr
            for n in ast.walk(func)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and isinstance(n.value.func.value, ast.Name)
            and n.value.func.value.id == "self"
        ]
        assert "_verify_schema_version" in order, order
        assert order.index("_verify_schema_version") < order.index("_verify_rls_enforcement")

    def test_schema_version_check_runs_after_migrations_are_applied(self) -> None:
        """It reads the ledger, which this boot may have just populated."""
        source = textwrap.dedent(inspect.getsource(NCEEngine.connect))
        (func,) = [n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)]
        order = [
            n.value.func.attr
            for n in ast.walk(func)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and isinstance(n.value.func.value, ast.Name)
            and n.value.func.value.id == "self"
        ]
        assert order.index("_apply_pg_migrations") < order.index("_verify_schema_version")


class TestBuildStamp:
    """Advisory, so it must degrade to a readable string rather than blow up."""

    def test_unset_stamp_reads_as_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NCE_GIT_SHA", raising=False)
        monkeypatch.delenv("NCE_BUILD_TIME", raising=False)
        assert build_info.git_sha() == build_info.UNKNOWN
        assert build_info.build_time() == build_info.UNKNOWN
        assert "unavailable" in build_info.describe()

    def test_blank_stamp_reads_as_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset docker build-arg becomes an empty string, not an absent var."""
        monkeypatch.setenv("NCE_GIT_SHA", "  ")
        assert build_info.git_sha() == build_info.UNKNOWN

    def test_set_stamp_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NCE_GIT_SHA", "4036f5b")
        monkeypatch.setenv("NCE_BUILD_TIME", "2026-08-17T13:51:00Z")
        assert "4036f5b" in build_info.describe()
        assert "2026-08-17T13:51:00Z" in build_info.describe()

    @pytest.mark.asyncio
    async def test_the_skew_message_carries_the_stamp(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("NCE_GIT_SHA", "deadbee")
        with caplog.at_level(logging.CRITICAL, logger="nce-orchestrator"):
            await _check([*_image_migrations(), "099_from_a_newer_image.sql"])
        assert "deadbee" in caplog.text


class TestImageStampWiring:
    """The stamp is only ever set if the build passes it in."""

    def test_dockerfile_accepts_the_build_args(self) -> None:
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "ARG NCE_GIT_SHA" in text
        assert "ARG NCE_BUILD_TIME" in text

    def test_dockerfile_promotes_them_to_env(self) -> None:
        """ARG alone does not survive into the running container."""
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "ENV NCE_GIT_SHA=${NCE_GIT_SHA}" in text
        assert "ENV NCE_BUILD_TIME=${NCE_BUILD_TIME}" in text

    @pytest.mark.parametrize("compose", _COMPOSE_FILES, ids=lambda p: p.parent.name)
    def test_every_service_built_from_the_app_image_passes_the_args(self, compose: Path) -> None:
        doc = yaml.safe_load(compose.read_text(encoding="utf-8"))
        built = {
            name: svc
            for name, svc in doc["services"].items()
            if isinstance(svc.get("build"), dict)
            and svc["build"].get("dockerfile") == "deploy/multiuser/Dockerfile"
        }
        assert built, f"no app-image services found in {compose}"
        for name, svc in built.items():
            args = svc["build"].get("args") or {}
            assert "NCE_GIT_SHA" in args, f"{name} does not pass NCE_GIT_SHA"
            assert "NCE_BUILD_TIME" in args, f"{name} does not pass NCE_BUILD_TIME"
