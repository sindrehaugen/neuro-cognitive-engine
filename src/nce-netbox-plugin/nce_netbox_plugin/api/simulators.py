from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def generate_simulated_data(
    namespace: str, as_of: datetime, error: str | None = None
) -> dict[str, Any]:
    """Generates realistic, time-diverged mock data for frontend rendering when core tables are not bound."""
    # Deterministic simulation based on as_of timestamp
    seed_hour = as_of.hour
    seed_day = as_of.day

    # 1. Incident Count Logs
    incidents = [
        {
            "event_seq": 105,
            "event_type": "circuit_degraded",
            "occurred_at": (as_of - timedelta(minutes=15)).isoformat(),
            "agent_id": "escalation_engine",
            "params": {"circuit_id": "CR_ST_009", "commit_rate_ratio": 0.35},
        },
        {
            "event_seq": 104,
            "event_type": "store_memory_failed",
            "occurred_at": (as_of - timedelta(hours=2)).isoformat(),
            "agent_id": "trimcp_integrator",
            "params": {"reason": "LockNotAvailableError", "target_key": "device-switch-01"},
        },
        {
            "event_seq": 103,
            "event_type": "burnout_alert",
            "occurred_at": (as_of - timedelta(hours=4)).isoformat(),
            "agent_id": "operator_stress_tracker",
            "params": {"operator_id": "operator-102", "frustration": 8.2},
        },
        {
            "event_seq": 102,
            "event_type": "predictive_fault_generated",
            "occurred_at": (as_of - timedelta(hours=6)).isoformat(),
            "agent_id": "mtbf_synthesis",
            "params": {"device_name": "switch-02", "failure_probability": 0.81},
        },
        {
            "event_seq": 101,
            "event_type": "unregistered_asset_discovered",
            "occurred_at": (as_of - timedelta(hours=10)).isoformat(),
            "agent_id": "asset_discovery",
            "params": {"device_name": "switch-new-09", "staging_branch": "nce-staged-discovery"},
        },
    ]

    # 2. Operator Stress Trend (longitudinal trend line)
    stress_trend = []
    base_time = as_of - timedelta(days=5)
    for i in range(24):
        time_point = base_time + timedelta(hours=i * 5)
        # Make stress spike/dip deterministically based on seed
        wave = (seed_hour + i) % 8
        frustration = 3.0 + wave * 0.6 if wave > 3 else 2.0 + wave * 0.3
        fatigue = 2.5 + (i * 0.15) - (wave * 0.2)

        # Encrypted tensor representation values (simulation overrides)
        stress_trend.append(
            {
                "frustration": round(frustration, 2),
                "fatigue": round(fatigue, 2),
                "created_at": time_point.isoformat(),
            }
        )

    # 3. Ahead-of-time Fault Maps
    fault_nodes = [
        {
            "node_id": "dev-01",
            "name": "switch-01",
            "node_type": "device",
            "failure_probability": round(0.12 + (seed_day % 5) * 0.05, 2),
            "estimated_mtbf_hours": 8760.0,
        },
        {
            "node_id": "dev-02",
            "name": "switch-02",
            "node_type": "device",
            "failure_probability": round(0.75 + (seed_hour % 4) * 0.04, 2),
            "estimated_mtbf_hours": 320.0,
        },
        {
            "node_id": "dev-03",
            "name": "switch-03",
            "node_type": "device",
            "failure_probability": round(0.04 + (seed_hour % 2) * 0.02, 2),
            "estimated_mtbf_hours": 24000.0,
        },
    ]

    # 4. Replay runs
    replay_runs = [
        {
            "run_id": "replay-run-101",
            "mode": "observational",
            "replay_mode": "deterministic",
            "status": "completed",
            "events_applied": 156,
        },
        {
            "run_id": "replay-run-102",
            "mode": "forked",
            "replay_mode": "re-execute",
            "status": "completed",
            "events_applied": 45,
        },
    ]

    return {
        "real_database": False,
        "database_error": error,
        "namespace": namespace,
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "as_of": as_of.isoformat(),
        "pending_queue_count": 3,
        "incidents": incidents,
        "operator_stress_trend": stress_trend,
        "replay_runs": replay_runs,
        "fault_nodes": fault_nodes,
    }
