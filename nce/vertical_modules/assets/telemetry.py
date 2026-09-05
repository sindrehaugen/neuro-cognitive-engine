"""
nce/vertical_modules/assets/telemetry.py
==========================================
Pull manufacturer telemetry for one asset and persist it as rows in migration
057's ``telemetry_samples`` (Module 9, Wave 5 — ``telemetry-adapter``,
Batch 145).

Per ``docs/vertical_engines/09-assets-engine.md``'s ``do_pull_telemetry`` spec
and its "Tables/migrations" section (``telemetry_samples`` is the "high-write
telemetry stream").

This module writes NO graph — declared here, never silently omitted
--------------------------------------------------------------------------
The engine doc says ``do_pull_telemetry`` "writes ``TELEMETRY`` nodes +
``monitored_by`` edges". **This wave does not**, by orchestrator decision: a
telemetry sample is a ROW IN A TABLE, not a node and not an edge. Nothing here
writes ``kg_nodes`` or ``kg_edges``, calls ``assert_owner``, or imports
``nce.entity_resolution.ownership`` / ``nce.events.emit``, and no row is added
to ``nce/config_data/node-ownership.json``.

That has a direct consequence worth stating rather than leaving to be
discovered: ``TELEMETRY`` is **not** a registered node type, so the
deny-by-default ``assert_owner`` guard would (correctly) refuse a
``TELEMETRY`` node write from this package today. The ``TELEMETRY`` node and
the ``ASSET -[monitored_by]-> TELEMETRY`` edge are a separate projection
wave's — the same split ``seed.py`` declared for the ``ASSET`` node before
Batch 142b built it.

What a "vendor stub" is here, and what it is NOT
---------------------------------------------------
Five manufacturer platforms are named — ``crestron``, ``qsys``, ``neat``,
``huddly``, ``poly`` — and **none of them has a real API client in this
wave.** :class:`UnimplementedVendorAdapter` implements
:class:`TelemetryAdapter`, is selected by an env key, and raises
``NotImplementedError`` naming the vendor API that a real adapter would call.
There is no ``httpx`` import, no network code, no credential and no new
dependency anywhere in this file. :class:`MockTelemetryAdapter` is the only
adapter with real behaviour.

This is the "mock-now / swap-ready" architecture the engine doc calls for
(``NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL``, mirroring Andreas's
``CRESTRON_FUSION_REAL=1``): the engine is fully usable **before** any vendor
key lands, and flipping one env var is the whole swap. The flag's meaning is
therefore inverted from what a reader might assume — **unset means mock**, set
means "use the real adapter", and today the real adapter is a stub that
raises. That is deliberate: a deployment that turns the flag on without a
built adapter must FAIL LOUDLY, never silently serve mock numbers as if they
came off the device.

Dependency direction (uncle-bob-craft)
------------------------------------------
:func:`do_pull_telemetry` depends on the :class:`TelemetryAdapter` abstraction
and never on a concrete vendor class. Platform → adapter resolution lives in
exactly one place, :func:`select_telemetry_adapter`, a factory at the edge —
there is no ``if platform == "crestron"`` chain inside the pull logic, and the
five vendors share one parameterised stub class rather than five copies of the
same body. The module imports only ``nce.config.live_env_str`` and
``nce.db_utils.scoped_pg_session``: no web/HTTP/admin framework import and
nothing from another vertical module. ``NCEEngine`` is imported under
``TYPE_CHECKING`` only, matching every other vertical module.

The adapter call is OUTSIDE the database transaction, on purpose
-------------------------------------------------------------------
``scoped_pg_session``'s own docstring forbids slow external I/O inside its
block — the whole block is one transaction, and a 30-second vendor HTTP call
held inside it would bloat locks and vacuum. So this module opens **two**
short sessions (the namespace-scoped existence pre-check, then the insert)
with :meth:`TelemetryAdapter.fetch_samples` in between. The cost is that the
asset could in principle be deleted between the two; the ``ON DELETE CASCADE``
FK makes that a lost sample, not a corrupt row.

Idempotency is BY DB CONSTRAINT, never a check-then-write
------------------------------------------------------------
``telemetry_samples_idempotency_uq`` — ``UNIQUE (namespace_id, asset_id,
metric, sampled_at)``, migration 057 — is the sole arbiter, and the INSERT is
``ON CONFLICT ON CONSTRAINT … DO NOTHING`` naming it so the constraint's
IDENTITY is load-bearing. This matters more here than in most tables: a
telemetry pull is a **cron re-reading overlapping windows**, so the same
reading arrives repeatedly *by design*. ``sampled_at`` is the VENDOR's
instant and ``created_at`` the pull instant, and only the former is in the
key — which is also why :class:`MockTelemetryAdapter` returns a FIXED
synthetic history rather than stamping ``now()``: a mock that moved its
timestamps would make every re-pull look like new data and would make
idempotency untestable.

``written`` (the ``RETURNING`` row count) is the flag any subsequent effect
must hang off. This wave has none — there is no graph write to gate — and it
is precisely the count a later projection wave will use so a replayed pull
does not re-emit.

Scoped explicitly by ``namespace_id``, never by RLS alone
-------------------------------------------------------------
Every statement below carries its own ``namespace_id = $n::uuid`` predicate in
addition to running inside ``scoped_pg_session``. The owner/superuser pool
used by integration tests BYPASSES ``FORCE ROW LEVEL SECURITY``, so an
RLS-only query passes its own test and leaks in production — this has bitten
B67, B120 and B130.

Note precisely what the FK does and does not add (migration 057's header says
the same): ``telemetry_samples_asset_fk`` is a SINGLE-column FK to
``assets(id)``. It proves the asset exists; it does **not** prove the asset
belongs to this row's namespace, because ``assets`` has no
``UNIQUE (id, namespace_id)`` for a composite FK to reference and this wave
does not add a constraint to another wave's table. The namespace binding comes
from FORCE RLS plus :func:`_require_asset_in_namespace` below.

Registration is deliberately NOT this wave's job
----------------------------------------------------
:func:`do_pull_telemetry` is a module-level function. It is not registered as
an MCP tool and has no REST route — ``nce/tool_registry.py`` and
``nce/admin_app.py`` reference nothing in this file, and neither was touched.
It is unreachable from any surface when this wave lands, exactly as
``seed.py`` and ``goods_receipt.py`` were; registration belongs to Module 9's
surface-completion wave.
"""

