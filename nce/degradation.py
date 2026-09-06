"""
nce/degradation.py
==================
Wave I-5: Degradation Register — observable tracking of grace degradations.

Every grace-degradation branch in NCE vertical engines increments a per-namespace
counter exposed at ``GET /api/health/degradations``. Silent fallback becomes
an observable metric.

Copper Onboarding Contract:
    Rendered in Copper, the degradation register serves as onboarding guidance,
    not mere telemetry — e.g.:
    "Design recall is similarity-only: 0 attributed outcomes; record 5 to unlock."
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

log = logging.getLogger("nce.degradation")


@dataclass
class DegradationRecord:
    """Represents an observed grace-degradation path in a vertical engine."""

    namespace_id: str
    engine: str
    code: str
    count: int
    first_seen_at: str
    last_seen_at: str
    detail: str = ""
    onboarding_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DegradationRegister:
    """Thread-safe, in-memory per-namespace degradation register."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # namespace_id -> code -> DegradationRecord
        self._records: dict[str, dict[str, DegradationRecord]] = {}

    def record(
        self,
        namespace_id: str | UUID,
        engine: str,
        code: str,
        detail: str = "",
        onboarding_hint: str = "",
    ) -> DegradationRecord:
        """Record an occurrence of a grace degradation for a given namespace."""
        ns_key = str(namespace_id).strip() if namespace_id is not None else "system"
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            ns_dict = self._records.setdefault(ns_key, {})
            if code in ns_dict:
                rec = ns_dict[code]
                rec.count += 1
                rec.last_seen_at = now_iso
                if detail:
                    rec.detail = detail
                if onboarding_hint:
                    rec.onboarding_hint = onboarding_hint
            else:
                rec = DegradationRecord(
                    namespace_id=ns_key,
                    engine=str(engine).strip(),
                    code=str(code).strip(),
                    count=1,
                    first_seen_at=now_iso,
                    last_seen_at=now_iso,
                    detail=str(detail).strip(),
                    onboarding_hint=str(onboarding_hint).strip(),
                )
                ns_dict[code] = rec

            log.info(
                "Degradation recorded: ns=%s engine=%s code=%s count=%d",
                ns_key,
                engine,
                code,
                rec.count,
            )
            return rec

    def get_degradations(self, namespace_id: str | UUID | None = None) -> list[dict[str, Any]]:
        """Retrieve degradation records for a specific namespace or all namespaces."""
        with self._lock:
            if namespace_id is not None:
                ns_key = str(namespace_id).strip()
                return [r.to_dict() for r in self._records.get(ns_key, {}).values()]

            all_records: list[dict[str, Any]] = []
            for ns_dict in self._records.values():
                all_records.extend(r.to_dict() for r in ns_dict.values())
            return all_records

    def total_count(self, namespace_id: str | UUID | None = None) -> int:
        """Return total count of degradation occurrences."""
        with self._lock:
            if namespace_id is not None:
                ns_key = str(namespace_id).strip()
                return sum(r.count for r in self._records.get(ns_key, {}).values())

            return sum(r.count for ns_dict in self._records.values() for r in ns_dict.values())

    def get_summary(self) -> dict[str, Any]:
        """Return an estate-wide degradation summary with counts by engine and namespace."""
        with self._lock:
            total_events = 0
            by_engine: dict[str, int] = {}
            by_namespace: dict[str, int] = {}
            unique_codes: set[str] = set()

            for ns_key, ns_dict in self._records.items():
                for code, rec in ns_dict.items():
                    total_events += rec.count
                    unique_codes.add(code)
                    by_engine[rec.engine] = by_engine.get(rec.engine, 0) + rec.count
                    by_namespace[ns_key] = by_namespace.get(ns_key, 0) + rec.count

            return {
                "total_events": total_events,
                "unique_degradations": len(unique_codes),
                "by_engine": by_engine,
                "by_namespace": by_namespace,
            }

    def clear(self, namespace_id: str | UUID | None = None) -> None:
        """Clear records (primarily for testing isolation)."""
        with self._lock:
            if namespace_id is not None:
                ns_key = str(namespace_id).strip()
                self._records.pop(ns_key, None)
            else:
                self._records.clear()


# Global module singleton
_REGISTER = DegradationRegister()


def get_degradation_register() -> DegradationRegister:
    """Return the shared degradation register singleton."""
    return _REGISTER


def record_degradation(
    namespace_id: str | UUID,
    engine: str,
    code: str,
    detail: str = "",
    onboarding_hint: str = "",
) -> DegradationRecord:
    """Convenience function to record a degradation event in the global register."""
    return _REGISTER.record(
        namespace_id=namespace_id,
        engine=engine,
        code=code,
        detail=detail,
        onboarding_hint=onboarding_hint,
    )
