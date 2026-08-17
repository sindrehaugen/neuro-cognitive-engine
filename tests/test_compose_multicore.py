"""Multicore configuration acceptance (NCE_MASTER_PLAN VI.5a, Batch 60).

Structural assertions over the parsed ``docker-compose.yml``:

* the three stateless HTTP services run N uvicorn worker processes;
* the ``worker`` (RQ) service runs M replicas;
* ``cron`` stays a singleton (CronLock is the only split-brain guard);
* CPU-thread env vars are pinned on every compute-bearing service;
* no background-loop service was given ``--workers``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _REPO_ROOT / "docker-compose.yml"

# Stateless HTTP services that are safe to run with multiple uvicorn workers.
_HTTP_SERVICES = ("admin", "a2a", "webhook-receiver")
# Services that must NOT carry --workers because they run background loops
# in-process (duplicating them would double GC/outbox/re-embed/cron work).
_BACKGROUND_LOOP_SERVICES = ("worker", "cron")
_THREAD_ENV_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "TOKENIZERS_PARALLELISM")


def _load() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _command(service: dict) -> list[str]:
    cmd = service.get("command", [])
    if isinstance(cmd, str):
        return cmd.split()
    return list(cmd)


def test_compose_yaml_parses() -> None:
    doc = _load()
    assert "services" in doc
    for name in (*_HTTP_SERVICES, *_BACKGROUND_LOOP_SERVICES):
        assert name in doc["services"], f"missing service {name}"


def test_http_services_declare_n_worker_processes() -> None:
    """Each stateless HTTP service runs >1 uvicorn worker (or replica)."""
    services = _load()["services"]
    for name in _HTTP_SERVICES:
        svc = services[name]
        cmd = _command(svc)
        replicas = svc.get("deploy", {}).get("replicas")
        has_workers = "--workers" in cmd
        has_scaled_replicas = isinstance(replicas, (int, str)) and "--workers" not in cmd
        assert has_workers or has_scaled_replicas, (
            f"{name} must scale via --workers N or deploy.replicas N"
        )
        if has_workers:
            value = cmd[cmd.index("--workers") + 1]
            assert value, f"{name} --workers has no value"
            # ${ADMIN_WORKERS:-2} style default must resolve to >1.
            assert (
                ":-2" in value
                or ":-3" in value
                or ":-4" in value
                or (value.isdigit() and int(value) > 1)
            ), f"{name} --workers default must be >1, got {value!r}"


def test_worker_runs_multiple_replicas() -> None:
    """The RQ worker service scales to M (>1) replicas; lanes unchanged."""
    worker = _load()["services"]["worker"]
    replicas = worker.get("deploy", {}).get("replicas")
    assert replicas is not None, "worker must declare deploy.replicas"
    text = str(replicas)
    assert ":-2" in text or ":-3" in text or ":-4" in text or (text.isdigit() and int(text) > 1), (
        f"worker replicas default must be >1, got {replicas!r}"
    )
    # Replicas > 1 require no container_name pin.
    assert "container_name" not in worker, "worker with replicas>1 must not set container_name"


def test_cron_stays_singleton() -> None:
    """cron must remain exactly one replica (CronLock guard)."""
    cron = _load()["services"]["cron"]
    replicas = cron.get("deploy", {}).get("replicas")
    assert replicas == 1, f"cron must be a singleton (replicas: 1), got {replicas!r}"
    assert "--workers" not in _command(cron)


def test_background_loop_services_have_no_workers_flag() -> None:
    """No in-process background-loop service may carry --workers."""
    services = _load()["services"]
    for name in _BACKGROUND_LOOP_SERVICES:
        cmd = _command(services[name])
        assert "--workers" not in cmd, (
            f"{name} runs background loops in-process; --workers would duplicate them"
        )


def test_cpu_thread_env_vars_pinned() -> None:
    """Compute-bearing services pin OMP/MKL/TOKENIZERS thread counts."""
    services = _load()["services"]
    for name in (*_HTTP_SERVICES, *_BACKGROUND_LOOP_SERVICES):
        env = services[name].get("environment", {})
        assert isinstance(env, dict), f"{name} environment must be a mapping"
        for var in _THREAD_ENV_VARS:
            assert var in env, f"{name} is missing CPU-thread env var {var}"
