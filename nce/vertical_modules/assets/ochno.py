"""
nce/vertical_modules/assets/ochno.py
=======================================
Ochno Operated telemetry adapter (new platform, MLV16F Wave F-5).

Read-only device telemetry from Ochno's USB-C switch/hub platform
("Operated", ``operated.ochno.com``), over Ochno's real REST API, with
strict timeouts (<5s).

No vendor inherits another's answer (ML-orch, charter §13) — Ochno's
shape differs from all four prior waves again, and in the sharpest way
yet on the auth axis:
- There is no token exchange in this adapter AT ALL. Ochno's own client
  takes a ready-made bearer token (the same JWT its own web portal stores
  in ``localStorage``) and relies on a SEPARATE service to mint and rotate
  it; the host's own doc for this is explicit that renewal is "a separate,
  deliberate decision" outside the read client. This adapter follows the
  same shape: a pre-issued, short-lived (60-minute) bearer token supplied
  via ``connectors/save``, with no refresh flow of its own — a fifth
  distinct credential shape in five waves (OAuth2 client-credentials, bare
  API-key bearer, RS256 JWT-bearer, HS256 JWT-bearer, now an externally-
  rotated bare token).
- Ochno publishes **no API documentation at all** — no swagger, no
  ``/api/docs``, no developer site. The host's own client says its five
  endpoints were measured directly against the running portal. This
  adapter uses only the one endpoint it needs (``/api/hubs``) and does not
  claim any broader coverage is documented anywhere.
- Unlike every prior wave, the list response is a BARE JSON ARRAY, not an
  enveloped ``{"key": [...]}`` object.
- The hub payload carries no per-reading timestamp at all (verified
  against the host's own test fixtures) — it is a current-state snapshot,
  not a time series. ``sampled_at`` therefore falls back to the pull
  instant, same as every other adapter's already-established fallback
  when a vendor supplies no instant of its own.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
The endpoint and field shapes were read from the host's
``integrations/ochno_client.py`` FOR SHAPE ONLY, and cross-checked against
that repo's own test fixtures (``tests/test_ochno_assets.py``,
``tests/test_ochno_synk.py``) — a primary source for the exact JSON
fields, not a guess. No text, comment or structure from either file was
copied.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_READS`` is the explicit (method, path) allow-list literal —
   one entry, ``GET /api/hubs``. ``_gated_request`` refuses any pair
   outside it BEFORE issuing the HTTP call.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file, and the one allow-listed
   read is a genuine ``GET`` — no path-semantics comment needed.
4. The bearer token (and the endpoint URL) come from ``connectors/save``
   via ``live_env_str``, never logged.
5. Tests use ``httpx.MockTransport``; no test reaches the network.

Device resolution — the cleanest of five waves, not an inference
----------------------------------------------------------------
Verified against the host's own test fixtures: a hub row is
``{"id": ..., "spaceIds": [...], "data": {"serial": "O474C90FA5EDC1",
"product": "O-PC-4", "presence": true, "state": {"connected": [0,0,0,1],
"active": 4, "mtrmode": true}}}``. ``data.serial`` is an explicit,
top-level-under-``data`` field with real evidence behind it — unlike
Wave F-3 (Neowit) and F-4 (Disruptive), this adapter needs no fallback
matching strategy at all.
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

log = logging.getLogger("nce.vertical_modules.assets.ochno")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule

#: The one read this adapter actually issues. Independently re-derived
#: for this adapter's needs (Q-25) — Ochno's own client exposes four more
#: endpoints (accounts, spaces, spaces/{id}, system/configuration) that
#: this adapter never touches.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", "/api/hubs")})


class OchnoAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(f"Ochno adapter refused {method} {gate_path}: not in its read allow-list")
        self.method = method
        self.path = gate_path


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """Ochno's ``/api/hubs`` returns a bare array, not an enveloped
    object (verified: the host client does ``list(await self._request(...))``
    directly) — tolerate an envelope anyway rather than assume the shape
    never changes."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _samples_from_hub(row: dict[str, Any], now: datetime) -> list[TelemetrySample]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    samples: list[TelemetrySample] = []

    presence = data.get("presence")
    if isinstance(presence, bool):
        samples.append(
            TelemetrySample(
                metric="presence",
                value=1.0 if presence else 0.0,
                sampled_at=now,
                raw={"source": "ochno_hubs", "field": "presence"},
            )
        )

    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    active = state.get("active")
    if isinstance(active, (int, float)) and not isinstance(active, bool):
        samples.append(
            TelemetrySample(
                metric="state.active",
                value=float(active),
                sampled_at=now,
                raw={"source": "ochno_hubs", "field": "state.active"},
            )
        )
    mtrmode = state.get("mtrmode")
    if isinstance(mtrmode, bool):
        samples.append(
            TelemetrySample(
                metric="state.mtrmode",
                value=1.0 if mtrmode else 0.0,
                sampled_at=now,
                raw={"source": "ochno_hubs", "field": "state.mtrmode"},
            )
        )
    connected = state.get("connected")
    if isinstance(connected, list):
        for index, port_state in enumerate(connected):
            if isinstance(port_state, bool) or not isinstance(port_state, (int, float)):
                continue
            samples.append(
                TelemetrySample(
                    metric=f"state.connected.{index}",
                    value=float(port_state),
                    sampled_at=now,
                    raw={"source": "ochno_hubs", "field": f"state.connected[{index}]"},
                )
            )
    return samples


class OchnoTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for Ochno Operated hubs."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (endpoint_url or live_env_str("NCE_ASSETS_OCHNO_ENDPOINT_URL") or "")
            .strip()
            .rstrip("/")
        )
        self._token = token or live_env_str("NCE_ASSETS_OCHNO_TOKEN")
        self._timeout = min(timeout, 4.9)
        self._transport = transport

    @property
    def platform(self) -> str:
        return "ochno"

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_OCHNO_ENDPOINT_URL")
        if not self._token:
            missing.append("NCE_ASSETS_OCHNO_TOKEN")
        return missing

    async def _gated_request(
        self, client: httpx.AsyncClient, method: str, path: str
    ) -> httpx.Response:
        if (method, path) not in _ALLOWED_READS:
            raise OchnoAllowListRefusal(method, path)
        resp = await client.request(
            method,
            f"{self._endpoint_url}{path}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token}"},
        )
        resp.raise_for_status()
        return resp

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch the latest port/connectivity telemetry for the specified
        asset from Ochno, resolving it by matching its captured serial
        against ``data.serial`` on ``GET /api/hubs``."""
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'ochno' is unconfigured "
                f"(missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "OchnoTelemetryAdapter: asset %s has no captured serial, cannot resolve an "
                "Ochno hub",
                asset_id,
            )
            return []

        now = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await self._gated_request(client, "GET", "/api/hubs")
                rows = _as_rows(resp.json())
                wanted = serial.strip().lower()
                for row in rows:
                    data = row.get("data") if isinstance(row.get("data"), dict) else {}
                    hub_serial = str(data.get("serial") or "").strip().lower()
                    if hub_serial and hub_serial == wanted:
                        return _samples_from_hub(row, now)
                log.info("OchnoTelemetryAdapter: no hub with serial %s", serial)
                return []
        except Exception as exc:
            log.warning(
                "OchnoTelemetryAdapter: query failed for asset %s (serial %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="ochno_telemetry_unavailable",
                    detail=(
                        f"Ochno telemetry query for serial {serial} failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint=(
                        "Verify the Ochno endpoint and bearer token via connectors/save — "
                        "the token expires after 60 minutes and has no automatic refresh here."
                    ),
                )
            except Exception:
                pass
            return []
