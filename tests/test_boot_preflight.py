"""Boot pre-flight: exit-code contract and the deploy wiring that uses it.

Fix recipe #2, item 1. ``uvicorn --workers N`` respawns a worker that dies
during startup immediately and forever with no backoff, and the supervisor (PID
1) never exits -- so the container stays ``Up``, Docker's ``restart:`` backoff
never engages, and an environmental startup failure (a stale image against a
migrated database) presents as saturated CPU with no error anywhere. On
2026-08-27 that pinned ``nce-admin`` and ``nce-a2a`` at ~137% CPU each while
``docker ps`` still reported both ``Up``.

The fix is a pre-flight in the entrypoint, before ``exec uvicorn``: run the real
startup path once in a process whose exit code the entrypoint respects, so the
container *exits* on that class of failure.

Two halves are asserted here, because either alone is inert:

  a. :mod:`nce.preflight` maps startup outcomes to exit codes, and never leaks
     secrets into the failure log.
  b. the image actually runs it -- ``ENTRYPOINT`` points at the script, the
     script runs the pre-flight before ``exec "$@"``, and ``set -e`` makes a
     non-zero pre-flight abort the boot.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from nce import preflight

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENTRYPOINT = _REPO_ROOT / "deploy" / "multiuser" / "entrypoint.sh"
_DOCKERFILE = _REPO_ROOT / "deploy" / "multiuser" / "Dockerfile"


def _entrypoint_code() -> str:
    """The entrypoint with comment-only lines stripped.

    The script documents *why* it runs the pre-flight, so a substring search
    over the raw text passes even when the command itself has been deleted.
    Every assertion about what the script *does* runs against this instead.
    """
    lines = _ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    return chr(10).join(ln for ln in lines if not ln.lstrip().startswith("#"))


def _entrypoint_statements() -> list[str]:
    """Executable lines, stripped -- so an ``echo`` mentioning the command cannot
    satisfy an assertion that the command is actually run."""
    return [ln.strip() for ln in _entrypoint_code().splitlines() if ln.strip()]


class _FakeEngine:
    """Stands in for NCEEngine: records the calls the pre-flight must make."""

    def __init__(self, *, connect_error: BaseException | None = None, hang: bool = False):
        self._connect_error = connect_error
        self._hang = hang
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        if self._hang:
            await asyncio.sleep(3600)
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch):
    """Patch the NCEEngine the pre-flight constructs; return an installer."""

    def install(engine: _FakeEngine) -> _FakeEngine:
        import nce

        monkeypatch.setattr(nce, "NCEEngine", lambda: engine)
        return engine

    return install


class TestExitCodeContract:
    """The exit code is the whole interface the entrypoint consumes."""

    @pytest.mark.asyncio
    async def test_successful_startup_exits_zero(self, fake_engine) -> None:
        engine = fake_engine(_FakeEngine())
        assert await preflight.run_preflight() == preflight.EXIT_OK
        assert engine.connected

    @pytest.mark.asyncio
    async def test_startup_failure_exits_nonzero(self, fake_engine) -> None:
        """The RLS-drift shape: connect() raises RuntimeError."""
        fake_engine(
            _FakeEngine(
                connect_error=RuntimeError(
                    "RLS catalog drift: 13 tables not in EXPECTED_TENANT_RLS_TABLES"
                )
            )
        )
        assert await preflight.run_preflight() == preflight.EXIT_STARTUP_FAILED

    def test_exit_codes_are_distinct_and_nonzero_on_failure(self) -> None:
        """Guard the guard: a refactor must not collapse them onto 0."""
        assert preflight.EXIT_OK == 0
        assert preflight.EXIT_STARTUP_FAILED != 0
        assert preflight.EXIT_TIMEOUT != 0
        assert preflight.EXIT_STARTUP_FAILED != preflight.EXIT_TIMEOUT

    @pytest.mark.asyncio
    async def test_hung_startup_times_out_rather_than_blocking_forever(
        self, fake_engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-flight that never returns would recreate the silent-hang shape."""
        monkeypatch.setenv("NCE_PREFLIGHT_TIMEOUT", "0.05")
        fake_engine(_FakeEngine(hang=True))
        assert await preflight.run_preflight() == preflight.EXIT_TIMEOUT

    @pytest.mark.asyncio
    async def test_pools_are_released_even_when_startup_fails(self, fake_engine) -> None:
        """A half-open pool would keep the pre-flight process alive past its exit."""
        engine = fake_engine(_FakeEngine(connect_error=RuntimeError("boom")))
        await preflight.run_preflight()
        assert engine.disconnected

    @pytest.mark.asyncio
    async def test_pools_are_released_on_success(self, fake_engine) -> None:
        engine = fake_engine(_FakeEngine())
        await preflight.run_preflight()
        assert engine.disconnected

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_mask_the_startup_verdict(self, fake_engine) -> None:
        """disconnect() blowing up must not turn a clean boot into a failure."""

        class _BadCleanup(_FakeEngine):
            async def disconnect(self) -> None:
                raise RuntimeError("pool already closed")

        fake_engine(_BadCleanup())
        assert await preflight.run_preflight() == preflight.EXIT_OK


