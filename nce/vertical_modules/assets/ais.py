"""
nce/vertical_modules/assets/ais.py
=====================================
AIS vessel-position telemetry adapter (new platform, MLV16F Wave F-7 —
the last of the seven vendor telemetry adapters in this lane).

Read-only vessel position/heading telemetry from an AIS (Automatic
Identification System) live feed — the host's reference is BarentsWatch's
Live AIS API — with strict timeouts (<5s).

No vendor inherits another's answer (ML-orch, charter §13) — AIS's shape
differs from all six prior waves again, and introduces two firsts:
- OAuth2 client-credentials, but with client_id/client_secret sent as
  FORM FIELDS in the token request body (not a Basic header like F-1's
  YMCS, and not a signed JWT assertion like F-3/F-4's Neowit/Disruptive)
  — a sixth distinct credential shape in seven waves.
- An OPTIONAL-AUTH mode: when no client id is configured, the host's own
  client sends a plain unauthenticated GET — "open data source" is a
  valid, deliberate configuration, not a half-configured one. This
  adapter follows the same shape.
- One call returns the ENTIRE fleet currently reporting in the source's
  coverage area in one response; there is no per-vessel query at all. The
  asset is found by filtering that snapshot client-side on MMSI.

Q-25 (AGPL / proprietary boundary) — read for shape only, re-implemented
--------------------------------------------------------------------------
The endpoint, auth flow and response field names were read from the
host's ``integrations/ais_client.py`` FOR SHAPE ONLY, cross-checked
against that repo's own test fixtures (``tests/test_ais_client.py``) — a
primary source for the exact JSON field names, not a guess. No text,
comment or structure from either file was copied. The host's own
display-oriented normalisation (its heading memory/fallback chain for
drawing a ship on a map) is NOT re-implemented here — this adapter stores
a vendor-reported instant's own numbers, never an inferred or
remembered one, which is the more honest choice for a persisted sample.

The gate this file exists to satisfy (MLV16 charter, Lane F, all five)
--------------------------------------------------------------------------
1. ``_ALLOWED_READS`` is the explicit (method, path) allow-list literal
   (one entry: the base endpoint itself, path ``""``). ``_gated_request``
   refuses any pair outside it BEFORE issuing the HTTP call.
2. ``tests/test_assets_telemetry.py`` proves that refusal.
3. No write method exists anywhere in this file, and the one allow-listed
   read is a genuine ``GET`` — no path-semantics comment needed.
4. Credentials (endpoint, client id/secret, token URL, scope) come from
   ``connectors/save`` via ``live_env_str``, never logged. When no client
   id is configured, no credential is sent at all.
5. Tests use ``httpx.MockTransport``; no test reaches the network.

Device resolution — MMSI, the vessel's one real identifier
------------------------------------------------------------
A ship has no "serial number" in the hardware-adapter sense; its MMSI (a
9-digit maritime identifier, verified as the ``mmsi`` field on every
fixture row) is the one unambiguous identity every AIS source keys on.
This adapter matches the asset's ``serial`` against MMSI as a plain digit
string. Unlike F-3/F-4's identity questions, there was no second
candidate field to weigh — MMSI is what the vendor itself calls "the
number nobody can remember" (the host's own client), which is why one
exists at all.

Known-invalid sentinels are filtered, not stored as data
--------------------------------------------------------------
Verified against the host's own client: ``trueHeading == 511`` and
``courseOverGround == 360`` are the standard's own "not available" codes,
not real readings (511 % 360 = 151, a plausible-looking but fabricated
bearing). Both are range-checked to ``[0, 360)`` before being stored as
samples, matching the host's own documented reasoning — storing 151° for
a ship that reported no heading would be indistinguishable from a real
reading.
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

log = logging.getLogger("nce.vertical_modules.assets.ais")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule

#: The one read this adapter actually issues: the base endpoint itself,
#: which returns the whole fleet snapshot in one call. Independently
#: re-derived for this adapter's needs (Q-25).
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", "")})


class AisAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this adapter's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, gate_path: str) -> None:
        super().__init__(f"AIS adapter refused {method} {gate_path!r}: not in its read allow-list")
        self.method = method
        self.path = gate_path


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """The feed returns a bare JSON array (verified: the host client's own
    test doubles return the list directly) — tolerate an envelope anyway
    rather than assume the shape never changes."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _samples_from_vessel(row: dict[str, Any], now: datetime) -> list[TelemetrySample]:
    samples: list[TelemetrySample] = []

    lat = _num(row.get("latitude"))
    lon = _num(row.get("longitude"))
    if lat is not None:
        samples.append(
            TelemetrySample(metric="latitude", value=lat, sampled_at=now, raw={"source": "ais"})
        )
    if lon is not None:
        samples.append(
            TelemetrySample(metric="longitude", value=lon, sampled_at=now, raw={"source": "ais"})
        )

    speed = _num(row.get("speedOverGround"))
    if speed is not None:
        samples.append(
            TelemetrySample(
                metric="speedOverGround", value=speed, sampled_at=now, raw={"source": "ais"}
            )
        )

    # 511 is AIS's own "not available" sentinel for trueHeading — a real
    # value is always 0 <= heading < 360.
    heading = _num(row.get("trueHeading"))
    if heading is not None and 0 <= heading < 360:
        samples.append(
            TelemetrySample(
                metric="trueHeading", value=heading, sampled_at=now, raw={"source": "ais"}
            )
        )

    # 360 is the equivalent "not available" sentinel for courseOverGround.
    cog = _num(row.get("courseOverGround"))
    if cog is not None and 0 <= cog < 360:
        samples.append(
            TelemetrySample(
                metric="courseOverGround", value=cog, sampled_at=now, raw={"source": "ais"}
            )
        )

    return samples


