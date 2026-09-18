"""
nce/vertical_modules/assets/ymcs.py
====================================
Yealink Management Cloud Service (YMCS) Real Telemetry Adapter
(Module 9, Telemetry Integration; retrofitted MLV16F Wave F-1).

Provides real read-only device + room-sensor telemetry for Yealink UC
hardware (MeetingBar A-series, MVC-series Microsoft Teams Rooms,
MeetingBoard, RoomPanel) over the real YMCS v2 REST API, with strict
timeouts (<5s).

Why this file was rewritten, not patched
-----------------------------------------
The adapter this replaced called a single fabricated endpoint,
``GET {endpoint}/api/v1/devices/{asset_id}/telemetry`` with a bare Bearer
token — a shape that does not exist in the real YMCS API and had never
worked against a live tenant (``d26b860`` removed the synthetic fallback
that had been masking this, so it failed rather than fabricated, but
nothing had verified it against the real service). Measured against the
host's real client for shape only (Q-25 below): YMCS is OAuth2
client-credentials over a ``/v2/dm/...`` REST surface, and devices are
looked up by serial number, never by NCE's own asset id.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
Endpoints, the auth flow and response shapes were read from the host's
``integrations/ymcs_client.py`` FOR SHAPE ONLY. No text, comment or
structure from that file was copied, and its write-path endpoint set
(``SKRIVEVEIENE``) was never read into this module — this adapter has no
concept of a write call at all. The allow-list below was independently
derived for what THIS adapter needs (device resolution + telemetry reads),
which is narrower than the host's own broader read allow-list.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_AUTH`` / ``_ALLOWED_READS`` are the explicit (method, path)
   allow-list literal. ``_request`` refuses any pair outside their union
   BEFORE issuing the HTTP call — see ``YmcsAllowListRefusal``.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file — there is no ``_skriv``
   equivalent, and the allow-list contains only ``GET``/``POST`` reads plus
   the one auth ``POST``.
4. Credentials come from ``connectors/save``
   (``nce/admin_handlers/fleet.py:api_admin_connectors_save``) via
   ``live_env_str``, exactly like every sibling adapter in this module —
   never logged (see ``_access_token``: the secret is used to build a
   header and never appears in a log call).
5. Tests use ``httpx.MockTransport`` with recorded response shapes; no
   test reaches the network.

Device resolution — the gap this retrofit closes
------------------------------------------------------
``TelemetryAdapter.fetch_samples`` previously received only NCE's own
``asset_id``, which is not a vendor identifier for any manufacturer. YMCS
keys a device by its serial number (assigned when the device is added to
the enterprise). ``nce/vertical_modules/assets/telemetry.py`` was widened
(same wave) to read ``assets.serial`` — a column that already existed,
so no migration — and pass it here as ``serial``. An asset with no
captured serial cannot be resolved against YMCS and is skipped rather
than guessed at.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from nce.config import live_env_str
from nce.vertical_modules.assets.telemetry import TelemetryAdapter, TelemetrySample

log = logging.getLogger("nce.vertical_modules.assets.ymcs")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule
_LIST_PAGE_SIZE = 100
_LIST_MAX_PAGES = 20  # a fleet larger than 2000 devices needs a narrower query, not a raised cap

#: The one auth route this adapter may call, labelled apart from the reads
#: (ML-orch ruling on MLV16F-F1(e), charter §13 GO-F entry): an allow-list
#: that does not name the call carrying the credentials constrains nothing
#: about how they are used, so it gets its own named set rather than living
#: inside "reads".
_ALLOWED_AUTH: frozenset[tuple[str, str]] = frozenset({("POST", "/v2/token")})

#: The read calls this adapter actually issues. Independently re-derived
#: for THIS adapter's needs (Q-25) — narrower than the host's own broader
#: read allow-list, which also covers sites, firmware, accounts, groups,
#: QoE and alarms that this adapter never touches.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v2/dm/listDevices"),
        ("GET", "/v2/dm/devices/{id}"),
        ("POST", "/v2/dm/devices/{id}/listParts"),
        ("GET", "/v2/dm/devices/{id}/parts/{id}"),
    }
)


class YmcsAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list.

    Raised BEFORE any HTTP call — the gate this class exists to prove is
    that the refusal happens whether or not a transport is even wired.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(
            f"YMCS adapter refused {method} {gate_path}: not in its read/auth allow-list"
        )
        self.method = method
        self.path = gate_path


def _templated(path: str, concrete_ids: Sequence[str]) -> str:
    """Replace each concrete id segment in *path* with the allow-list's
    literal ``{id}`` placeholder, so a real request path (which always
    carries a real device/part id) can be checked against the fixed
    literal entries above without the allow-list growing one entry per id.
    """
    templated = path
    for value in concrete_ids:
        if value:
            templated = templated.replace(f"/{value}", "/{id}", 1)
    return templated


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """YMCS list endpoints wrap rows in ``{"data": [...]}``; tolerate a bare
    list too rather than assume one envelope shape everywhere (the host's
    own client notes ``/v2/dm/models`` is the one endpoint that does not
    wrap its payload — this adapter does not call that one, but the same
    defensive read costs nothing here)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
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


