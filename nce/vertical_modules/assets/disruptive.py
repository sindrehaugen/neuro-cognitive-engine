"""
nce/vertical_modules/assets/disruptive.py
============================================
Disruptive Technologies telemetry adapter (new platform, MLV16F Wave F-4).

Read-only device telemetry from Disruptive Technologies (DT) wireless
sensors (temperature, CO2, humidity, desk/room occupancy), over the real
DT REST API, with strict timeouts (<5s).

No vendor inherits another's answer (ML-orch, charter §13) — DT's shape
differs from all three prior waves again:
- Auth is OAuth2 JWT-bearer like Neowit (Wave F-3), but HS256 with a
  shared secret, not RS256 with a private key — a fourth distinct
  credential shape in four waves (OAuth2 client-credentials, bare API-key
  bearer, RS256 JWT-bearer, now HS256 JWT-bearer).
- The token endpoint lives on a DIFFERENT HOST from the data API
  (``identity.disruptive-technologies.com`` vs the project API host) —
  the first wave in this lane where auth and data are not even the same
  origin.
- One call, ``GET /devices``, returns each device's LATEST reading of
  every event type it reports (a ``reported`` object) — no separate
  "get current telemetry" or "resolve then fetch" round trip is needed at
  all, unlike every prior wave.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
Endpoints, the auth flow and response shapes were read from the host's
``integrations/disruptive_client.py`` FOR SHAPE ONLY, and field shapes
were cross-checked against that repo's own test fixtures
(``tests/test_disruptive_sensorer.py``) — a primary source, not a guess.
No text, comment or structure from either file was copied.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_READS`` (plus the one dedicated, explicitly-checked token
   URL) is the explicit allow-list. ``_gated_request`` refuses any path
   outside it BEFORE issuing the HTTP call.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file, and every allow-listed
   read is a genuine ``GET`` — no path-semantics comment needed.
4. Credentials (endpoint, token URL, project id, service-account email,
   key id, HS256 secret) come from ``connectors/save`` via
   ``live_env_str``, never logged. The secret is used only to sign a JWT
   locally; it is never sent over the wire.
5. Tests use ``httpx.MockTransport``; no test reaches the network.

Device resolution — an honest gap, same shape as Wave F-3's
----------------------------------------------------------------
Verified against the host's own test fixtures: a DT device row is
``{"name": "projects/{p}/devices/{id}", "type": ..., "labels": {...},
"reported": {...}}``. There is no serial-number field anywhere in this
shape — device identity is DT's own opaque id (the last path segment of
``name``), and ``labels`` is a free-form, installer-defined map (commonly
used for a human-readable room name, per the fixture: ``labels.name`` =
"Møterom 3"). This adapter therefore matches the asset's ``serial``
against, in order: a ``serial``/``sn``-shaped key inside ``labels``, or
the raw DT device id extracted from ``name``. If a live tenant's install
records neither in a matchable form, this adapter resolves nothing —
flagged as an inference in the PR body, not presented as verified.

Metric extraction follows the vendor's own per-type sub-object shape
(verified: ``reported.temperature.value``, ``reported.co2.ppm``,
``reported.humidity.relativeHumidity``, ``reported.batteryStatus.percentage``,
``reported.networkStatus.signalStrength``) rather than a single flat
"metrics" dict — a non-numeric reading (``reported.deskOccupancy.state``
is a string, e.g. ``"NOT_OCCUPIED"``) is skipped rather than invented into
a number.
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

log = logging.getLogger("nce.vertical_modules.assets.disruptive")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule
_ASSERTION_LIFETIME_S = 3600
_PAGE_SIZE = 100
_MAX_PAGES = 100

#: The read calls this adapter actually issues, relative to
#: ``{endpoint}/projects/{project_id}``. Independently re-derived for this
#: adapter's needs (Q-25) — narrower than the host's own read-only
#: surface, which also covers the project resource itself and data
#: connectors that this adapter never touches.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", "/devices")})


class DisruptiveAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list, or a
    call was attempted to a token URL other than the configured one.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(
            f"Disruptive adapter refused {method} {gate_path}: not in its read allow-list"
        )
        self.method = method
        self.path = gate_path


def _as_rows(payload: Any, envelope_key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        value = payload.get(envelope_key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _device_id_from_name(name: str) -> str:
    """``projects/{p}/devices/{id}`` -> ``{id}`` (verified shape, host
    fixtures). Returns "" if the resource-path shape does not match rather
    than guessing at a truncation."""
    parts = name.split("/")
    if len(parts) == 4 and parts[0] == "projects" and parts[2] == "devices":
        return parts[3]
    return ""


def _latest_update_time(reported: dict[str, Any]) -> datetime | None:
    latest: datetime | None = None
    for sub in reported.values():
        if not isinstance(sub, dict):
            continue
        raw = sub.get("updateTime")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _samples_from_device(row: dict[str, Any], now: datetime) -> list[TelemetrySample]:
    reported = row.get("reported")
    if not isinstance(reported, dict):
        return []
    fallback_time = _latest_update_time(reported) or now

    samples: list[TelemetrySample] = []
    for event_type, sub in reported.items():
        if not isinstance(sub, dict):
            continue
        raw_time = sub.get("updateTime")
        sampled_at = fallback_time
        if isinstance(raw_time, str):
            try:
                sampled_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                sampled_at = fallback_time
        if sampled_at.tzinfo is None:
            sampled_at = sampled_at.replace(tzinfo=timezone.utc)

        for field_name, value in sub.items():
            if field_name == "updateTime":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue  # e.g. deskOccupancy.state is a string — skipped, never invented
            samples.append(
                TelemetrySample(
                    metric=f"{event_type}.{field_name}" if field_name != event_type else event_type,
                    value=float(value),
                    sampled_at=sampled_at,
                    raw={"source": "disruptive_devices", "event_type": event_type},
                )
            )
    return samples


class DisruptiveTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for Disruptive Technologies sensors."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        token_url: str | None = None,
        project_id: str | None = None,
        service_account_email: str | None = None,
        key_id: str | None = None,
        secret: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (endpoint_url or live_env_str("NCE_ASSETS_DISRUPTIVE_ENDPOINT_URL") or "")
            .strip()
            .rstrip("/")
        )
        self._token_url = (
            token_url or live_env_str("NCE_ASSETS_DISRUPTIVE_TOKEN_URL") or ""
        ).strip()
        self._project_id = project_id or live_env_str("NCE_ASSETS_DISRUPTIVE_PROJECT_ID")
        self._service_account_email = service_account_email or live_env_str(
            "NCE_ASSETS_DISRUPTIVE_SERVICE_ACCOUNT_EMAIL"
        )
        self._key_id = key_id or live_env_str("NCE_ASSETS_DISRUPTIVE_KEY_ID")
        self._secret = secret or live_env_str("NCE_ASSETS_DISRUPTIVE_SECRET")
        self._timeout = min(timeout, 4.9)
        self._transport = transport

    @property
    def platform(self) -> str:
        return "disruptive"

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_DISRUPTIVE_ENDPOINT_URL")
        if not self._token_url:
            missing.append("NCE_ASSETS_DISRUPTIVE_TOKEN_URL")
        if not self._project_id:
            missing.append("NCE_ASSETS_DISRUPTIVE_PROJECT_ID")
        if not self._service_account_email:
            missing.append("NCE_ASSETS_DISRUPTIVE_SERVICE_ACCOUNT_EMAIL")
        if not self._key_id:
            missing.append("NCE_ASSETS_DISRUPTIVE_KEY_ID")
        if not self._secret:
            missing.append("NCE_ASSETS_DISRUPTIVE_SECRET")
        return missing

    async def _gated_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """The one call-site every DATA request in this adapter goes
        through (the token exchange is a separate, dedicated method with
        its own explicit check — it targets a different host entirely).
        """
        if (method, path) not in _ALLOWED_READS:
            raise DisruptiveAllowListRefusal(method, path)
        resp = await client.request(
            method,
            f"{self._endpoint_url}/projects/{self._project_id}{path}",
            headers=headers,
            params={"pageSize": _PAGE_SIZE, **(params or {})},
        )
        resp.raise_for_status()
        return resp

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        """Sign a short-lived HS256 assertion and exchange it at the
        configured token URL — a different host from the data API, so
        this does not go through ``_gated_request``. The check here is
        explicit rather than implicit: the assertion (and the secret it
        proves) is sent ONLY to the URL the operator configured, never
        somewhere derived or guessed.
        """
        if not self._token_url:
            raise DisruptiveAllowListRefusal("POST", "<unconfigured token url>")
        issued = int(time.time())
        assertion = jwt.encode(
            {
                "iat": issued,
                "exp": issued + _ASSERTION_LIFETIME_S,
                "aud": self._token_url,
                "iss": self._service_account_email,
            },
            self._secret,
            algorithm="HS256",
            headers={"alg": "HS256", "kid": self._key_id},
        )
        resp = await client.post(
            self._token_url,
            data={
                "assertion": assertion,
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Disruptive token response carried no access_token")
        return str(token)

    async def _find_device_by_serial(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], serial: str
    ) -> dict[str, Any] | None:
        wanted = serial.strip().lower()
        page_token = ""
        for _ in range(_MAX_PAGES):
            params = {"pageToken": page_token} if page_token else None
            resp = await self._gated_request(
                client, "GET", "/devices", headers=auth_headers, params=params
            )
            payload = resp.json()
            rows = _as_rows(payload, "devices")
            for row in rows:
                labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
                for key, value in labels.items():
                    if (
                        str(key).strip().lower() in ("serial", "sn", "serialnumber")
                        and str(value or "").strip().lower() == wanted
                    ):
                        return row
                name = str(row.get("name") or "")
                device_id = _device_id_from_name(name)
                if device_id and device_id.strip().lower() == wanted:
                    return row
            page_token = str((payload or {}).get("nextPageToken") or "")
            if not page_token:
                return None
        log.warning("DisruptiveTelemetryAdapter: /devices exceeded %d pages, gave up", _MAX_PAGES)
        return None

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch the latest telemetry for the specified asset from
        Disruptive, resolving it by matching its captured serial against
        either a labelled serial or DT's own device id."""
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'disruptive' is unconfigured "
                f"(missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "DisruptiveTelemetryAdapter: asset %s has no captured serial, cannot resolve a "
                "Disruptive device",
                asset_id,
            )
            return []

        now = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                token = await self._access_token(client)
                auth_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

                device = await self._find_device_by_serial(client, auth_headers, serial)
                if device is None:
                    log.info("DisruptiveTelemetryAdapter: no device matching serial %s", serial)
                    return []
                return _samples_from_device(device, now)
        except Exception as exc:
            log.warning(
                "DisruptiveTelemetryAdapter: query failed for asset %s (serial %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="disruptive_telemetry_unavailable",
                    detail=(
                        f"Disruptive telemetry query for serial {serial} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint=(
                        "Verify Disruptive endpoint/token URL, project id and service-account "
                        "credentials via connectors/save."
                    ),
                )
            except Exception:
                pass
            return []
