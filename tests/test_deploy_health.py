"""Deploy-time health gate: the verdict that Compose does not act on.

Fix recipe #2, item 5. On 2026-08-27 nce-admin and nce-a2a went ``unhealthy``
and stayed there, crash-looping inside their own uvicorn supervisors. Because
the supervisor process never exited, ``docker ps`` reported both ``Up``; plain
Compose acts on a healthcheck verdict in no way at all, so there was no alert,
no restart and no non-zero exit anywhere.

``nce.deploy_health`` turns that verdict into an exit code. The judgement is a
pure function of parsed ``docker inspect`` output, asserted here without Docker.
"""

from __future__ import annotations

import json

import pytest

import nce.deploy_health as deploy_health
from nce.deploy_health import ContainerState, evaluate, list_argv, parse_inspect


def _inspect_payload(**overrides) -> str:
    """One container in ``docker inspect`` shape, with fields overridable."""
    entry = {
        "Name": overrides.pop("name", "/nce-admin"),
        "RestartCount": overrides.pop("restart_count", 0),
        "State": {
            "Status": overrides.pop("status", "running"),
            "ExitCode": overrides.pop("exit_code", 0),
            "Restarting": overrides.pop("restarting", False),
            "Health": {"Status": overrides.pop("health", "healthy")},
        },
    }
    assert not overrides, overrides
    return json.dumps([entry])


def _state(**kwargs) -> ContainerState:
    defaults = {"name": "nce-admin", "status": "running", "health": "healthy"}
    defaults.update(kwargs)
    return ContainerState(**defaults)  # type: ignore[arg-type]


class TestParsing:
    def test_empty_payload_is_no_containers(self) -> None:
        assert parse_inspect("") == []
        assert parse_inspect("   \n") == []

    def test_leading_slash_is_stripped_from_the_name(self) -> None:
        """docker inspect reports /nce-admin; every human reference omits it."""
        (state,) = parse_inspect(_inspect_payload(name="/nce-admin"))
        assert state.name == "nce-admin"

    def test_fields_are_read_from_the_nested_state(self) -> None:
        (state,) = parse_inspect(
            _inspect_payload(status="exited", health="unhealthy", exit_code=3, restart_count=7)
        )
        assert state.status == "exited"
        assert state.health == "unhealthy"
        assert state.exit_code == 3
        assert state.restart_count == 7

    def test_a_bare_object_is_accepted_as_well_as_a_list(self) -> None:
        entry = json.loads(_inspect_payload())[0]
        (state,) = parse_inspect(json.dumps(entry))
        assert state.name == "nce-admin"

    def test_a_container_without_a_healthcheck_has_no_health(self) -> None:
        """postgres in the alternate compose file declares none; it must not fail."""
        payload = json.dumps([{"Name": "/nce-redis", "State": {"Status": "running"}}])
        (state,) = parse_inspect(payload)
        assert state.health == ""
        assert state.has_healthcheck is False

    def test_missing_numeric_fields_default_rather_than_raise(self) -> None:
        payload = json.dumps([{"Name": "/x", "State": {"Status": "running"}}])
        (state,) = parse_inspect(payload)
        assert state.restart_count == 0
        assert state.exit_code == 0


class TestTheFailureThatWasMissed:
    """The exact 2026-08-27 shape: unhealthy, but the container says Up."""

    def test_unhealthy_while_running_is_a_failure(self) -> None:
        verdict = evaluate([_state(status="running", health="unhealthy")])
        assert verdict.failures
        assert not verdict.ok
        assert verdict.settled, "an unhealthy container must stop the wait, not extend it"

    def test_the_failure_names_the_container_and_says_why(self) -> None:
        (line,) = evaluate([_state(health="unhealthy")]).failures
        assert "nce-admin" in line
        assert "unhealthy" in line