def _samples_from_device_detail(payload: dict[str, Any], now: datetime) -> list[TelemetrySample]:
    """``GET /v2/dm/devices/{id}?select=sensor,wifi`` — WiFi signal strength
    lives directly on the device; a room system's own environmental
    readings live one level down, on its bound "part" (see
    ``_samples_from_part_detail``)."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    samples: list[TelemetrySample] = []
    wifi = data.get("wifi") if isinstance(data.get("wifi"), dict) else {}
    samples.extend(_numeric_samples(wifi, now, "ymcs_device_wifi"))
    sensor = data.get("sensor") if isinstance(data.get("sensor"), dict) else {}
    samples.extend(_numeric_samples(sensor, now, "ymcs_device_sensor"))
    return samples


def _samples_from_part_detail(payload: dict[str, Any], now: datetime) -> list[TelemetrySample]:
    """A device's "part" (the room sensor accessory) carries its own
    readings in ``extraInfo`` — motion state, irradiance (light level),
    battery level; humidity travels the same way where the model reports
    it. Verified field names, not guessed: ``motionState``, ``irradiance``,
    ``batteryLevel``."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    extra = data.get("extraInfo") if isinstance(data.get("extraInfo"), dict) else {}
    return _numeric_samples(extra, now, "ymcs_part_sensor")


class YMCSTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for Yealink YMCS / UC devices."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        platform_name: str = "ymcs",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (
                endpoint_url
                or live_env_str("NCE_ASSETS_YMCS_ENDPOINT_URL")
                or live_env_str("NCE_ASSETS_YEALINK_ENDPOINT_URL")
                or ""
            )
            .strip()
            .rstrip("/")
        )
        self._client_id = (
            client_id
            or live_env_str("NCE_ASSETS_YMCS_CLIENT_ID")
            or live_env_str("NCE_ASSETS_YEALINK_CLIENT_ID")
        )
        self._client_secret = (
            client_secret
            or live_env_str("NCE_ASSETS_YMCS_CLIENT_SECRET")
            or live_env_str("NCE_ASSETS_YEALINK_CLIENT_SECRET")
        )
        self._timeout = min(timeout, 4.9)
        self._platform_name = platform_name
        self._transport = transport

    @property
    def platform(self) -> str:
        return self._platform_name

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_YMCS_ENDPOINT_URL")
        if not self._client_id:
            missing.append("NCE_ASSETS_YMCS_CLIENT_ID")
        if not self._client_secret:
            missing.append("NCE_ASSETS_YMCS_CLIENT_SECRET")
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
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """The one call-site every request in this adapter goes through.

        The allow-list check happens here, before ``client.request`` is
        reached — a call outside *allowed* never becomes network traffic.
        """
        gate_path = _templated(path, concrete_ids)
        if (method, gate_path) not in allowed:
            raise YmcsAllowListRefusal(method, gate_path)
        resp = await client.request(
            method,
            f"{self._endpoint_url}{path}",
            headers=headers,
            json=json_body,
            params=params,
        )
        resp.raise_for_status()
        return resp

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        """OAuth2 client-credentials exchange. The secret is used exactly
        once, to build this Basic header, and is never passed to ``log``."""
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        resp = await self._gated_request(
            client,
            "POST",
            "/v2/token",
            allowed=_ALLOWED_AUTH,
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
            json_body={"grantType": "client_credentials"},
        )
        data = resp.json()
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise RuntimeError("YMCS token response carried no access token")
        return str(token)

    async def _find_device_by_serial(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], serial: str
    ) -> dict[str, Any] | None:
        wanted = serial.strip().lower()
        for page in range(_LIST_MAX_PAGES):
            resp = await self._gated_request(
                client,
                "POST",
                "/v2/dm/listDevices",
                allowed=_ALLOWED_READS,
                headers=auth_headers,
                json_body={
                    "skip": page * _LIST_PAGE_SIZE,
                    "limit": _LIST_PAGE_SIZE,
                    "autoCount": page == 0,
                },
            )
            rows = _as_rows(resp.json())
            for row in rows:
                sn = str(row.get("sn") or row.get("SN") or "").strip().lower()
                if sn and sn == wanted:
                    return row
            if len(rows) < _LIST_PAGE_SIZE:
                return None
        log.warning("YMCSTelemetryAdapter: listDevices exceeded %d pages, gave up", _LIST_MAX_PAGES)
        return None

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch telemetry readings for the specified asset from the real
        Yealink YMCS platform, resolving it by its captured serial number.
        """
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for '{self._platform_name}' is "
                f"unconfigured (missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "YMCSTelemetryAdapter: asset %s has no captured serial, cannot resolve a "
                "YMCS device",
                asset_id,
            )
            return []

        now = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                token = await self._access_token(client)
                auth_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

                device = await self._find_device_by_serial(client, auth_headers, serial)
                if device is None:
                    log.info("YMCSTelemetryAdapter: no YMCS device with serial %s", serial)
                    return []
                device_id = str(device.get("id") or "")
                if not device_id:
                    return []

                samples: list[TelemetrySample] = []

                detail_resp = await self._gated_request(
                    client,
                    "GET",
                    f"/v2/dm/devices/{device_id}",
                    allowed=_ALLOWED_READS,
                    concrete_ids=[device_id],
                    headers=auth_headers,
                    params={"select": "sensor,wifi"},
                )
                samples.extend(_samples_from_device_detail(detail_resp.json(), now))

                parts_resp = await self._gated_request(
                    client,
                    "POST",
                    f"/v2/dm/devices/{device_id}/listParts",
                    allowed=_ALLOWED_READS,
                    concrete_ids=[device_id],
                    headers=auth_headers,
                    json_body={"skip": 0, "limit": _LIST_PAGE_SIZE, "autoCount": False},
                )
                for part in _as_rows(parts_resp.json()):
                    part_id = str(part.get("id") or "")
                    if not part_id:
                        continue
                    part_resp = await self._gated_request(
                        client,
                        "GET",
                        f"/v2/dm/devices/{device_id}/parts/{part_id}",
                        allowed=_ALLOWED_READS,
                        concrete_ids=[device_id, part_id],
                        headers=auth_headers,
                    )
                    samples.extend(_samples_from_part_detail(part_resp.json(), now))

                return samples
        except YmcsAllowListRefusal:
            raise
        except Exception as exc:
            log.warning(
                "YMCSTelemetryAdapter: query failed for asset %s (serial %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="ymcs_telemetry_unavailable",
                    detail=(
                        f"YMCS telemetry query for serial {serial} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint=(
                        "Verify YMCS endpoint, client_id/client_secret and region via "
                        "connectors/save."
                    ),
                )
            except Exception:
                pass
            return []


#: Convenience alias for Yealink telemetry adapter
YealinkTelemetryAdapter = YMCSTelemetryAdapter
