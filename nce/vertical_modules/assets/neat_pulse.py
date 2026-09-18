"""
nce/vertical_modules/assets/neat_pulse.py
==========================================
Neat Pulse Real Telemetry Adapter (retrofitted MLV16F Wave F-2).

Read-only device + room-sensor telemetry for Neat devices (Neat Bar, Neat
Bar Pro, Neat Board, Neat Pad, Neat Center) over the real Neat Pulse REST
API, with strict timeouts (<5s).

Why this file was rewritten, not patched
-----------------------------------------
The adapter this replaced called a single fabricated endpoint,
``GET {endpoint}/v1/{org}/devices/{asset_id}/telemetry`` — a shape that
does not exist in the real Neat Pulse API (v0.1.1) and had never worked
against a live tenant. Measured against the host's real client for shape
only (Q-25 below): Pulse's real read surface is ``GET /v1/orgs/{org}/...``,
authenticated with a bare API key as a Bearer token (no OAuth2 exchange —
unlike YMCS, Wave F-1), and a device is resolved by its own ``serial``
field on ``GET /endpoints``, never by NCE's own asset id.

No vendor inherits another's allow-list answer (ML-orch, charter §13):
Pulse's own read-only guard is METHOD-shaped (``GET`` allowed on any path;
``POST``/``PATCH``/``DELETE`` allowed on exactly one path, ``/users``, and
refused everywhere else) rather than YMCS's path-enumeration shape. This
adapter needs neither ``/users`` nor any write verb at all, so its
allow-list is simply the two read paths it actually calls.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
Endpoints, the auth shape and response envelopes were read from the
host's ``integrations/neat_client.py`` FOR SHAPE ONLY. No text, comment or
structure from that file was copied, and its ``/users`` write path was
never read into this module — this adapter has no concept of a write call
at all.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_READS`` is the explicit (method, path) allow-list literal.
   ``_gated_request`` refuses any pair outside it BEFORE issuing the HTTP
   call — see ``NeatAllowListRefusal``.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file.
4. Credentials come from ``connectors/save``
   (``nce/admin_handlers/fleet.py:api_admin_connectors_save``) via
   ``live_env_str``, exactly like every sibling adapter — never logged.
5. Tests use ``httpx.MockTransport`` with recorded response shapes; no
   test reaches the network.

Device resolution
------------------
Same gap as Wave F-1, closed the same way: ``TelemetryAdapter`` now
carries the asset's ``serial`` (``nce/vertical_modules/assets/telemetry.py``,
Wave F-1). Pulse's ``GET /endpoints`` response carries a ``serial`` field
per device (the host client's own docstring: the bridge to matching a
device to the room it stands in), so resolution needs no separate lookup
endpoint the way YMCS did.

Field extraction is deliberately generic, not a hand-picked list: the
host client's own doc records that Pulse fields are passed through
*unscanned* ("we don't pick fields — a new field at Neat must not vanish
silently on the way through our own layer"). This adapter follows the
same principle for numeric fields, rather than hard-coding a metric name
list it has no evidence for.
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

log = logging.getLogger("nce.vertical_modules.assets.neat_pulse")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule

#: The read calls this adapter actually issues, independently re-derived
#: for its own needs (Q-25) against the vendor's real ``/endpoints``
#: surface — narrower than the host's full read-only allow-list, which
#: also covers rooms, regions, profiles, notes, firmware and the audit
#: log that this adapter never touches.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/endpoints"),
        ("GET", "/endpoints/{id}/sensor"),
    }
)


class NeatAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list.

    Raised BEFORE any HTTP call — the gate this class exists to prove is
    that the refusal happens whether or not a transport is even wired.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(
            f"Neat Pulse adapter refused {method} {gate_path}: not in its read allow-list"
        )
        self.method = method
        self.path = gate_path


def _templated(path: str, concrete_ids: Sequence[str]) -> str:
    templated = path
    for value in concrete_ids:
        if value:
            templated = templated.replace(f"/{value}", "/{id}", 1)
    return templated


def _as_rows(payload: Any, *envelope_keys: str) -> list[dict[str, Any]]:
    """Pulse wraps a list under one named key that varies by endpoint
    (``{"endpoints": [...]}`` here). Tries each expected key, then falls
    back to the sole list-valued field if there is exactly one — mirrors
    the host client's own ``_liste`` behaviour (an ambiguous envelope with
    two lists yields an empty result, not a guess)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in envelope_keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    candidates = [v for v in payload.values() if isinstance(v, list)]
    if len(candidates) == 1:
        return [r for r in candidates[0] if isinstance(r, dict)]
    return []