from __future__ import annotations

import json
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.config import live_env_str
from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.assets.telemetry")

# Engine-authored write, not an external-system sync — mirrors seed.py's /
# rma.py's own 'agent' choice ('sync' is reserved for the D365 origin).
_DEFAULT_CHANGE_ORIGIN = "agent"

# Migration 057's named idempotency arbiter. Named (not the column list) in
# the ON CONFLICT below so the constraint's IDENTITY is load-bearing: rename
# or drop it and this module fails loudly instead of silently duplicating
# every re-pulled reading.
_IDEMPOTENCY_CONSTRAINT = "telemetry_samples_idempotency_uq"

#: The one platform with real behaviour in this wave.
MOCK_PLATFORM = "mock"

#: The five manufacturer platforms named in ``09-assets-engine.md``, mapped to
#: the vendor API a real adapter would call. The mapping is data, not a
#: dispatch table of classes: all five share one stub implementation, so
#: onboarding a sixth platform is a line here plus (later) its real adapter.
VENDOR_PLATFORMS: dict[str, str] = {
    "crestron": "Crestron XiO Cloud / Fusion xAPI",
    "qsys": "Q-SYS Reflect Enterprise Manager API",
    "neat": "Neat Pulse API",
    "huddly": "Huddly device API",
    "poly": "Poly Lens API",
}