class TestFailureLogging:
    """The operator reads the log; it must name the problem and hide secrets."""

    @pytest.mark.asyncio
    async def test_failure_is_logged_critical(
        self, fake_engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_engine(_FakeEngine(connect_error=RuntimeError("schema drift")))
        with caplog.at_level(logging.CRITICAL, logger="nce-preflight"):
            await preflight.run_preflight()
        assert any(r.levelno >= logging.CRITICAL for r in caplog.records)
        assert "schema drift" in caplog.text

    @pytest.mark.asyncio
    async def test_secrets_in_the_exception_are_redacted(
        self, fake_engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """connect() failures routinely carry the DSN, password included."""
        secret = "hunter2_super_secret_password"
        fake_engine(
            _FakeEngine(
                connect_error=RuntimeError(
                    f"could not connect: postgresql://mcp_user:{secret}@postgres:5432/memory_meta"
                )
            )
        )
        with caplog.at_level(logging.CRITICAL, logger="nce-preflight"):
            await preflight.run_preflight()
        assert secret not in caplog.text


class TestTimeoutConfiguration:
    """The timeout must be overridable and must never be disabled by a typo."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NCE_PREFLIGHT_TIMEOUT", raising=False)
        assert preflight._timeout_seconds() == preflight.DEFAULT_TIMEOUT_SECONDS

    def test_explicit_value_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NCE_PREFLIGHT_TIMEOUT", "12.5")
        assert preflight._timeout_seconds() == 12.5

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "0", "-5"])
    def test_unusable_values_fall_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """A malformed or non-positive timeout must not mean no timeout at all."""
        monkeypatch.setenv("NCE_PREFLIGHT_TIMEOUT", bad)
        assert preflight._timeout_seconds() == preflight.DEFAULT_TIMEOUT_SECONDS


class TestEntrypointScript:
    """The pre-flight is inert unless the container actually runs it."""

    def test_entrypoint_script_exists(self) -> None:
        assert _ENTRYPOINT.is_file()

    def test_entrypoint_has_a_posix_shebang(self) -> None:
        assert _ENTRYPOINT.read_bytes().startswith(b"#!/bin/sh")

    def test_entrypoint_is_lf_only(self) -> None:
        """A CRLF shell script fails to exec inside the Linux image.

        This reads the working tree on purpose: the working tree *is* the Docker
        build context. A checkout that predates the .gitattributes pin keeps its
        CRLF copy even after pulling the pin, because the blob never changed -- so
        if this fails, renormalise rather than relaxing the assertion:

            rm deploy/multiuser/entrypoint.sh
            git checkout -- deploy/multiuser/entrypoint.sh
        """
        assert bytes([13, 10]) not in _ENTRYPOINT.read_bytes(), (
            "entrypoint.sh is CRLF in the working tree; see this test's docstring"
        )

    def test_entrypoint_aborts_on_a_failed_preflight(self) -> None:
        """Without set -e a non-zero pre-flight would fall through to exec."""
        assert "set -e" in _entrypoint_code()

    def test_entrypoint_runs_the_preflight_before_exec(self) -> None:
        """The command must be its own statement -- not a comment, not an echo."""
        lines = _entrypoint_statements()
        assert "python -m nce.preflight" in lines, (
            "the pre-flight is mentioned but never run as a command: " + repr(lines)
        )
        assert 'exec "$@"' in lines
        assert lines.index("python -m nce.preflight") < lines.index('exec "$@"')

    def test_entrypoint_offers_an_escape_hatch(self) -> None:
        assert "NCE_PREFLIGHT" in _entrypoint_code()


class TestLineEndingPin:
    """The entrypoint must still be LF after a checkout, not just as authored.

        The repository is developed on Windows with ``core.autocrlf=true`` and the
        working tree *is* the Docker build context, so without a ``.gitattributes``
        pin a fresh checkout rewrites the script to CRLF and the image fails to exec
        it (``#!/bin/sh
    ``). Every other tracked ``.sh`` in this repo is CRLF on
        disk today for exactly that reason.
    """

    def test_gitattributes_exists(self) -> None:
        assert (_REPO_ROOT / ".gitattributes").is_file()

    def test_shell_scripts_are_pinned_to_lf(self) -> None:
        text = (_REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        pins = [ln.strip() for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert "*.sh text eol=lf" in pins, pins

    def test_dockerfiles_are_pinned_to_lf(self) -> None:
        """CRLF also breaks multi-line RUN continuations."""
        text = (_REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        pins = [ln.strip() for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert "Dockerfile text eol=lf" in pins, pins


class TestDockerfileWiring:
    """ENTRYPOINT must point at the script, and the script must be in the image."""

    def test_dockerfile_declares_the_entrypoint(self) -> None:
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in text

    def test_dockerfile_copies_the_entrypoint_script(self) -> None:
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert "COPY deploy/multiuser/entrypoint.sh ./entrypoint.sh" in text

    def test_dockerfile_makes_the_entrypoint_executable(self) -> None:
        """The repo is checked out on Windows, where the exec bit does not survive."""
        assert "chmod 0755 /app/entrypoint.sh" in _DOCKERFILE.read_text(encoding="utf-8")

    def test_dockerfile_still_declares_a_default_cmd(self) -> None:
        """ENTRYPOINT plus CMD: exec "$@" needs arguments when none are supplied."""
        assert 'CMD ["python", "start_worker.py"]' in _DOCKERFILE.read_text(encoding="utf-8")

    def test_entrypoint_is_declared_after_the_copy(self) -> None:
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert text.index("COPY deploy/multiuser/entrypoint.sh") < text.index("ENTRYPOINT [")
