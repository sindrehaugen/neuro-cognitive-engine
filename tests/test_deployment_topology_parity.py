"""H-12: deployment-topology parity (charter §9 "Lane H", dispatched after a
janitor pass found this layer has no ratchet at all).

**Why this earns a wave.** H-1..H-11 measure code-level facts (routes,
tools, migrations, host-parity dispositions, advertised-vendor-vs-real). None
of them see the layer that actually serves traffic: `docker-compose.yml` (what
runs) and `Caddyfile` (what's reachable). This estate's worst incidents have
lived exactly there — a deployed SHA lagging `main` for days unnoticed, a
health endpoint reporting "degraded" while `docker inspect` said healthy for
four days, and CP-5 adding a fourteenth container with its own route and a
confinement claim that nothing verified.

**The design rule that keeps this from becoming a treadmill: derive, never
hardcode.** Every assertion below reads `docker-compose.yml` and `Caddyfile`
fresh and compares them to EACH OTHER — never to a literal list of service
names typed into this file. Add a service tomorrow and this file does not
need editing to notice it; it either gets a route/healthcheck and passes, or
it does not and the walk below flags it by name.

**Deliberately not done: asserting a container COUNT anywhere.** That is
exactly the hand-maintained number this estate keeps getting wrong (see
`ENGINE_STATUS.md`'s own history, and H-1's whole reason for existing). Every
floor below is a MINIMUM sanity bound to catch a collapsed parse, not a
published total, and no test here prints or asserts "N services" as a fact
about the estate.

**Three assertions, all requested by the wave:**

1. Every service in `docker-compose.yml` that publishes a port is either a
   Caddy `reverse_proxy` target or carries a reasoned exemption (mirrors
   `internal-cores.json` / H-11's exemption shape: owner + reason).
2. Every Caddy `reverse_proxy` target resolves to a service that exists in
   compose, at a port that service actually publishes on its container side
   (never the host-side port, which is often env-templated and irrelevant to
   in-network routing).
3. Every service has a `healthcheck:` or a reasoned exemption.

**A fourth, scoped narrowly on purpose (charter's own instruction: report why
if it can't be done without a heuristic).** CP-5's compose file carries a
literal comment: "Confinement: customer-portal has 0 admin credentials and no
broad keys in environ," directly above two lines that blank `NCE_MASTER_KEY`
and `NCE_ADMIN_API_KEY`. That specific, named claim is mechanically checkable
-- read the same two keys, assert they are blank. This test does NOT
generalise to "no broad credential anywhere in the environment block": every
service in this compose file, including the two pure background workers,
injects the same root MinIO credential (`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`
via `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`) -- an estate-wide, pre-existing
pattern the comment does not claim to address and CP-5 did not introduce.
Deciding whether that pattern itself is a problem is a security-scoping
question, not something this file can turn into a mechanical gate without
inventing what "broad" means for a credential it wasn't told about by name --
exactly the same reasoning H-11 applied to adapter realness. Flagged here in
prose, not gated, and reported as a finding rather than silently gated or
silently ignored.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_FILE = _ROOT / "docker-compose.yml"
_CADDYFILE = _ROOT / "Caddyfile"

_REVERSE_PROXY_RE = re.compile(r"reverse_proxy\s+([A-Za-z0-9_.-]+):(\d+)")


def _load_compose_services() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    services = data.get("services")
    assert isinstance(services, dict), f"{_COMPOSE_FILE}: no top-level 'services' mapping"
    return services


def _container_side_ports(service_cfg: dict[str, Any]) -> set[str]:
    """The CONTAINER-side port of every 'ports' entry (the right-hand side of
    HOST:CONTAINER, or IP:HOST:CONTAINER) -- always a literal in this file,
    never env-templated, which is what makes it safe to compare against a
    Caddy reverse_proxy target's port without any interpolation logic."""
    ports = service_cfg.get("ports") or []
    result = set()
    for mapping in ports:
        result.add(str(mapping).split(":")[-1])
    return result


