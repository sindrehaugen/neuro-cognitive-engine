"""
nce/vertical_modules/netbox/mtbf.py
===================================
BATCH-P3-NB-004 — Predictive MTBF Synthesis Engine

Integrates NetBox device hardware lifespan metrics, provisioning dates, and
serial parameters with NCE's operational anomaly frequency registers from the
`event_log` database. Calculates custom MTBF probability matrices to pinpoint
decaying devices.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from nce.vertical_modules.netbox.graphql_activation import NetBoxGraphQLClient

log = logging.getLogger("nce.vertical_modules.netbox.mtbf")

# GraphQL query to retrieve NetBox devices with serial and custom fields
DEVICE_HARDWARE_QUERY = """
query GetDevicesMTBFAge {
  device_list {
    id
    name
    serial
    custom_fields
  }
}
"""


class NetBoxMTBFForecaster:
    """
    Forecasting engine that cross-references physical hardware lifespan age and
    historical anomaly registries to synthesize failure probability matrices.
    """

    def __init__(self, netbox_client: NetBoxGraphQLClient):
        self.netbox_client = netbox_client

    async def fetch_anomaly_counts(
        self, conn: Any, namespace_id: uuid.UUID, window_days: float = 30.0
    ) -> dict[str, int]:
        """
        Query event_log to aggregate incident failure occurrences per node/device.
        """
        # Enforce database RLS
        from nce.auth import set_namespace_context

        await set_namespace_context(conn, namespace_id)

        rows = await conn.fetch(
            """
            SELECT event_type, params
            FROM event_log
            WHERE namespace_id = $1::uuid
              AND occurred_at >= NOW() - ($2::float * INTERVAL '1 day')
              AND (
                  event_type IN ('store_memory_rolled_back', 'saga_recovered')
                  OR event_type LIKE '%error%'
                  OR event_type LIKE '%fail%'
              )
            """,
            namespace_id,
            window_days,
        )

        counts: dict[str, int] = {}
        for r in rows:
            params_raw = r["params"]
            params: dict[str, Any] = {}

            if isinstance(params_raw, str):
                try:
                    params = json.loads(params_raw)
                except Exception:
                    pass
            elif isinstance(params_raw, dict):
                params = params_raw

            node_ids: list[str] = []
            # Check direct reference fields
            direct_id = (
                params.get("device_id")
                or params.get("node_id")
                or params.get("memory_id")
                or params.get("device")
            )
            if direct_id:
                node_ids.append(str(direct_id))

            # Check entities parameter
            entities = params.get("entities", [])
            for ent in entities:
                if isinstance(ent, dict) and "label" in ent:
                    node_ids.append(str(ent["label"]))
                elif isinstance(ent, str):
                    node_ids.append(ent)

            # Deduplicate per incident
            for nid in set(node_ids):
                counts[nid] = counts.get(nid, 0) + 1

        return counts

    async def fetch_device_inventory(self) -> list[dict[str, Any]]:
        """
        Query NetBox GraphQL endpoint for hardware attributes.
        """
        response = await self.netbox_client.execute_query(DEVICE_HARDWARE_QUERY)
        data_payload = response.get("data") or {}
        return data_payload.get("device_list") or []

    def _parse_device_hardware(self, device: dict[str, Any]) -> tuple[datetime, float, str]:
        """
        Parses provisioning date, expected lifespan, and serial defensively.
        """
        cf = device.get("custom_fields") or {}

        # 1. Parse provisioning date from various possible custom field keys
        prov_raw = (
            cf.get("provisioning_date") or cf.get("provisioned_date") or cf.get("date_provisioned")
        )
        prov_date = None

        if prov_raw:
            try:
                if isinstance(prov_raw, str):
                    if "T" in prov_raw:
                        prov_date = datetime.fromisoformat(prov_raw.replace("Z", "+00:00"))
                    else:
                        prov_date = datetime.strptime(prov_raw, "%Y-%m-%d")
                elif isinstance(prov_raw, (int, float)):
                    prov_date = datetime.fromtimestamp(prov_raw, tz=timezone.utc)
            except Exception as exc:
                log.debug("Failed parsing provisioning date raw value %s: %s", prov_raw, exc)

        if prov_date is None:
            # Default fallback: 3 years ago
            prov_date = datetime.now(timezone.utc) - timedelta(days=3 * 365.25)

        if prov_date.tzinfo is None:
            prov_date = prov_date.replace(tzinfo=timezone.utc)

        # 2. Parse hardware lifespan in years
        lifespan_raw = (
            cf.get("hardware_lifespan_years") or cf.get("lifespan_years") or cf.get("lifespan")
        )
        try:
            lifespan = float(lifespan_raw) if lifespan_raw is not None else 5.0
        except Exception:
            lifespan = 5.0

        serial = str(device.get("serial") or "UNKNOWN")
        return prov_date, lifespan, serial

    async def evaluate_forecast(
        self,
        conn: Any,
        namespace_id: uuid.UUID,
        forecast_window_days: float = 30.0,
        observation_window_days: float = 30.0,
        baseline_mtbf_years: float = 10.0,
    ) -> list[dict[str, Any]]:
        """
        Calculates the synthesized MTBF failure probability matrix.
        Pinpoints devices nearing physical decay windows.
        """
        # Fetch operational anomalies and inventory
        anomaly_counts = await self.fetch_anomaly_counts(
            conn, namespace_id, observation_window_days
        )
        devices = await self.fetch_device_inventory()

        now = datetime.now(timezone.utc)
        results = []

        # Convert baseline MTBF (e.g. 10 years) to hours
        # 1 year = 8766 hours (taking leap years into account)
        baseline_mtbf_hours = baseline_mtbf_years * 8766.0
        lambda_0 = 1.0 / baseline_mtbf_hours if baseline_mtbf_hours > 0 else 0.0

        observation_hours = observation_window_days * 24.0
        forecast_hours = forecast_window_days * 24.0

        for device in devices:
            device_name = device.get("name")
            if not device_name:
                continue

            prov_date, lifespan_years, serial = self._parse_device_hardware(device)

            # Age calculation in years
            age_seconds = (now - prov_date).total_seconds()
            age_years = age_seconds / (365.25 * 24.0 * 3600.0)
            if age_years < 0.0:
                age_years = 0.0

            # 1. Age-based failure rate: Weibull-like exponential wear-out rate
            # Failure rate increases exponentially as age exceeds expected lifespan
            lifespan_ratio = age_years / lifespan_years if lifespan_years > 0 else 1.0
            # Safety limit to avoid float OverflowError
            lifespan_ratio = min(10.0, lifespan_ratio)
            lambda_age = lambda_0 * math.exp(3.0 * lifespan_ratio)

            # 2. Operational anomaly-based failure rate
            anomaly_count = anomaly_counts.get(device_name, 0)
            lambda_empirical = anomaly_count / observation_hours if observation_hours > 0 else 0.0

            # 3. Combined rate (failures per hour)
            lambda_combined = lambda_age + lambda_empirical

            # 4. Failure probability over forecast window
            # P_fail = 1 - e^(-lambda * t)
            prob_fail = 1.0 - math.exp(-lambda_combined * forecast_hours)
            prob_fail = max(0.0, min(1.0, prob_fail))

            # 5. Estimated MTBF in hours
            estimated_mtbf = 1.0 / lambda_combined if lambda_combined > 0.0 else float("inf")

            results.append(
                {
                    "device_name": device_name,
                    "serial": serial,
                    "age_years": round(age_years, 2),
                    "lifespan_years": round(lifespan_years, 2),
                    "anomaly_count": anomaly_count,
                    "failure_probability": round(prob_fail, 4),
                    "estimated_mtbf_hours": round(estimated_mtbf, 1)
                    if estimated_mtbf != float("inf")
                    else float("inf"),
                }
            )

        # Sort matrix: high failure probability first
        results.sort(key=lambda x: x["failure_probability"], reverse=True)
        return results
