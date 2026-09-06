"""
nce/vertical_modules/assets/neat_pulse.py
==========================================
Neat Pulse Real Telemetry Adapter.

Provides real read-only device and environmental telemetry for Neat devices
(Neat Bar, Neat Bar Pro, Neat Board, Neat Pad, Neat Center) over the Neat Pulse
Cloud REST API with strict timeouts (<5s).

Rules:
- Strictly read-only; no configuration mutation.
- Explicit socket timeout (<5s per AV Operations rule 5).
- Recorded in degradation register on connection/timeout error.
- Idempotent sample construction via TelemetrySample.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

import httpx

from nce.config import live_env_str
from nce.vertical_modules.assets.telemetry import TelemetryAdapter, TelemetrySample

log = logging.getLogger("nce.vertical_modules.assets.neat_pulse")

_DEFAULT_TIMEOUT_S = 4.5  # < 5.0s per AV Operations rule


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

    async def fetch_samples(self, asset_id: UUID) -> Sequence[TelemetrySample]:
        """Fetch telemetry and environmental sensor readings for the specified Neat device."""
        if not self._endpoint_url or not self._api_key:
            missing: list[str] = []
            if not self._endpoint_url:
                missing.append("NCE_ASSETS_NEAT_ENDPOINT_URL")
            if not self._api_key:
                missing.append("NCE_ASSETS_NEAT_API_KEY")
            missing_str = ", ".join(missing)
            raise NotImplementedError(
                f"do_pull_telemetry: real telemetry adapter for 'neat' is unconfigured "
                f"(missing {missing_str})"
            )

        now = datetime.now(timezone.utc)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        org_path = f"orgs/{self._org_id}/" if self._org_id else ""
        url = f"{self._endpoint_url}/v1/{org_path}devices/{asset_id}/telemetry"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            samples: list[TelemetrySample] = []
            sampled_at_str = data.get("sampled_at")
            sampled_at = datetime.fromisoformat(sampled_at_str) if sampled_at_str else now

            metrics = data.get("metrics", {})
            for m_name, m_val in metrics.items():
                if isinstance(m_val, (int, float)):
                    samples.append(
                        TelemetrySample(
                            metric=str(m_name),
                            value=float(m_val),
                            sampled_at=sampled_at,
                            raw=data.get("raw", {"source": "neat_pulse"}),
                        )
                    )
            return samples
        except Exception as exc:
            log.warning(
                "NeatPulseTelemetryAdapter: network call failed for asset %s: %s", asset_id, exc
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(asset_id),
                    engine="assets",
                    code="neat_telemetry_unavailable",
                    detail=f"Neat Pulse query to {url} failed: {type(exc).__name__}: {exc}",
                    onboarding_hint="Verify Neat Pulse API endpoint and Bearer tokens.",
                )
            except Exception:
                pass
            return []
