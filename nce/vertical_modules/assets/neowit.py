"""
nce/vertical_modules/assets/neowit.py
========================================
Neowit smart-building telemetry adapter (new platform, MLV16F Wave F-3).

Read-only device telemetry from the Neowit smart-building platform, which
re-exposes sensors from underlying vendors (Disruptive, Neat, Airthings)
under its own building/floor/room ("space") tree, over Neowit's real REST
API, with strict timeouts (<5s).

No vendor inherits another's answer (ML-orch, charter §13) — Neowit's
shape differs from both prior waves:
- Auth is OAuth2 JWT-bearer: a short-lived RS256-signed assertion,
  exchanged for an access token at ``POST /auth/oauth/token`` (a private
  key, not a shared secret — different credential shape from F-1's
  client-secret and F-2's bare API key).
- The host's read-only guard here is method-shaped like F-2's (``GET``
  allowed everywhere) rather than F-1's path-enumeration shape, but with
  no writable exception at all — Neowit's service-account credential is
  documented as carrying REAL write access to the customer's production
  environment (``writeSpaceEnabled: true``), so the host's own client
  blocks every verb but ``GET`` unconditionally.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
Endpoints, the auth flow and response envelope names were read from the
host's ``integrations/neowit_client.py`` FOR SHAPE ONLY. No text, comment
or structure from that file was copied.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_AUTH`` / ``_ALLOWED_READS`` are the explicit (method, path)
   allow-list literal. ``_gated_request`` refuses any pair outside their
   union BEFORE issuing the HTTP call.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file, and unlike F-1 there is
   no read-shaped POST to name here either — every allow-listed read is a
   genuine ``GET``.
4. Credentials (endpoint, account id, key id, and the RSA private key
   itself) come from ``connectors/save`` via ``live_env_str``, never
   logged. The private key is used only to sign a JWT locally; it is
   never sent over the wire.
5. Tests use ``httpx.MockTransport``; no test reaches the network.

Device resolution — an honest gap, not a guess
--------------------------------------------------
Unlike F-1 (YMCS: ``sn``) and F-2 (Neat: ``serial``), nothing in the host
client or its tests documents a device-level serial-number field for
Neowit — the one identity bridge it documents is ``externalId`` ("the id
bridge to the source under Neowit"). This adapter therefore matches the
asset's ``serial`` against EITHER an ``externalId`` or a ``serial``-shaped
field on the device row, rather than assuming one field name it has no
evidence for. If neither is present the device cannot be resolved and the
pull is skipped — see the module's PR for why this is flagged as an
inference, not a verified shape.

Telemetry window, not a single "latest" call
----------------------------------------------
Neowit's sensor endpoint (``/series/v1/device/{id}``) is a time-series
range query, not a "give me the latest reading" call — there is no
narrower endpoint. ``_SERIES_WINDOW_SECONDS`` covers a window comfortably
wider than the 5-minute cron tick (``nce/cron.py``'s
``_assets_telemetry_tick``), so a slow tick or a clock skew does not miss
a reading; the resulting overlap is exactly what
``telemetry_samples_idempotency_uq`` (migration 057) exists to absorb.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import jwt

from nce.config import live_env_str
from nce.vertical_modules.assets.telemetry import TelemetryAdapter, TelemetrySample

log = logging.getLogger("nce.vertical_modules.assets.neowit")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule
_ASSERTION_LIFETIME_S = 600
_SERIES_WINDOW_SECONDS = 30 * 60  # comfortably wider than the 5-minute cron tick

#: The one auth route this adapter may call, labelled apart from the
#: reads (same convention as F-1's ruling): an allow-list that does not
#: name the call carrying the signed assertion constrains nothing about
#: how it is used.
_ALLOWED_AUTH: frozenset[tuple[str, str]] = frozenset({("POST", "/auth/oauth/token")})

#: The read calls this adapter actually issues. Independently re-derived
#: for THIS adapter's needs (Q-25) — narrower than the host's own full
#: read-only surface, which also covers spaces, bookings and space
#: insights that this adapter never touches.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/device/v1/device"),
        ("GET", "/series/v1/device/{id}"),
    }
)


class NeowitAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(
            f"Neowit adapter refused {method} {gate_path}: not in its read/auth allow-list"
        )
        self.method = method
        self.path = gate_path


def _templated(path: str, concrete_ids: Sequence[str]) -> str:
    templated = path
    for value in concrete_ids:
        if value:
            templated = templated.replace(f"/{value}", "/{id}", 1)
    return templated


def _as_rows(payload: Any, envelope_key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        value = payload.get(envelope_key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _sensor_label(descriptor: Any, index: int) -> str:
    """A metric name for one column of a ``device_series`` row.

    The exact shape of a ``sensors[i]`` descriptor is not evidenced by the
    host client or its tests (module docstring) — this tries the field
    names a descriptor of that kind would plausibly carry, falling back to
    a positional name rather than guessing at one specific shape.
    """
    if isinstance(descriptor, str) and descriptor.strip():
        return descriptor.strip()
    if isinstance(descriptor, dict):
        for key in ("type", "sensorType", "name", "id"):
            value = descriptor.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"sensor_{index}"


class NeowitTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for devices under Neowit."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        account_id: str | None = None,
        key_id: str | None = None,
        private_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (endpoint_url or live_env_str("NCE_ASSETS_NEOWIT_ENDPOINT_URL") or "")
            .strip()
            .rstrip("/")
        )
        self._account_id = account_id or live_env_str("NCE_ASSETS_NEOWIT_ACCOUNT_ID")
        self._key_id = key_id or live_env_str("NCE_ASSETS_NEOWIT_KEY_ID")
        self._private_key = private_key or live_env_str("NCE_ASSETS_NEOWIT_PRIVATE_KEY")
        self._timeout = min(timeout, 4.9)
        self._transport = transport

    @property
    def platform(self) -> str:
        return "neowit"

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_NEOWIT_ENDPOINT_URL")
        if not self._account_id:
            missing.append("NCE_ASSETS_NEOWIT_ACCOUNT_ID")
        if not self._key_id:
            missing.append("NCE_ASSETS_NEOWIT_KEY_ID")
        if not self._private_key:
            missing.append("NCE_ASSETS_NEOWIT_PRIVATE_KEY")
        return missing

    async def _gated_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        allowed: frozenset[tuple[str, str]],
        concrete_ids: Sequence[str] = (),
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        gate_path = _templated(path, concrete_ids)
        if (method, gate_path) not in allowed:
            raise NeowitAllowListRefusal(method, gate_path)
        resp = await client.request(
            method, f"{self._endpoint_url}{path}", headers=headers, params=params, data=data
        )
        resp.raise_for_status()
        return resp

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        """Sign a short-lived RS256 assertion locally and exchange it.

        The private key never leaves this process — it is used only to
        compute ``jwt.encode``'s signature, never sent over the wire, and
        never passed to ``log``.
        """
        issued = int(time.time())
        token_url = f"{self._endpoint_url}/auth/oauth/token"
        assertion = jwt.encode(
            {
                "iat": issued,
                "exp": issued + _ASSERTION_LIFETIME_S,
                "aud": token_url,
                "iss": self._account_id,
                "sub": self._account_id,
                "jti": f"{self._account_id}-{issued}",
            },
            self._private_key,
            algorithm="RS256",
            headers={"alg": "RS256", "kid": self._key_id},
        )
        resp = await self._gated_request(
            client,
            "POST",
            "/auth/oauth/token",
            allowed=_ALLOWED_AUTH,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "assertion": assertion,
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            },
        )
        data_payload = resp.json()
        token = data_payload.get("access_token")
        if not token:
            raise RuntimeError("Neowit token response carried no access_token")
        return str(token)

    async def _find_device_by_serial(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], serial: str
    ) -> dict[str, Any] | None:
        resp = await self._gated_request(
            client, "GET", "/device/v1/device", allowed=_ALLOWED_READS, headers=auth_headers
        )
        wanted = serial.strip().lower()
        for row in _as_rows(resp.json(), "devices"):
            for key in ("externalId", "serial", "serialNumber"):
                value = str(row.get(key) or "").strip().lower()
                if value and value == wanted:
                    return row
        return None

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch telemetry readings for the specified asset from Neowit,
        resolving it by its captured serial (matched against Neowit's own
        ``externalId``/``serial``-shaped device fields)."""
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'neowit' is unconfigured "
                f"(missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "NeowitTelemetryAdapter: asset %s has no captured serial, cannot resolve a "
                "Neowit device",
                asset_id,
            )
            return []

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                token = await self._access_token(client)
                auth_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

                device = await self._find_device_by_serial(client, auth_headers, serial)
                if device is None:
                    log.info("NeowitTelemetryAdapter: no Neowit device matching serial %s", serial)
                    return []
                device_id = str(device.get("id") or "")
                if not device_id:
                    return []

                now = int(time.time())
                series_resp = await self._gated_request(
                    client,
                    "GET",
                    f"/series/v1/device/{device_id}",
                    allowed=_ALLOWED_READS,
                    concrete_ids=[device_id],
                    headers=auth_headers,
                    params={"start": now - _SERIES_WINDOW_SECONDS, "end": now},
                )
                return _samples_from_series(series_resp.json())
        except Exception as exc:
            log.warning(
                "NeowitTelemetryAdapter: query failed for asset %s (serial %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="neowit_telemetry_unavailable",
                    detail=(
                        f"Neowit telemetry query for serial {serial} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint=(
                        "Verify Neowit endpoint, account/key id and private key via "
                        "connectors/save."
                    ),
                )
            except Exception:
                pass
            return []


def _samples_from_series(payload: dict[str, Any]) -> list[TelemetrySample]:
    """``{"sensors": [...], "rows": [{"time": ISO, "values": [...]}, ...]}``
    — one column per sensor, one row per instant. Emits one
    ``TelemetrySample`` per (sensor, row), which is the natural shape for
    a range query; ``telemetry_samples_idempotency_uq`` absorbs the
    overlap between successive pulls' windows."""
    if not isinstance(payload, dict):
        return []
    sensors = payload.get("sensors")
    rows = payload.get("rows")
    if not isinstance(sensors, list) or not isinstance(rows, list):
        return []

    samples: list[TelemetrySample] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        time_str = row.get("time")
        values = row.get("values")
        if not isinstance(time_str, str) or not isinstance(values, list):
            continue
        try:
            sampled_at = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=timezone.utc)
        for index, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            sensor_descriptor = sensors[index] if index < len(sensors) else None
            metric = _sensor_label(sensor_descriptor, index)
            samples.append(
                TelemetrySample(
                    metric=metric,
                    value=float(value),
                    sampled_at=sampled_at,
                    raw={"source": "neowit_device_series", "sensor_index": index},
                )
            )
    return samples