class AisTelemetryAdapter(TelemetryAdapter):
    """Real read-only telemetry adapter for AIS vessel positions."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        scope: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = (
            (endpoint_url or live_env_str("NCE_ASSETS_AIS_ENDPOINT_URL") or "").strip().rstrip("/")
        )
        self._client_id = (client_id or live_env_str("NCE_ASSETS_AIS_CLIENT_ID") or "").strip()
        self._client_secret = (
            client_secret or live_env_str("NCE_ASSETS_AIS_CLIENT_SECRET") or ""
        ).strip()
        self._token_url = (token_url or live_env_str("NCE_ASSETS_AIS_TOKEN_URL") or "").strip()
        self._scope = (scope or live_env_str("NCE_ASSETS_AIS_SCOPE") or "").strip()
        self._timeout = min(timeout, 4.9)
        self._transport = transport

    @property
    def platform(self) -> str:
        return "ais"

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._endpoint_url:
            missing.append("NCE_ASSETS_AIS_ENDPOINT_URL")
        # An unset client id is a valid "open source" mode (module
        # docstring) — only demand its companions when it IS set.
        if self._client_id:
            if not self._client_secret:
                missing.append("NCE_ASSETS_AIS_CLIENT_SECRET")
            if not self._token_url:
                missing.append("NCE_ASSETS_AIS_TOKEN_URL")
        return missing

    async def _access_token(self, client: httpx.AsyncClient) -> str | None:
        """``None`` when the source is open (no client id configured) —
        a valid mode, not a missing one. The token endpoint is a distinct,
        explicitly-configured URL, checked directly rather than through
        ``_gated_request`` (which only knows the data endpoint's base)."""
        if not self._client_id:
            return None
        resp = await client.post(
            self._token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": self._scope,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("AIS token response carried no access_token")
        return str(token)

    async def _gated_request(
        self, client: httpx.AsyncClient, method: str, path: str, *, headers: dict[str, str]
    ) -> httpx.Response:
        if (method, path) not in _ALLOWED_READS:
            raise AisAllowListRefusal(method, path)
        url = self._endpoint_url if not path else f"{self._endpoint_url}/{path}"
        resp = await client.request(method, url, headers=headers, params={"modelType": "Full"})
        resp.raise_for_status()
        return resp

    async def fetch_samples(
        self, asset_id: UUID, *, serial: str | None = None
    ) -> Sequence[TelemetrySample]:
        """Fetch the latest position/heading telemetry for the specified
        vessel asset, resolving it by matching its captured serial (the
        vessel's MMSI) against the live fleet snapshot."""
        missing = self._missing_config()
        if missing:
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'ais' is unconfigured "
                f"(missing {', '.join(missing)})"
            )
        if not serial or not serial.strip():
            log.info(
                "AisTelemetryAdapter: asset %s has no captured serial (MMSI), cannot resolve a "
                "vessel",
                asset_id,
            )
            return []
        wanted_mmsi = serial.strip()
        if not wanted_mmsi.isdigit():
            log.info(
                "AisTelemetryAdapter: asset %s's serial %r is not a valid MMSI (digits only)",
                asset_id,
                serial,
            )
            return []

        now = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                token = await self._access_token(client)
                headers = {"Accept": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                resp = await self._gated_request(client, "GET", "", headers=headers)
                for row in _as_rows(resp.json()):
                    mmsi = row.get("mmsi")
                    if mmsi is not None and str(int(mmsi)) == wanted_mmsi:
                        return _samples_from_vessel(row, now)
                log.info("AisTelemetryAdapter: no vessel with MMSI %s in the live feed", serial)
                return []
        except Exception as exc:
            log.warning(
                "AisTelemetryAdapter: query failed for asset %s (MMSI %s): %s",
                asset_id,
                serial,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="ais_telemetry_unavailable",
                    detail=(
                        f"AIS telemetry query for MMSI {serial} failed: {type(exc).__name__}: {exc}"
                    ),
                    onboarding_hint=(
                        "Verify the AIS endpoint (and, if using an authenticated source, the "
                        "client id/secret and token URL) via connectors/save."
                    ),
                )
            except Exception:
                pass
            return []
