"""
Longitudinal Operator Stress Tracking (BATCH-P3-004).

This module contains analytical algorithms to trend operator frustration and
predict fatigue via exponential smoothing over VAD vectors, enforcing strict
field-level encryption at rest on all personal stress metrics.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from nce.signing import MasterKey, decrypt_signing_key, encrypt_signing_key

log = logging.getLogger("nce.analytics.stress")


class StressTracker:
    """
    Longitudinal Operator Stress Tracking system (BATCH-P3-004).

    Provides analytical tools to trend empathic tensor metrics, calculate
    predictive fatigue (smoothed VAD), and identify burnout alerts, while
    guaranteeing strict field-level encryption at rest using repository cryptoprimitives.
    """

    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn

    async def get_raw_empathic_data(self, namespace_id: UUID | str) -> list[dict[str, Any]]:
        """
        Fetch chronological empathic records for a tenant with RLS scope.
        """
        ns_uuid = UUID(str(namespace_id))
        from nce.auth import set_namespace_context

        await set_namespace_context(self.conn, ns_uuid)

        rows = await self.conn.fetch(
            """
            SELECT empathic_tensor, created_at, tlx_scores, vad_scores 
            FROM v3_cognitive_ledger 
            WHERE namespace_id = $1::uuid 
            ORDER BY created_at ASC
            """,
            ns_uuid,
        )
        records = []
        for r in rows:
            tensor_raw = r["empathic_tensor"]
            tensor = []
            if isinstance(tensor_raw, str):
                tensor = [float(x) for x in tensor_raw.strip("[]").split(",") if x.strip()]
            elif tensor_raw is not None:
                tensor = [float(x) for x in tensor_raw]

            while len(tensor) < 6:
                tensor.append(0.0)
            tensor = tensor[:6]

            tlx = r["tlx_scores"]
            if isinstance(tlx, str):
                try:
                    tlx = json.loads(tlx)
                except Exception:
                    tlx = {}

            vad = r["vad_scores"]
            if isinstance(vad, str):
                try:
                    vad = json.loads(vad)
                except Exception:
                    vad = {}

            records.append(
                {
                    "empathic_tensor": tensor,
                    "created_at": r["created_at"].isoformat()
                    if hasattr(r["created_at"], "isoformat")
                    else str(r["created_at"]),
                    "tlx_scores": tlx,
                    "vad_scores": vad,
                }
            )
        return records

    async def analyze_and_encrypt_stress(
        self,
        namespace_id: UUID | str,
        master_key: MasterKey,
        beta: float = 0.3,
    ) -> bytes:
        """
        Trend frustration, calculate fatigue (smoothed VAD), check for burnout,
        and encrypt the resulting personal stress metrics.
        """
        records = await self.get_raw_empathic_data(namespace_id)
        if not records:
            report = {
                "burnout_alert": False,
                "frustration_trend": [],
                "smoothed_vad_trend": [],
                "record_count": 0,
                "raw_records": [],
            }
            return self._encrypt_report(report, master_key)

        # 1. Trend frustration (index 5)
        frustration_trend = [r["empathic_tensor"][5] for r in records]

        # 2. Burnout Alert Rule: frustration > 7.0 across 5+ consecutive tracked shifts
        burnout_alert = False
        consecutive_count = 0
        for f in frustration_trend:
            if f > 7.0:
                consecutive_count += 1
                if consecutive_count >= 5:
                    burnout_alert = True
            else:
                consecutive_count = 0

        # 3. Predictive Fatigue Parser: Exponential smoothing over VAD vector (indices 0, 1, 2)
        smoothed_vad_trend = []
        last_smoothed = None
        for r in records:
            tensor = r["empathic_tensor"]
            current_vad = [tensor[0], tensor[1], tensor[2]]

            if last_smoothed is None:
                smoothed = current_vad
            else:
                smoothed = [
                    beta * current_vad[i] + (1.0 - beta) * last_smoothed[i] for i in range(3)
                ]
            smoothed_vad_trend.append(smoothed)
            last_smoothed = smoothed

        report = {
            "burnout_alert": burnout_alert,
            "frustration_trend": frustration_trend,
            "smoothed_vad_trend": smoothed_vad_trend,
            "record_count": len(records),
            "raw_records": records,
        }

        return self._encrypt_report(report, master_key)

    def _encrypt_report(self, report: dict, master_key: MasterKey) -> bytes:
        plaintext = json.dumps(report).encode("utf-8")
        return encrypt_signing_key(plaintext, master_key)

    @staticmethod
    def decrypt_report(encrypted_report: bytes, master_key: MasterKey) -> dict:
        """
        Decrypts an encrypted stress report.
        """
        plaintext_bytes = decrypt_signing_key(encrypted_report, master_key)
        return json.loads(plaintext_bytes.decode("utf-8"))