class TestCrashLoopDetection:
    """A container that does exit now shows up as restart-count growth."""

    def test_restart_growth_during_the_wait_is_a_failure(self) -> None:
        verdict = evaluate(
            [_state(restart_count=4, health="starting")],
            baseline_restarts={"nce-admin": 1},
        )
        assert verdict.failures
        assert "crash loop" in verdict.failures[0]

    def test_a_stable_restart_count_is_not_a_failure(self) -> None:
        """Containers restarted long before this deploy are not this deploy's problem."""
        verdict = evaluate(
            [_state(restart_count=9)],
            baseline_restarts={"nce-admin": 9},
        )
        assert verdict.ok

    def test_restart_growth_is_ignored_without_a_baseline(self) -> None:
        """--once has no baseline; a historical count must not alert forever."""
        assert evaluate([_state(restart_count=9)]).ok

    def test_a_restarting_container_is_a_failure(self) -> None:
        verdict = evaluate([_state(status="restarting", restarting=True)])
        assert verdict.failures

    def test_an_exited_container_fails_the_deploy_gate(self) -> None:
        """This is what a refused pre-flight looks like from outside."""
        verdict = evaluate([_state(status="exited", exit_code=1, health="")])
        assert verdict.failures
        assert "exit=1" in verdict.failures[0]


class TestStoppedContainers:
    """`docker stop` is an operator decision and looks identical to a crash."""

    def test_a_stopped_container_does_not_alert_a_watchdog(self) -> None:
        verdict = evaluate([_state(status="exited", health="unhealthy")], fail_on_exited=False)
        assert verdict.ok, verdict.failures

    def test_a_stopped_container_still_fails_the_deploy_gate(self) -> None:
        """Straight after `compose up`, nothing should be exited."""
        verdict = evaluate([_state(status="exited", health="unhealthy")], fail_on_exited=True)
        assert verdict.failures

    def test_a_stopped_container_is_reported_once_and_for_the_right_reason(self) -> None:
        """A stopped container keeps its last health status; judging it double-reports.

        The two containers stopped during the 2026-08-27 remediation still
        inspect as ``health=unhealthy`` hours later.
        """
        (line,) = evaluate([_state(status="exited", health="unhealthy")]).failures
        assert "not running" in line
        assert "reports unhealthy" not in line

    def test_a_running_unhealthy_container_still_alerts_a_watchdog(self) -> None:
        """Guard the guard: ignoring stopped containers must not ignore sick ones."""
        verdict = evaluate([_state(status="running", health="unhealthy")], fail_on_exited=False)
        assert verdict.failures

    def test_a_crash_looping_container_still_alerts_a_watchdog(self) -> None:
        verdict = evaluate(
            [_state(status="running", restart_count=5)],
            baseline_restarts={"nce-admin": 2},
            fail_on_exited=False,
        )
        assert verdict.failures


class TestPendingVersusFailure:
    """Confusing a slow start for a failure would fail every deploy."""

    def test_starting_health_is_pending_not_failure(self) -> None:
        verdict = evaluate([_state(health="starting")])
        assert not verdict.failures
        assert verdict.pending
        assert not verdict.settled

    def test_created_status_is_pending(self) -> None:
        verdict = evaluate([_state(status="created", health="")])
        assert not verdict.failures
        assert verdict.pending

    def test_a_running_container_without_a_healthcheck_passes(self) -> None:
        assert evaluate([_state(status="running", health="")]).ok

    def test_all_healthy_passes(self) -> None:
        verdict = evaluate(
            [
                _state(name="nce-admin"),
                _state(name="nce-a2a"),
                _state(name="nce-redis", health=""),
            ]
        )
        assert verdict.ok
        assert verdict.settled

    def test_no_containers_is_vacuously_ok(self) -> None:
        """Guard the guard: an empty list must not read as success in the CLI.

        ``evaluate([])`` is trivially ok, which is why ``main`` treats an empty
        container list as its own non-zero exit rather than passing it here.
        """
        assert evaluate([]).ok


