"""Deploy-time health gate: act on ``unhealthy`` instead of ignoring it.

Fix recipe #2, item 5. Plain Docker Compose does nothing with a healthcheck
verdict. On 2026-08-27 ``nce-admin`` and ``nce-a2a`` went ``unhealthy`` and
stayed there, crash-looping inside their own uvicorn supervisors, and because
the supervisor process never exited ``docker ps`` reported both ``Up``. No
alert, no restart, no non-zero exit anywhere: it read as ambient CPU load.

Two entry points over the same verdict:

``python -m nce.deploy_health``
    Poll until every container is healthy, or fail non-zero on timeout. Run it
    after ``compose up -d`` so a deploy that never comes up fails the deploy.

``python -m nce.deploy_health --once``
    One-shot verdict for a watchdog or scheduled alert -- non-zero while
    anything is unhealthy, restarting, or has exited.

The verdict is a pure function of parsed ``docker inspect`` output so it is
tested without Docker; only :func:`_run` touches a subprocess.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("nce-deploy-health")

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_TIMEOUT = 2
EXIT_NO_CONTAINERS = 3

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class ContainerState:
    """The fields of ``docker inspect`` this gate actually judges on."""

    name: str
    status: str = ""
    health: str = ""
    restart_count: int = 0
    exit_code: int = 0
    restarting: bool = False

    @property
    def has_healthcheck(self) -> bool:
        return bool(self.health)

    def describe(self) -> str:
        parts = [f"status={self.status or '?'}"]
        if self.health:
            parts.append(f"health={self.health}")
        parts.append(f"restarts={self.restart_count}")
        if self.status == "exited":
            parts.append(f"exit={self.exit_code}")
        return f"{self.name} ({', '.join(parts)})"


@dataclass(frozen=True)
class Verdict:
    """``settled`` means stop polling; ``failures`` non-empty means fail."""

    failures: list[str]
    pending: list[str]

    @property
    def settled(self) -> bool:
        return bool(self.failures) or not self.pending

    @property
    def ok(self) -> bool:
        return not self.failures and not self.pending


def parse_inspect(payload: str) -> list[ContainerState]:
    """Parse ``docker inspect`` JSON into the states this gate judges.

    Tolerates an empty payload and unknown keys; a container missing
    ``State.Health`` simply has no healthcheck.
    """
    text = payload.strip()
    if not text:
        return []
    raw = json.loads(text)
    if isinstance(raw, dict):
        raw = [raw]

    states: list[ContainerState] = []
    for entry in raw:
        state = entry.get("State") or {}
        health = (state.get("Health") or {}).get("Status") or ""
        states.append(
            ContainerState(
                name=str(entry.get("Name") or "").lstrip("/"),
                status=str(state.get("Status") or ""),
                health=str(health),
                restart_count=int(entry.get("RestartCount") or 0),
                exit_code=int(state.get("ExitCode") or 0),
                restarting=bool(state.get("Restarting") or False),
            )
        )
    return states


def evaluate(
    states: list[ContainerState],
    *,
    baseline_restarts: dict[str, int] | None = None,
    fail_on_exited: bool = True,
) -> Verdict:
    """Judge a set of container states.

    Failures (stop, non-zero):
      * ``restarting``, or a restart count above the baseline -- the crash-loop
        signal that the entrypoint pre-flight converts a silent CPU burn into;
      * ``unhealthy`` while running -- the verdict Compose throws away;
      * ``exited`` or ``dead``, when ``fail_on_exited``.

    Pending (keep polling):
      * ``starting`` health, or any not-yet-``running`` status.

    A container with no healthcheck only has to be ``running``: judging it on a
    healthcheck it does not declare would fail every deploy.

    ``fail_on_exited`` separates the two callers. Straight after ``compose up``
    nothing should be exited, so the deploy gate fails on it. A watchdog polling
    a long-lived stack must not, because ``docker stop`` is an operator decision
    and leaves exactly the same status.

    A stopped container also retains its *last* health status -- the two
    containers stopped during the 2026-08-27 remediation still inspect as
    ``unhealthy`` -- so health is only judged on a container that is running.
    Otherwise a deliberate stop is reported twice, once under the wrong reason.
    """
    baseline = baseline_restarts or {}
    failures: list[str] = []
    pending: list[str] = []

    for state in states:
        if state.status in {"exited", "dead"}:
            if fail_on_exited:
                failures.append(f"{state.describe()} — container is not running")
            continue
        before = baseline.get(state.name)
        if before is not None and state.restart_count > before:
            failures.append(
                f"{state.describe()} — restarted {state.restart_count - before} time(s) "
                "during the wait; this is a crash loop, not a slow start"
            )
            continue
        if state.restarting:
            failures.append(f"{state.describe()} — container is restarting")
            continue
        if state.status != "running":
            pending.append(f"{state.describe()} — not running yet")
            continue
        if state.health == "unhealthy":
            failures.append(f"{state.describe()} — healthcheck reports unhealthy")
            continue
        if state.has_healthcheck and state.health != "healthy":
            pending.append(f"{state.describe()} — waiting for healthy")

    return Verdict(failures=failures, pending=pending)


def _run(argv: list[str]) -> str:
    """Run a command and return stdout; raises on a non-zero exit."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def list_argv(compose_file: str | None, project: str | None) -> list[str]:
    """The command used to enumerate the stack's containers.

    With an explicit project this filters on the Compose project label instead
    of going through ``docker compose``. That matters: ``docker compose ps``
    interpolates the whole compose file first, so a stack whose file declares
    ``${REDIS_PASSWORD:?...}`` fails to even *list* its containers unless the
    caller happens to have the deploy environment loaded -- and a health gate
    that needs the secrets to report health is no gate at all.
    """
    if project and not compose_file:
        return [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ]
    argv = ["docker", "compose"]
    if compose_file:
        argv += ["-f", compose_file]
    if project:
        argv += ["-p", project]
    argv += ["ps", "-aq"]
    return argv


