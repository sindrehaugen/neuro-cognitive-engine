"""
nce/vertical_modules/netbox/contacts.py
=======================================
BATCH-P3-NB-001 — Contacts → Operator Stress Mapping Integration

Integrates NetBox contact tenancy profiles with NCE's Longitudinal Operator
Stress Tracker. Updates on-call routing weights based on tracked frustration.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg
import httpx

from nce.signing import MasterKey, decrypt_signing_key, encrypt_signing_key

log = logging.getLogger("nce.vertical_modules.netbox.contacts")


class NetBoxClient:
    """
    HTTP client for querying NetBox DCIM and Tenancy contact models.
    """

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        self._client = client

    async def fetch_contacts(self) -> list[dict[str, Any]]:
        """Fetch all contact profiles from NetBox."""
        url = f"{self.base_url}/api/tenancy/contacts/"
        if self._client is not None:
            return await self._send_get(self._client, url)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            return await self._send_get(client, url)

    async def fetch_contact_assignments(self) -> list[dict[str, Any]]:
        """Fetch all site, rack, and device contact assignments from NetBox."""
        url = f"{self.base_url}/api/tenancy/contact-assignments/"
        if self._client is not None:
            return await self._send_get(self._client, url)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            return await self._send_get(client, url)

    async def _send_get(self, client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
        resp = await client.get(url, headers=self.headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])


class NetBoxContactSync:
    """
    Synchronizes NetBox contacts and updates on-call load routing based on
    longitudinal stress metrics (Empathic Tensor frustration > 7.0).
    """

    _schema_ensured: bool = False

    def __init__(self, pg_pool: Any, netbox_client: NetBoxClient):
        self.pg_pool = pg_pool
        self.netbox_client = netbox_client

    async def ensure_on_call_schema(self, conn: asyncpg.Connection) -> None:
        """
        Verify and construct the on-call load routing table.
        Enforces tenant isolation RLS policy directly.
        """
        if NetBoxContactSync._schema_ensured:
            return
        # Create on-call routing table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS on_call_routing (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
                contact_email TEXT NOT NULL,
                username TEXT NOT NULL,
                is_active_on_call BOOLEAN NOT NULL DEFAULT TRUE,
                routing_weight REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (namespace_id, contact_email)
            )
            """
        )

        # Enable RLS
        await conn.execute("ALTER TABLE on_call_routing ENABLE ROW LEVEL SECURITY")

        # Create isolation policy if it does not exist
        policy_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename = 'on_call_routing' AND policyname = 'on_call_tenant_isolation'
            )
            """
        )
        if not policy_exists:
            await conn.execute(
                """
                CREATE POLICY on_call_tenant_isolation ON on_call_routing
                FOR ALL USING (namespace_id = get_nce_namespace())
                """
            )

    async def fetch_stress_records_for_operator(
        self, conn: asyncpg.Connection, namespace_id: uuid.UUID, operator_id: str, email: str
    ) -> list[dict[str, Any]]:
        """
        Retrieve raw empathic records for a specific contact linked to memories.
        """
        rows = await conn.fetch(
            """
            SELECT l.empathic_tensor, l.created_at, l.tlx_scores, l.vad_scores
            FROM v3_cognitive_ledger l
            JOIN memories m ON l.memory_id = m.id
            WHERE l.namespace_id = $1::uuid
              AND (m.agent_id = $2 OR m.user_id = $2 OR m.agent_id = $3 OR m.user_id = $3)
            ORDER BY l.created_at ASC
            """,
            namespace_id,
            operator_id,
            email,
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

            records.append(
                {
                    "empathic_tensor": tensor,
                    "created_at": r["created_at"].isoformat()
                    if hasattr(r["created_at"], "isoformat")
                    else str(r["created_at"]),
                }
            )
        return records

    async def evaluate_contact_stress_report(
        self,
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        operator_id: str,
        email: str,
        master_key: MasterKey,
    ) -> dict[str, Any]:
        """
        Build, encrypt, and decrypt a contact's stress report to verify the data
        payload alignment and field parsing against NCE cryptoprimitives.
        """
        records = await self.fetch_stress_records_for_operator(
            conn, namespace_id, operator_id, email
        )
        if not records:
            return {
                "burnout_alert": False,
                "frustration_trend": [],
                "record_count": 0,
            }

        frustration_trend = [r["empathic_tensor"][5] for r in records]

        # Burnout criteria: > 5 consecutive shifts frustration > 7.0
        burnout_alert = False
        consecutive = 0
        for f in frustration_trend:
            if f > 7.0:
                consecutive += 1
                if consecutive >= 5:
                    burnout_alert = True
            else:
                consecutive = 0

        # Construct payload
        report_payload = {
            "burnout_alert": burnout_alert,
            "frustration_trend": frustration_trend,
            "record_count": len(records),
            "last_frustration": frustration_trend[-1] if frustration_trend else 0.0,
        }

        # Cryptographic field-level encryption verification
        plaintext = json.dumps(report_payload).encode("utf-8")
        encrypted_bytes = encrypt_signing_key(plaintext, master_key)

        # Decrypt to ensure data payload alignment
        decrypted_bytes = decrypt_signing_key(encrypted_bytes, master_key)
        verified_report = json.loads(decrypted_bytes.decode("utf-8"))

        return verified_report

    async def sync_contacts_and_update_oncall(
        self,
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        master_key: MasterKey,
    ) -> list[dict[str, Any]]:
        """
        Fetch NetBox contacts, bind stress records, check burnout thresholds, and
        update/redistribute on-call load routing weights dynamically.
        """
        # Ensure target schema exists
        await self.ensure_on_call_schema(conn)

        # Fetch contacts from NetBox
        contacts = await self.netbox_client.fetch_contacts()
        if not contacts:
            log.info("[NETBOX-CONTACTS] No contacts fetched from NetBox.")
            return []

        contact_details = []

        async with conn.transaction():
            # 1. Evaluate individual stress and update database
            for contact in contacts:
                username = contact.get("username") or contact.get("name", "").lower().replace(
                    " ", "_"
                )
                email = contact.get("email") or f"{username}@example.com"

                # Parse frustration metric from encrypted tensor pipeline
                report = await self.evaluate_contact_stress_report(
                    conn, namespace_id, username, email, master_key
                )
                last_frustration = report.get("last_frustration", 0.0)

                # Trigger burnout standby if frustration exceeds 7.0
                is_burned_out = last_frustration > 7.0 or report.get("burnout_alert", False)

                is_active = not is_burned_out
                status = "burnout_standby" if is_burned_out else "active"
                weight = 0.0 if is_burned_out else 1.0

                # Upsert into on_call_routing
                await conn.execute(
                    """
                    INSERT INTO on_call_routing (
                        namespace_id, contact_email, username, is_active_on_call, routing_weight, status, updated_at
                    ) VALUES ($1::uuid, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (namespace_id, contact_email) DO UPDATE
                    SET is_active_on_call = EXCLUDED.is_active_on_call,
                        routing_weight = EXCLUDED.routing_weight,
                        status = EXCLUDED.status,
                        updated_at = NOW()
                    """,
                    namespace_id,
                    email,
                    username,
                    is_active,
                    weight,
                    status,
                )

                contact_details.append(
                    {
                        "username": username,
                        "email": email,
                        "is_active": is_active,
                        "status": status,
                        "frustration": last_frustration,
                        "weight": weight,
                    }
                )

            # 2. Redistribute load weights among active contacts
            active_contacts = [c for c in contact_details if c["is_active"]]
            standby_contacts = [c for c in contact_details if not c["is_active"]]

            if active_contacts and standby_contacts:
                # Distribute lost standby weights (each standby had baseline weight of 1.0)
                lost_weight = len(standby_contacts) * 1.0
                weight_bonus = lost_weight / len(active_contacts)

                for c in active_contacts:
                    c["weight"] += weight_bonus

                    # Update redistributed weights in DB
                    await conn.execute(
                        """
                        UPDATE on_call_routing
                        SET routing_weight = $1::real, updated_at = NOW()
                        WHERE namespace_id = $2::uuid AND contact_email = $3
                        """,
                        c["weight"],
                        namespace_id,
                        c["email"],
                    )
                log.info(
                    "[NETBOX-CONTACTS] Redistributed %f load weight among %d active operators.",
                    lost_weight,
                    len(active_contacts),
                )

        return contact_details