class TestOneFailureIsEnough:
    def test_a_single_unhealthy_container_fails_a_healthy_stack(self) -> None:
        verdict = evaluate(
            [
                _state(name="nce-postgres"),
                _state(name="nce-admin", health="unhealthy"),
                _state(name="nce-mongo"),
            ]
        )
        assert len(verdict.failures) == 1
        assert "nce-admin" in verdict.failures[0]
        assert not verdict.ok

    def test_every_failing_container_is_reported_not_just_the_first(self) -> None:
        verdict = evaluate(
            [
                _state(name="nce-admin", health="unhealthy"),
                _state(name="nce-a2a", health="unhealthy"),
            ]
        )
        assert len(verdict.failures) == 2

    def test_a_failure_outranks_a_pending_sibling(self) -> None:
        """Otherwise the gate waits out its whole timeout on a known failure."""
        verdict = evaluate(
            [
                _state(name="nce-admin", health="unhealthy"),
                _state(name="nce-a2a", health="starting"),
            ]
        )
        assert verdict.settled
        assert verdict.failures


class TestContainerEnumeration:
    """`docker compose ps` interpolates the compose file before it can list."""

    def test_a_project_is_enumerated_by_label_without_compose(self) -> None:
        """Otherwise a stack whose file needs ${REDIS_PASSWORD:?} cannot be checked."""
        argv = list_argv(None, "neuro-cognitiveengine")
        assert argv[:3] == ["docker", "ps", "-aq"]
        assert "label=com.docker.compose.project=neuro-cognitiveengine" in argv
        assert "compose" not in argv

    def test_no_project_falls_back_to_docker_compose(self) -> None:
        assert list_argv(None, None) == ["docker", "compose", "ps", "-aq"]

    def test_an_explicit_compose_file_is_honoured(self) -> None:
        argv = list_argv("deploy/multiuser/docker-compose.yml", None)
        assert argv == [
            "docker",
            "compose",
            "-f",
            "deploy/multiuser/docker-compose.yml",
            "ps",
            "-aq",
        ]

    def test_a_compose_file_plus_project_uses_compose(self) -> None:
        """An explicit file is a deliberate choice; do not silently ignore it."""
        argv = list_argv("x.yml", "proj")
        assert argv == ["docker", "compose", "-f", "x.yml", "-p", "proj", "ps", "-aq"]


class TestCliWiring:
    """The mode split lives in main(); evaluate() alone cannot prove it."""

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, states: list[ContainerState]) -> None:
        monkeypatch.setattr(deploy_health, "container_ids", lambda f, p: ["cid"])
        monkeypatch.setattr(deploy_health, "inspect", lambda ids: states)

    def test_once_ignores_a_stopped_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub(monkeypatch, [_state(status="exited", health="unhealthy")])
        assert deploy_health.main(["--once"]) == deploy_health.EXIT_OK

    def test_once_with_include_exited_fails_on_a_stopped_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, [_state(status="exited", health="unhealthy")])
        assert deploy_health.main(["--once", "--include-exited"]) == deploy_health.EXIT_UNHEALTHY

    def test_the_deploy_gate_fails_on_a_stopped_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --once: straight after `compose up`, exited is a failed deploy."""
        self._stub(monkeypatch, [_state(status="exited", health="unhealthy")])
        assert deploy_health.main([]) == deploy_health.EXIT_UNHEALTHY

    def test_once_still_fails_on_a_running_unhealthy_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, [_state(status="running", health="unhealthy")])
        assert deploy_health.main(["--once"]) == deploy_health.EXIT_UNHEALTHY

    def test_a_healthy_stack_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub(monkeypatch, [_state()])
        assert deploy_health.main([]) == deploy_health.EXIT_OK

    def test_no_containers_is_its_own_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty stack is vacuously healthy to evaluate(); it is not a pass."""
        monkeypatch.setattr(deploy_health, "container_ids", lambda f, p: [])
        assert deploy_health.main([]) == deploy_health.EXIT_NO_CONTAINERS

    def test_a_pending_container_times_out_rather_than_passing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, [_state(health="starting")])
        assert (
            deploy_health.main(["--timeout", "0", "--interval", "0"]) == deploy_health.EXIT_TIMEOUT
        )

    def test_a_pending_container_does_not_alert_a_watchdog(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mid-start is normal on a live stack; only failures are alertable."""
        self._stub(monkeypatch, [_state(health="starting")])
        assert deploy_health.main(["--once"]) == deploy_health.EXIT_OK
