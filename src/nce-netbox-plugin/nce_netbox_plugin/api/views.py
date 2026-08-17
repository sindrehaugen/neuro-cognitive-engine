from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.http import JsonResponse
from django.views import View

from nce_netbox_plugin.api.simulators import generate_simulated_data


class NceCognitiveStatsView(View):
    """
    Exposes an API endpoint providing cognitive telemetry data from NCE core.
    Supports temporal playback via the 'as_of' query parameter.
    """

    REQUIRED_NCE_TABLES = [
        "namespaces",
        "event_log",
        "v3_cognitive_ledger",
        "replay_runs",
        "kg_nodes",
    ]

    def get(self, request, *args, **kwargs):
        as_of_raw = request.GET.get("as_of")
        namespace_slug = request.GET.get("namespace")
        object_type = request.GET.get("object_type")
        object_id = request.GET.get("object_id")

        # Resolve namespace slug based on tenant of NetBox object
        default_slug = getattr(settings, "NCE_DEFAULT_NAMESPACE_SLUG", "default")
        if not namespace_slug:
            if object_type and object_id:
                namespace_slug = self._resolve_tenant_slug(object_type, object_id) or default_slug
            else:
                namespace_slug = default_slug

        # Parse temporal playback target
        if as_of_raw:
            try:
                as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
            except ValueError:
                return JsonResponse(
                    {"error": "Invalid ISO 8601 timestamp format for 'as_of'"}, status=400
                )
        else:
            as_of = datetime.now(timezone.utc)

        # 1. Check if NCE database tables exist in the target database
        try:
            nce_tables_exist = self._verify_nce_tables()
        except Exception:
            nce_tables_exist = False

        if nce_tables_exist:
            try:
                data = self._fetch_real_nce_data(namespace_slug, as_of)
                return JsonResponse(data)
            except Exception as err:
                # Fallback to simulated data if query fails
                return JsonResponse(generate_simulated_data(namespace_slug, as_of, error=str(err)))
        else:
            return JsonResponse(generate_simulated_data(namespace_slug, as_of))

    def _resolve_tenant_slug(self, object_type: str, object_id: str) -> str | None:
        """Resolves the tenant slug associated with the NetBox object."""
        try:
            if object_type == "device":
                from dcim.models import Device

                obj = Device.objects.filter(id=object_id).first()
            elif object_type == "rack":
                from dcim.models import Rack

                obj = Rack.objects.filter(id=object_id).first()
            elif object_type == "site":
                from dcim.models import Site

                obj = Site.objects.filter(id=object_id).first()
            else:
                return None

            if obj and obj.tenant:
                return obj.tenant.slug
        except Exception:
            pass
        return None

    def _verify_nce_tables(self) -> bool:
        """Checks if NCE core tables exist in the active PostgreSQL database schema."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tablename FROM pg_tables 
                WHERE schemaname = 'public' AND tablename = ANY(%s);
                """,
                [self.REQUIRED_NCE_TABLES],
            )
            found = {row[0] for row in cursor.fetchall()}
            return len(found) == len(self.REQUIRED_NCE_TABLES)

    def _fetch_real_nce_data(self, namespace_slug: str, as_of: datetime) -> dict[str, Any]:
        """Queries the raw NCE core tables bounded by RLS and temporal limits."""
        with transaction.atomic():
            with connection.cursor() as cursor:
                # 1. Resolve namespace UUID
                cursor.execute(
                    "SELECT id FROM namespaces WHERE slug = %s LIMIT 1;", [namespace_slug]
                )
                ns_row = cursor.fetchone()
                if not ns_row:
                    # If namespace doesn't exist, fall back to first namespace
                    cursor.execute("SELECT id, slug FROM namespaces LIMIT 1;")
                    ns_row = cursor.fetchone()
                    if not ns_row:
                        raise ValueError("No NCE namespaces exist in database.")
                ns_id = ns_row[0]
                ns_slug = ns_row[1] if len(ns_row) > 1 else namespace_slug

                # Set PostgreSQL session-level RLS context (restricted to this transaction)
                cursor.execute("SELECT set_config('nce.namespace_id', %s, true);", [str(ns_id)])

                # 2. Fetch events occurred_at <= as_of
                cursor.execute(
                    """
                    SELECT id, event_seq, event_type, occurred_at, agent_id, params
                    FROM event_log
                    WHERE namespace_id = %s AND occurred_at <= %s
                    ORDER BY event_seq DESC
                    LIMIT 10;
                    """,
                    [ns_id, as_of],
                )
                event_rows = cursor.fetchall()
                events = []
                for r in event_rows:
                    events.append(
                        {
                            "event_id": str(r[0]),
                            "event_seq": r[1],
                            "event_type": r[2],
                            "occurred_at": r[3].isoformat()
                            if hasattr(r[3], "isoformat")
                            else str(r[3]),
                            "agent_id": r[4],
                            "params": json.loads(r[5]) if isinstance(r[5], str) else r[5],
                        }
                    )

                # 3. Fetch operator stress logs created_at <= as_of
                cursor.execute(
                    """
                    SELECT empathic_tensor, tlx_scores, vad_scores, created_at
                    FROM v3_cognitive_ledger
                    WHERE namespace_id = %s AND created_at <= %s
                    ORDER BY created_at ASC;
                    """,
                    [ns_id, as_of],
                )
                stress_rows = cursor.fetchall()
                stress_trend = []
                for r in stress_rows:
                    tensor_raw = r[0]
                    tensor = []
                    if isinstance(tensor_raw, str):
                        tensor = [float(x) for x in tensor_raw.strip("[]").split(",") if x.strip()]
                    elif tensor_raw is not None:
                        tensor = [float(x) for x in tensor_raw]

                    while len(tensor) < 6:
                        tensor.append(0.0)
                    tensor = tensor[:6]

                    stress_trend.append(
                        {
                            "frustration": tensor[5],
                            "fatigue": tensor[0] * 0.4
                            + tensor[1] * 0.4,  # Derived VAD fatigue indicator
                            "created_at": r[3].isoformat()
                            if hasattr(r[3], "isoformat")
                            else str(r[3]),
                        }
                    )

                # 4. Fetch Replay Runs
                cursor.execute(
                    """
                    SELECT id, mode, replay_mode, status, events_applied
                    FROM replay_runs
                    WHERE source_namespace_id = %s OR target_namespace_id = %s
                    ORDER BY started_at DESC
                    LIMIT 5;
                    """,
                    [ns_id, ns_id],
                )
                replay_rows = cursor.fetchall()
                replays = []
                for r in replay_rows:
                    replays.append(
                        {
                            "run_id": str(r[0]),
                            "mode": r[1],
                            "replay_mode": r[2],
                            "status": r[3],
                            "events_applied": r[4],
                        }
                    )

                # 5. Fetch Pending Confirmation queue count
                cursor.execute(
                    "SELECT COUNT(*)::int FROM active_learning_queue WHERE namespace_id = %s AND status = 'pending';",
                    [ns_id],
                )
                pending_count = cursor.fetchone()[0]

                return {
                    "real_database": True,
                    "namespace": ns_slug,
                    "namespace_id": str(ns_id),
                    "as_of": as_of.isoformat(),
                    "pending_queue_count": pending_count,
                    "incidents": events,
                    "operator_stress_trend": stress_trend,
                    "replay_runs": replays,
                    "fault_nodes": self._derive_fault_nodes_from_db(ns_id, as_of),
                }

    def _derive_fault_nodes_from_db(self, ns_id: Any, as_of: datetime) -> list[dict[str, Any]]:
        """Extracts active predictive fault nodes in NCE core topology."""
        # Check if topology_graph or similar exists. Fallback if empty
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    SELECT id, name, node_type, metadata 
                    FROM kg_nodes 
                    WHERE namespace_id = %s AND created_at <= %s;
                    """,
                    [ns_id, as_of],
                )
                rows = cursor.fetchall()
                nodes = []
                for r in rows:
                    meta = json.loads(r[3]) if isinstance(r[3], str) else r[3] or {}
                    # Filter for nodes flagged with failure probability
                    if "failure_probability" in meta or r[2] == "predictive_fault":
                        nodes.append(
                            {
                                "node_id": str(r[0]),
                                "name": r[1],
                                "node_type": r[2],
                                "failure_probability": meta.get("failure_probability", 0.0),
                                "estimated_mtbf_hours": meta.get("estimated_mtbf_hours", 720.0),
                            }
                        )
                return nodes
            except Exception:
                return []