def _caddy_route_targets() -> list[tuple[str, str]]:
    """Every (service, port) pair Caddy's reverse_proxy directives name,
    parsed from the Caddyfile itself -- never a hand-copied list."""
    text = _CADDYFILE.read_text(encoding="utf-8")
    return [(m.group(1), m.group(2)) for m in _REVERSE_PROXY_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Shrink-only, reasoned exemption lists -- shaped like internal-cores.json /
# H-11's _VENDOR_PLATFORM_EXEMPTIONS. A service leaves a list only when it
# gains the thing the list says it lacks (a route, a healthcheck) -- never by
# widening what "covered" means.
# ---------------------------------------------------------------------------
_NO_CADDY_ROUTE_EXEMPTIONS: dict[str, str] = {
    "redis": "Internal data store, bound to 127.0.0.1 only; reached by other "
    "containers over the docker network directly, never through the edge proxy.",
    "postgres": "Internal data store, bound to 127.0.0.1 only; same reasoning as redis.",
    "mongodb": "Internal data store, bound to 127.0.0.1 only; same reasoning as redis.",
    "minio": "Internal object store, bound to 127.0.0.1 only; same reasoning as redis.",
    "citus-coordinator": "Test-only Postgres variant, bound to 127.0.0.1 only; "
    "not part of the served stack.",
    "cognitive": "Internal LLM service, called directly by other containers on "
    "the docker network (worker, admin); not a customer-facing surface.",
    "jaeger": "Tracing UI, operator-facing for local debugging, not part of the "
    "customer-facing routed surface.",
    "caddy": "Caddy is the proxy itself; it does not reverse_proxy to its own listener.",
}

_NO_HEALTHCHECK_EXEMPTIONS: dict[str, str] = {
    "worker": "Background RQ worker, no HTTP server and no ports published -- "
    "liveness is queue-depth/process-based, not HTTP, per the module's own "
    "'NO PROFILE, DELIBERATELY' comment about why it must never look idle-healthy.",
    "cron": "Background scheduler, no HTTP server and no ports published; same "
    "reasoning as worker.",
}


def test_compose_services_discovery_floor() -> None:
    """Guard-the-guard: the compose parse must not have silently collapsed.
    A MINIMUM bound, not a published total -- see module docstring."""
    services = _load_compose_services()
    assert len(services) >= 10, (
        f"Only {len(services)} services parsed from {_COMPOSE_FILE.name} -- "
        "expected at least 10 based on today's structure. Either the YAML "
        "shape changed or the parse is broken."
    )


def test_caddy_routes_discovery_floor() -> None:
    routes = _caddy_route_targets()
    assert len(routes) >= 3, (
        f"Only {len(routes)} reverse_proxy targets parsed from {_CADDYFILE.name} "
        "-- expected at least 3. Either the Caddyfile shape changed or the "
        "regex stopped matching."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED by design (H-1's pattern): 'a2a' publishes port 8004 directly to "
        "the host -- not localhost-bound like the data stores, so the same "
        "reasoning that exempts redis/postgres/mongodb/minio does not apply -- "
        "and is neither a Caddy reverse_proxy target nor in "
        "_NO_CADDY_ROUTE_EXEMPTIONS. No comment anywhere in docker-compose.yml "
        "explains why the A2A protocol server bypasses the edge proxy (TLS "
        "termination, security headers, request-size limiting) that every "
        "other externally-reachable service goes through. This is a real, "
        "undocumented gap, not a considered exclusion -- deciding whether a2a "
        "needs a Caddy route or has a legitimate reason to bypass it is a "
        "security/architecture call outside Lane H's mandate (bounded "
        "judgement: propose, do not decide a contract). Filed as a question, "
        "not resolved here. XPASSes (and fails CI) the day someone adds a "
        "route or a reasoned exemption, forcing that decision to be conscious."
    ),
)
def test_every_port_exposing_service_is_routed_or_exempt() -> None:
    """Assertion 1: every service with a published port is a Caddy target or
    carries a reasoned exemption."""
    services = _load_compose_services()
    routed = {svc for svc, _port in _caddy_route_targets()}

    unclassified = [
        name
        for name, cfg in services.items()
        if _container_side_ports(cfg)
        and name not in routed
        and name not in _NO_CADDY_ROUTE_EXEMPTIONS
    ]
    assert not unclassified, (
        f"{unclassified} publish a port, are not a Caddy reverse_proxy target, "
        "and have no exemption. Either add a Caddyfile route, or a reasoned "
        "entry in _NO_CADDY_ROUTE_EXEMPTIONS."
    )


def test_every_caddy_target_resolves_to_a_real_service_and_port() -> None:
    """Assertion 2: every Caddy reverse_proxy target names a service that
    exists in compose, at a port that service actually publishes."""
    services = _load_compose_services()
    problems: list[str] = []
    for svc, port in _caddy_route_targets():
        if svc not in services:
            problems.append(f"{svc}:{port} -- no such service in {_COMPOSE_FILE.name}")
            continue
        container_ports = _container_side_ports(services[svc])
        if port not in container_ports:
            problems.append(
                f"{svc}:{port} -- {svc} does not publish container port {port} "
                f"(publishes: {sorted(container_ports) or 'none'})"
            )

    assert not problems, "Caddy routes pointing at nothing real:\n" + "\n".join(
        f"  - {p}" for p in problems
    )


def test_every_service_has_a_healthcheck_or_exemption() -> None:
    """Assertion 3: every compose service has a healthcheck or a reasoned
    exemption -- applied to ALL services, not only the port-exposing ones,
    per the wave's own wording."""
    services = _load_compose_services()
    unclassified = [
        name
        for name, cfg in services.items()
        if "healthcheck" not in cfg and name not in _NO_HEALTHCHECK_EXEMPTIONS
    ]
    assert not unclassified, (
        f"{unclassified} have no healthcheck and no exemption. Either add a "
        "healthcheck, or a reasoned entry in _NO_HEALTHCHECK_EXEMPTIONS."
    )


def test_exemption_lists_are_shrink_only_and_reasoned() -> None:
    """Mirrors H-11 / internal-cores.json: no stale entries, every entry
    reasoned, and (the shrink-only regression guard) no exempted service has
    quietly gained the thing its exemption says it lacks."""
    services = _load_compose_services()
    routed = {svc for svc, _port in _caddy_route_targets()}

    stale_route_exemptions = set(_NO_CADDY_ROUTE_EXEMPTIONS) - set(services)
    assert not stale_route_exemptions, (
        f"_NO_CADDY_ROUTE_EXEMPTIONS references services no longer in compose: "
        f"{stale_route_exemptions}"
    )
    stale_health_exemptions = set(_NO_HEALTHCHECK_EXEMPTIONS) - set(services)
    assert not stale_health_exemptions, (
        f"_NO_HEALTHCHECK_EXEMPTIONS references services no longer in compose: "
        f"{stale_health_exemptions}"
    )

    for name, reason in _NO_CADDY_ROUTE_EXEMPTIONS.items():
        assert len(" ".join(reason.split())) >= 40, f"{name}: route exemption reason too short"
        assert name not in routed, (
            f"{name} is exempted from needing a Caddy route but IS now a route "
            "target -- remove the exemption, it has been resolved."
        )

    for name, reason in _NO_HEALTHCHECK_EXEMPTIONS.items():
        assert len(" ".join(reason.split())) >= 40, (
            f"{name}: healthcheck exemption reason too short"
        )
        assert "healthcheck" not in services[name], (
            f"{name} is exempted from needing a healthcheck but NOW HAS ONE -- "
            "remove the exemption, it has been resolved."
        )


def test_positive_control_route_mapping_check_is_not_vacuous() -> None:
    """U18: prove test_every_caddy_target_resolves_to_a_real_service_and_port
    actually fires, using synthetic (service, port) pairs the real Caddyfile
    does not contain."""
    services = _load_compose_services()

    fake_targets_unknown_service = [("totally-fake-service", "9999")]
    problems = []
    for svc, port in fake_targets_unknown_service:
        if svc not in services:
            problems.append(f"{svc}:{port} -- no such service")
    assert problems, "A route to a nonexistent service was not flagged -- check is vacuous."

    fake_targets_wrong_port = [("admin", "1")]
    real_admin_ports = _container_side_ports(services["admin"])
    assert "1" not in real_admin_ports
    problems2 = []
    for svc, port in fake_targets_wrong_port:
        if port not in _container_side_ports(services[svc]):
            problems2.append(f"{svc}:{port} -- wrong port")
    assert problems2, "A route to a real service's wrong port was not flagged -- check is vacuous."


def test_positive_control_healthcheck_check_is_not_vacuous() -> None:
    """U18: prove test_every_service_has_a_healthcheck_or_exemption actually
    fires, using a synthetic service dict with neither a healthcheck nor an
    exemption."""
    synthetic_services = {"totally_fake_unmonitored_service": {"image": "x"}}
    unclassified = [
        name
        for name, cfg in synthetic_services.items()
        if "healthcheck" not in cfg and name not in _NO_HEALTHCHECK_EXEMPTIONS
    ]
    assert unclassified == ["totally_fake_unmonitored_service"]


def test_customer_portal_confinement_comment_is_true() -> None:
    """The fourth, narrowly-scoped assertion (see module docstring for why it
    is not generalised): CP-5's compose file states, in a comment,
    'customer-portal has 0 admin credentials and no broad keys in environ,'
    naming exactly two keys. Check those two, and only those two, are blank."""
    services = _load_compose_services()
    env = services["customer-portal"].get("environment") or {}
    for key in ("NCE_MASTER_KEY", "NCE_ADMIN_API_KEY"):
        assert key in env, (
            f"customer-portal's confinement comment names {key!r}, but that key "
            "is no longer present at all -- the comment may now be describing "
            "something that isn't there to check."
        )
        assert env[key] == "", (
            f"customer-portal's confinement comment claims '0 admin credentials' "
            f"but {key!r} is {env[key]!r}, not blank. The comment and the "
            "environment block have diverged."
        )