_TRUTHY = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# The sample — the unit every adapter speaks in.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One reading, as an adapter reports it.

    ``sampled_at`` is the instant the VENDOR took the reading, not the instant
    we pulled it — the distinction the idempotency key rests on. ``raw`` is the
    adapter's untouched payload for this sample, so a later health writer can
    recover vendor fields ``telemetry_samples`` does not model.
    """

    metric: str
    value: float
    sampled_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The abstraction. do_pull_telemetry depends on THIS and nothing below it.
# ---------------------------------------------------------------------------


class TelemetryAdapter(ABC):
    """One manufacturer telemetry platform, as this engine needs to see it.

    Implementations are constructed only by :func:`select_telemetry_adapter`.
    An implementation may raise ``NotImplementedError`` from
    :meth:`fetch_samples` — that is the declared shape of a not-yet-built
    vendor adapter, and callers must let it propagate rather than degrade to
    mock data.
    """

    @property
    @abstractmethod
    def platform(self) -> str:
        """The platform key this adapter actually speaks for."""

    @abstractmethod
    async def fetch_samples(self, asset_id: UUID) -> Sequence[TelemetrySample]:
        """Return the readings currently available for *asset_id*.

        Called OUTSIDE any database transaction (module docstring), so a real
        implementation may perform network I/O here.
        """


# ---------------------------------------------------------------------------
# The one adapter with real behaviour.
# ---------------------------------------------------------------------------

#: A fixed instant, not ``now()``. See the module docstring: a mock whose
#: timestamps moved would make every re-pull look like new data.
_MOCK_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: ``(metric, modulus)`` — the synthetic history's shape. The modulus keeps
#: each metric in a plausible range while the value stays derived from the
#: asset id, so two assets differ and one asset never does.
_MOCK_METRICS: tuple[tuple[str, int], ...] = (
    ("uptime_seconds", 86_400),
    ("temperature_celsius", 60),
    ("packet_loss_percent", 5),
)


class MockTelemetryAdapter(TelemetryAdapter):
    """The default adapter: a FIXED synthetic history per asset.

    Deterministic in both axes on purpose. The values are derived from
    ``asset_id`` so two assets do not look identical, and the instants are
    derived from a fixed epoch so re-pulling one asset is a genuine REPLAY —
    which is what makes ``telemetry_samples_idempotency_uq`` testable at all.
    """

    @property
    def platform(self) -> str:
        return MOCK_PLATFORM

    async def fetch_samples(self, asset_id: UUID) -> Sequence[TelemetrySample]:
        seed = int(asset_id)
        return [
            TelemetrySample(
                metric=metric,
                value=float((seed + index) % modulus),
                sampled_at=_MOCK_EPOCH + timedelta(minutes=index),
                raw={"source": MOCK_PLATFORM, "metric": metric},
            )
            for index, (metric, modulus) in enumerate(_MOCK_METRICS)
        ]


# ---------------------------------------------------------------------------
# The env-swap stubs. One class, five platforms — a vendor's HTTP client is
# NOT in this wave's scope.
# ---------------------------------------------------------------------------


class UnimplementedVendorAdapter(TelemetryAdapter):
    """The "real" adapter for a manufacturer platform, not yet built.

    Selected when ``NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL`` is set. It raises
    rather than returning nothing, because an operator who turns that flag on
    is asking for device truth: silently serving mock numbers, or an empty
    list that reads as "this device reported nothing", would be worse than a
    loud failure. Building the client behind it — auth, ``httpx`` via
    ``nce.http_resilience.request_with_retry``, the vendor's pagination — is a
    later wave per manufacturer.
    """

    __slots__ = ("_platform", "_vendor_api")

    def __init__(self, platform: str, vendor_api: str) -> None:
        self._platform = platform
        self._vendor_api = vendor_api

    @property
    def platform(self) -> str:
        return self._platform

    async def fetch_samples(self, asset_id: UUID) -> Sequence[TelemetrySample]:
        raise NotImplementedError(
            f"telemetry platform {self._platform!r} has no real adapter yet — "
            f"{self._vendor_api} would be called here. Unset "
            f"{real_adapter_env_key(self._platform)} to fall back to the mock adapter."
        )


# ---------------------------------------------------------------------------
# The factory at the edge — the ONLY place a platform key becomes a class.
# ---------------------------------------------------------------------------


def real_adapter_env_key(platform: str) -> str:
    """The env var that flips *platform* from mock to its real adapter."""
    return f"NCE_ASSETS_TELEMETRY_{platform.upper()}_REAL"


def _real_adapter_enabled(platform: str) -> bool:
    """Read the swap flag from the LIVE environment.

    ``live_env_str`` (not a value captured at import) so a runtime change —
    and ``monkeypatch.setenv`` in tests — is honoured, matching
    ``nce.config.live_admin_override_enabled``'s idiom.
    """
    return live_env_str(real_adapter_env_key(platform)).lower() in _TRUTHY


def select_telemetry_adapter(platform: str) -> TelemetryAdapter:
    """Resolve a platform key to an adapter.

    ``mock`` is always the mock. A vendor platform is the mock UNLESS its
    ``NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL`` flag is set, in which case it is
    that vendor's (unbuilt) real adapter — see the module docstring for why
    the flag's default is the mock and why the real one raises.

    Raises
    ------
    ValueError
        *platform* is blank or is not a known platform key. An unknown
        platform is refused rather than defaulted to the mock: a typo'd
        ``crestron`` would otherwise silently serve fabricated numbers.
    """
    name = str(platform or "").strip().lower()
    if not name:
        raise ValueError("do_pull_telemetry: 'platform' is required")
    if name == MOCK_PLATFORM:
        return MockTelemetryAdapter()

    vendor_api = VENDOR_PLATFORMS.get(name)
    if vendor_api is None:
        known = ", ".join(sorted([MOCK_PLATFORM, *VENDOR_PLATFORMS]))
        raise ValueError(f"do_pull_telemetry: unknown telemetry platform {name!r} (known: {known})")

    if _real_adapter_enabled(name):
        return UnimplementedVendorAdapter(name, vendor_api)
    return MockTelemetryAdapter()


# ---------------------------------------------------------------------------
# Parameter and payload coercion — rejected before any DB call.
# ---------------------------------------------------------------------------


def _as_uuid(raw: Any, field_name: str) -> UUID:
    if not raw:
        raise ValueError(f"do_pull_telemetry: '{field_name}' is required")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _validated_samples(samples: Sequence[TelemetrySample]) -> list[TelemetrySample]:
    """Mirror migration 057's ``*_not_blank`` / ``*_value_finite`` CHECKs.

    An adapter is the boundary where a third party's payload enters, so a
    malformed reading becomes a domain error here instead of a raw
    ``asyncpg.CheckViolationError``. This is a MIRROR, not the guard: the DB
    CHECKs stand on their own and are pinned by direct-INSERT tests that never
    call this function.
    """
    validated: list[TelemetrySample] = []
    for sample in samples:
        metric = str(sample.metric or "").strip()
        if not metric:
            raise ValueError("do_pull_telemetry: adapter returned a sample with a blank 'metric'")
        value = float(sample.value)
        if not math.isfinite(value):
            raise ValueError(
                f"do_pull_telemetry: adapter returned a non-finite value for metric {metric!r}"
            )
        validated.append(
            TelemetrySample(
                metric=metric, value=value, sampled_at=sample.sampled_at, raw=sample.raw
            )
        )
    return validated


# ---------------------------------------------------------------------------
# Persistence — two short sessions, the adapter call between them.
# ---------------------------------------------------------------------------


async def _require_asset_in_namespace(engine: NCEEngine, ns_uuid: UUID, asset_id: UUID) -> None:
    """Refuse an asset that is not visible in the caller's namespace.

    The ``namespace_id`` predicate is EXPLICIT, not left to RLS: the owner pool
    used by integration tests bypasses FORCE RLS, and ``asset_id`` alone is a
    perfectly good key there — so without this predicate a caller in one
    namespace could name another tenant's asset and get as far as the INSERT.
    """
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        found = await conn.fetchval(
            "SELECT 1 FROM assets WHERE id = $1::uuid AND namespace_id = $2::uuid",
            str(asset_id),
            str(ns_uuid),
        )
    if found is None:
        raise ValueError(f"do_pull_telemetry: asset {asset_id} is not in namespace {ns_uuid}")


async def _insert_samples(
    engine: NCEEngine,
    ns_uuid: UUID,
    asset_id: UUID,
    samples: Sequence[TelemetrySample],
) -> int:
    """Insert *samples* in one statement; return how many were NEW.

    Set-based rather than a per-sample loop because this is the engine's
    high-write path. ``ON CONFLICT … DO NOTHING`` means ``RETURNING`` yields a
    row only for a genuinely new reading, so the length IS the written count —
    no second query, and no Python "have I seen this?" pre-check that two
    concurrent pulls would both pass.
    """
    if not samples:
        return 0

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            f"""
            INSERT INTO telemetry_samples
                (namespace_id, asset_id, metric, value, sampled_at, raw, change_origin)
            SELECT $1::uuid, $2::uuid, s.metric, s.reading, s.taken_at, s.payload::jsonb, $7
            FROM unnest($3::text[], $4::float8[], $5::timestamptz[], $6::text[])
                AS s(metric, reading, taken_at, payload)
            ON CONFLICT ON CONSTRAINT {_IDEMPOTENCY_CONSTRAINT} DO NOTHING
            RETURNING id
            """,  # the only interpolated value is the module constant above
            str(ns_uuid),
            str(asset_id),
            [s.metric for s in samples],
            [s.value for s in samples],
            [s.sampled_at for s in samples],
            [json.dumps(s.raw, sort_keys=True, default=str) for s in samples],
            _DEFAULT_CHANGE_ORIGIN,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Public: do_pull_telemetry — the SOLE writer of `telemetry_samples`.
# ---------------------------------------------------------------------------


async def do_pull_telemetry(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Pull one asset's telemetry through a :class:`TelemetryAdapter` and store it.

    Writes NOTHING but ``telemetry_samples`` rows: no ``kg_nodes``, no
    ``kg_edges``, no outbox event, no A2A call (module docstring). Idempotent
    by DB constraint — re-pulling an asset whose adapter re-reports the same
    instants adds no rows.

    ``platform`` is a caller-supplied parameter rather than something read off
    the asset. That is a named limitation, not an oversight: the engine doc
    puts ``monitoringPlatform`` on the ``ASSET`` **kg_node**, migration 054's
    ``assets`` table has no such column, and this wave may add neither. A
    later wave that stores the platform can pass it here without changing this
    signature.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "asset_id":     str | UUID,   # required — must exist in this namespace
            "platform":     str,          # optional — defaults to "mock"
        }``

    Returns
    -------
    dict
        ``{"ok": True, "asset_id": str, "platform": str,
        "adapter_platform": str, "pulled": int, "written": int,
        "duplicates": int}``. ``platform`` is what was ASKED for and
        ``adapter_platform`` what actually served it — they differ whenever a
        vendor platform's swap flag is unset and the mock stood in, which is
        the normal state until vendor keys land. ``written`` counts genuinely
        new rows; ``duplicates`` is ``pulled - written``.

    Raises
    ------
    ValueError
        ``namespace_id``/``asset_id`` missing or unparseable, ``platform``
        unknown, the asset is not in this namespace, or the adapter returned a
        blank metric or a non-finite value.
    NotImplementedError
        The requested platform's real adapter is enabled but not built. Let it
        propagate — see :class:`UnimplementedVendorAdapter`.
    """
    ns_uuid = _as_uuid(params.get("namespace_id"), "namespace_id")
    asset_id = _as_uuid(params.get("asset_id"), "asset_id")
    platform = str(params.get("platform") or MOCK_PLATFORM).strip().lower()
    adapter = select_telemetry_adapter(platform)

    await _require_asset_in_namespace(engine, ns_uuid, asset_id)

    # Outside any transaction on purpose (module docstring): a real adapter
    # does network I/O here.
    samples = _validated_samples(await adapter.fetch_samples(asset_id))

    written = await _insert_samples(engine, ns_uuid, asset_id, samples)

    log.debug(
        "do_pull_telemetry asset=%s platform=%s adapter=%s pulled=%d written=%d",
        asset_id,
        platform,
        adapter.platform,
        len(samples),
        written,
    )
    return {
        "ok": True,
        "asset_id": str(asset_id),
        "platform": platform,
        "adapter_platform": adapter.platform,
        "pulled": len(samples),
        "written": written,
        "duplicates": len(samples) - written,
    }