def _numeric_samples(
    fields: dict[str, Any], sampled_at: datetime, source: str
) -> list[TelemetrySample]:
    samples: list[TelemetrySample] = []
    for name, value in fields.items():
        if isinstance(value, bool):
            samples.append(
                TelemetrySample(
                    metric=str(name),
                    value=1.0 if value else 0.0,
                    sampled_at=sampled_at,
                    raw={"source": source, "field": name},
                )
            )
        elif isinstance(value, (int, float)):
            samples.append(
                TelemetrySample(
                    metric=str(name),
                    value=float(value),
                    sampled_at=sampled_at,
                    raw={"source": source, "field": name},
                )
            )
    return samples


class NeatPulseTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for Neat Pulse managed hardware."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        api_key: str | None = None,
        org_id: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (endpoint_url or live_env_str("NCE_ASSETS_NEAT_ENDPOINT_URL") or "").strip().rstrip("/")
        )
        self._api_key = api_key or live_env_str("NCE_ASSETS_NEAT_API_KEY")
        self._org_id = org_id or live_env_str("NCE_ASSETS_NEAT_ORG_ID")
        self._timeout = min(timeout, 4.9)
        self._transport = transport

    @property
    def platform(self) -> str:
        return "neat"

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_NEAT_ENDPOINT_URL")
        if not self._api_key:
            missing.append("NCE_ASSETS_NEAT_API_KEY")
        if not self._org_id:
            missing.append("NCE_ASSETS_NEAT_ORG_ID")
        return missing

    async def _gated_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        concrete_ids: Sequence[str] = (),
    ) -> httpx.Response:
        """The one call-site every request in this adapter goes through.

        The allow-list check happens here, before ``client.request`` is
        reached — a call outside ``_ALLOWED_READS`` never becomes network
        traffic. ``path`` is relative to the organisation, matching the
        host client's own convention (``/endpoints``, not the full URL).
        """
        gate_path = _templated(path, concrete_ids)
        if (method, gate_path) not in _ALLOWED_READS:
            raise NeatAllowListRefusal(method, gate_path)
        resp = await client.request(
            method,
            f"{self._endpoint_url}/v1/orgs/{self._org_id}{path}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        return resp

    async def _find_endpoint_by_serial(
        self, client: httpx.AsyncClient, serial: str
    ) -> dict[str, Any] | None:
        resp = await self._gated_request(client, "GET", "/endpoints")
        wanted = serial.strip().lower()
        for row in _as_rows(resp.json(), "endpoints"):
            sn = str(row.get("serial") or "").strip().lower()
            if sn and sn == wanted:
                return row
        return None

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch telemetry readings for the specified asset from the real
        Neat Pulse platform, resolving it by its captured serial number.
        """
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'neat' is unconfigured "
                f"(missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "NeatPulseTelemetryAdapter: asset %s has no captured serial, cannot resolve a "
                "Neat Pulse device",
                asset_id,
            )
            return []

        now = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                endpoint = await self._find_endpoint_by_serial(client, serial)
                if endpoint is None:
                    log.info("NeatPulseTelemetryAdapter: no Neat endpoint with serial %s", serial)
                    return []
                endpoint_id = str(endpoint.get("id") or "")
                if not endpoint_id:
                    return []

                samples: list[TelemetrySample] = []
                connected = endpoint.get("connected")
                if isinstance(connected, bool):
                    samples.append(
                        TelemetrySample(
                            metric="connected",
                            value=1.0 if connected else 0.0,
                            sampled_at=now,
                            raw={"source": "neat_pulse_endpoint", "field": "connected"},
                        )
                    )

                sensor_resp = await self._gated_request(
                    client,
                    "GET",
                    f"/endpoints/{endpoint_id}/sensor",
                    concrete_ids=[endpoint_id],
                )
                sensor_data = sensor_resp.json()
                if isinstance(sensor_data, dict):
                    samples.extend(_numeric_samples(sensor_data, now, "neat_pulse_sensor"))

                return samples
        except Exception as exc:
            log.warning(
                "NeatPulseTelemetryAdapter: query failed for asset %s (serial %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="neat_telemetry_unavailable",
                    detail=(
                        f"Neat Pulse telemetry query for serial {serial} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint=(
                        "Verify Neat Pulse endpoint, org id and API key via connectors/save."
                    ),
                )
            except Exception:
                pass
            return []