def container_ids(compose_file: str | None, project: str | None) -> list[str]:
    """Container ids in the compose project."""
    argv = list_argv(compose_file, project)
    return [line.strip() for line in _run(argv).splitlines() if line.strip()]


def inspect(ids: list[str]) -> list[ContainerState]:
    if not ids:
        return []
    return parse_inspect(_run(["docker", "inspect", *ids]))


def _report(verdict: Verdict) -> None:
    for line in verdict.failures:
        log.error("FAIL %s", line)
    for line in verdict.pending:
        log.info("wait %s", line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nce.deploy_health",
        description="Fail the deploy when containers do not become healthy.",
    )
    parser.add_argument("-f", "--file", help="compose file (default: docker compose default)")
    parser.add_argument("-p", "--project", help="compose project name")
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "one-shot verdict for a watchdog: non-zero while anything is unhealthy "
            "or crash-looping. Deliberately stopped containers are ignored unless "
            "--include-exited is given"
        ),
    )
    parser.add_argument(
        "--include-exited",
        action="store_true",
        help="with --once, also fail on stopped containers (default: deploy-gate mode does)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    try:
        ids = container_ids(args.file, args.project)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # Surface the underlying stderr: the usual cause is an uninterpolable
        # compose file, and "returned non-zero exit status 1" says nothing.
        detail = (getattr(exc, "stderr", "") or "").strip()
        log.error("could not list containers: %s", exc)
        if detail:
            log.error("  %s", detail)
        log.error(
            "if the compose file needs deploy secrets to interpolate, pass "
            "-p <project> to enumerate by Compose project label instead"
        )
        return EXIT_UNHEALTHY
    if not ids:
        log.error("no containers found — was `compose up` run?")
        return EXIT_NO_CONTAINERS

    baseline = {s.name: s.restart_count for s in inspect(ids)}
    fail_on_exited = args.include_exited or not args.once

    deadline = time.monotonic() + args.timeout
    while True:
        states = inspect(ids)
        verdict = evaluate(states, baseline_restarts=baseline, fail_on_exited=fail_on_exited)
        _report(verdict)
        if verdict.failures:
            return EXIT_UNHEALTHY
        if verdict.ok:
            stopped = sorted(s.name for s in states if s.status in {"exited", "dead"})
            if stopped:
                # Never call a stack fully healthy while part of it is not
                # running; the watchdog ignores stopped containers by choice.
                log.info(
                    "%d of %d container(s) healthy; %d stopped and ignored: %s",
                    len(states) - len(stopped),
                    len(states),
                    len(stopped),
                    ", ".join(stopped),
                )
            else:
                log.info("all %d container(s) healthy", len(states))
            return EXIT_OK
        if args.once:
            # Pending is not a failure for a watchdog: a container mid-start is
            # normal. Only the failure cases above are alertable.
            return EXIT_OK
        if time.monotonic() >= deadline:
            log.error(
                "timed out after %.0fs waiting for containers to become healthy", args.timeout
            )
            return EXIT_TIMEOUT
        time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main())
