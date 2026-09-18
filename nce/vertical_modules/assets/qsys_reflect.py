"""
nce/vertical_modules/assets/qsys_reflect.py
============================================
Q-SYS Reflect Enterprise Manager Real Telemetry Adapter
(retrofitted MLV16F Wave F-6).

Provides real read-only device telemetry for QSC / Q-SYS hardware (Core
110f, Core Nano, Core 610, NV-32-H, touch screens, Q-SYS amplifiers) over
the real Q-SYS Reflect public API, with strict timeouts (<5s).

Why this file was rewritten, not patched
-----------------------------------------
The adapter this replaced called a single fabricated endpoint,
``GET {endpoint}/api/v0/cores/{asset_id}/telemetry`` — a shape that does
not exist in the real Reflect API and had never worked against a live
tenant. Found by Lane H's advertised-vs-implemented ratchet, which
initially flagged this as a possible fourth instance of Q-38 (the three
orphan adapters with no host client at all) — it is not: unlike
Crestron/Sennheiser/Shure, Q-SYS has a real host client
(``integrations/qsys_reflect_client.py``) and its own test fixtures
(``tests/test_qsys_reflect.py``), so this is an F-1-style retrofit against
a real reference, not a build against nothing.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
Endpoints and response shapes were read from the host's
``integrations/qsys_reflect_client.py`` FOR SHAPE ONLY, and field names
were cross-checked against that repo's own test fixtures
(``tests/test_qsys_reflect.py``) — a primary source, not a guess. No
text, comment or structure from either file was copied.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_READS`` is the explicit (method, path) allow-list literal.
   ``_gated_request`` refuses any pair outside it BEFORE issuing the HTTP
   call.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file, and every allow-listed
   read is a genuine ``GET`` — no path-semantics comment needed (unlike
   Wave F-1's YMCS).
4. The bearer token (and the endpoint URL) come from ``connectors/save``
   via ``live_env_str``, never logged.
5. Tests use ``httpx.MockTransport``; no test reaches the network.

Device resolution — a two-tier search, both tiers verified
------------------------------------------------------------
Reflect manages a hierarchy: Cores (the DSP hardware) -> Systems (a
design running on a Core) -> Items (components attached to a system —
mics, amplifiers, touch panels). Verified against the host's own test
fixtures: a core row's ``serial`` field is REFLECT'S OWN internal
identifier (e.g. ``"reflect-id"``), NOT the physical serial number — the
actual physical serial lives in a SEPARATE ``serialNumber`` field (e.g.
``"SN-1001"``), which is the field this adapter matches against
``assets.serial``. The same distinction holds for items. This is exactly
the kind of field-name trap Q-25's "read for shape, verify against
fixtures" rule exists to catch — the two fields are one letter apart in
meaning but carry entirely different values.

An asset may be either a Core or an Item; this adapter checks ``cores``
first, then walks ``systems`` -> ``systems/{id}/items`` if no Core
matches. ``status.code`` (an int; verified: ``0`` = OK, ``3`` = Fault) and
an item's ``isOnline`` (a bool) are numeric samples; ``status.message``
and ``redundancy.role``/``redundancy.state`` are strings and are skipped
rather than invented into numbers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from nce.config import live_env_str
from nce.vertical_modules.assets.telemetry import TelemetryAdapter, TelemetrySample

log = logging.getLogger("nce.vertical_modules.assets.qsys_reflect")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule

#: The read calls this adapter actually issues, relative to the
#: configured endpoint (the real API's own base already ends in
#: ``/api/public/v0/``). Independently re-derived for this adapter's
#: needs (Q-25) — the host's own client also reads nothing beyond these
#: three, which is the whole read surface this adapter needs.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "cores"),
        ("GET", "systems"),
        ("GET", "systems/{id}/items"),
    }
)


class QSysAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(
            f"Q-SYS Reflect adapter refused {method} {gate_path}: not in its read allow-list"
        )
        self.method = method
        self.path = gate_path


def _templated(path: str, concrete_ids: Sequence[str]) -> str:
    templated = path
    for value in concrete_ids:
        if value:
            templated = templated.replace(f"/{value}", "/{id}", 1)
    return templated


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """Reflect's list endpoints return a bare, unpaginated JSON array
    (verified: the host client's own ``_liste`` asserts ``isinstance(data,
    list)``) — tolerate an envelope anyway rather than assume the shape
    never changes."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _samples_from_status_bearing_row(row: dict[str, Any], now: datetime) -> list[TelemetrySample]:
    samples: list[TelemetrySample] = []
    status = row.get("status") if isinstance(row.get("status"), dict) else {}
    code = status.get("code")
    if isinstance(code, (int, float)) and not isinstance(code, bool):
        samples.append(
            TelemetrySample(
                metric="status.code",
                value=float(code),
                sampled_at=now,
                raw={"source": "qsys_reflect", "field": "status.code"},
            )
        )
    is_online = row.get("isOnline")
    if isinstance(is_online, bool):
        samples.append(
            TelemetrySample(
                metric="isOnline",
                value=1.0 if is_online else 0.0,
                sampled_at=now,
                raw={"source": "qsys_reflect", "field": "isOnline"},
            )
        )
    return samples


class QSysReflectTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for Q-SYS Reflect managed hardware."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (endpoint_url or live_env_str("NCE_ASSETS_QSYS_ENDPOINT_URL") or "").strip().rstrip("/")
        )
        self._api_key = api_key or live_env_str("NCE_ASSETS_QSYS_API_KEY")
        self._timeout = min(timeout, 4.9)
        self._transport = transport

    @property
    def platform(self) -> str:
        return "qsys"

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_QSYS_ENDPOINT_URL")
        if not self._api_key:
            missing.append("NCE_ASSETS_QSYS_API_KEY")
        return missing

    async def _gated_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        concrete_ids: Sequence[str] = (),
    ) -> httpx.Response:
        gate_path = _templated(path, concrete_ids)
        if (method, gate_path) not in _ALLOWED_READS:
            raise QSysAllowListRefusal(method, gate_path)
        resp = await client.request(
            method,
            f"{self._endpoint_url}/{path}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        return resp

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch telemetry for the specified asset from Q-SYS Reflect,
        resolving it by matching its captured serial against a Core's or
        an Item's ``serialNumber`` (NOT the ``serial`` field, which is
        Reflect's own internal id — see the module docstring)."""
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'qsys' is unconfigured "
                f"(missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "QSysReflectTelemetryAdapter: asset %s has no captured serial, cannot resolve "
                "a Q-SYS Reflect device",
                asset_id,
            )
            return []

        now = datetime.now(timezone.utc)
        wanted = serial.strip().lower()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                cores_resp = await self._gated_request(client, "GET", "cores")
                cores = _as_rows(cores_resp.json())
                for core in cores:
                    sn = str(core.get("serialNumber") or "").strip().lower()
                    if sn and sn == wanted:
                        return _samples_from_status_bearing_row(core, now)

                systems_resp = await self._gated_request(client, "GET", "systems")
                systems = _as_rows(systems_resp.json())
                for system in systems:
                    system_id = system.get("id")
                    if system_id is None:
                        continue
                    items_resp = await self._gated_request(
                        client,
                        "GET",
                        f"systems/{system_id}/items",
                        concrete_ids=[str(system_id)],
                    )
                    for item in _as_rows(items_resp.json()):
                        sn = str(item.get("serialNumber") or "").strip().lower()
                        if sn and sn == wanted:
                            return _samples_from_status_bearing_row(item, now)

                log.info(
                    "QSysReflectTelemetryAdapter: no core or item with serialNumber %s", serial
                )
                return []
        except Exception as exc:
            log.warning(
                "QSysReflectTelemetryAdapter: query failed for asset %s (serial %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="qsys_telemetry_unavailable",
                    detail=(
                        f"Q-SYS Reflect telemetry query for serial {serial} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint="Verify Q-SYS Reflect endpoint and bearer token via connectors/save.",
                )
            except Exception:
                pass
            return []
