"""
nce.vertical_modules.support.on_call
====================================
On-call rota and active responder routing for Module 10 (Support Engine).
Replaces legacy ad-hoc on_call_routing table by querying Module 15 (Staff & Resources Engine)
allocations with role=on_call per Charter §8 Wave D-7.

Cross-engine calls route strictly through `engine.modules["resources"]` per Charter §8.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from nce.engine_registry import EngineDisabledError, EngineUnavailableError

log = logging.getLogger("nce.vertical_modules.support.on_call")


def _parse_uuid(val: Any, field_name: str) -> uuid.UUID:
    if not val:
        raise ValueError(f"{field_name} is required.")
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


async def do_get_on_call(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Retrieve active on-call responders for a tenant namespace.
    Resolves allocations from the Staff & Resources Engine via `engine.modules["resources"]`.

    Parameters:
      - namespace_id: UUID of tenant (required)
      - at: ISO datetime string to query point-in-time on-call roster (optional, defaults to now UTC)
      - starts_at, ends_at: ISO datetime window for schedule ranges (optional)
      - include_released: bool, whether to include released allocations (optional, default False)
      - contractor_view: bool, whether to redact internal rates/margins (optional, default False)
      - namespace_metadata: dict, engine opt-in flags (optional)

    Returns:
      dict with:
        - namespace_id: str
        - on_call: list of active allocation records joined with resource details
        - count: int
        - query_time: ISO-8601 UTC timestamp of query execution
        - resources_available: bool
    """
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")

    at_raw = params.get("at")
    if at_raw:
        try:
            at_dt = datetime.fromisoformat(str(at_raw).replace("Z", "+00:00"))
            if at_dt.tzinfo is None:
                at_dt = at_dt.replace(tzinfo=timezone.utc)
            else:
                at_dt = at_dt.astimezone(timezone.utc)
            query_time = at_dt.isoformat()
        except Exception as exc:
            raise ValueError(f"Invalid 'at' ISO datetime: {at_raw!r}") from exc
    else:
        query_time = datetime.now(timezone.utc).isoformat()

    # Cross-engine invocation via engine.modules per Charter §8
    resources_module: Any = None
    if engine is not None and getattr(engine, "modules", None) is not None:
        try:
            if callable(getattr(engine.modules, "for_namespace", None)):
                scoped_registry = engine.modules.for_namespace(ns_id)
                resources_module = scoped_registry["resources"]
            else:
                resources_module = engine.modules["resources"]
        except EngineDisabledError:
            log.info(
                "Resources engine disabled for namespace %s; returning empty on-call rota.", ns_id
            )
            return {
                "namespace_id": str(ns_id),
                "on_call": [],
                "count": 0,
                "query_time": query_time,
                "resources_available": False,
            }
        except (EngineUnavailableError, KeyError):
            log.warning("Resources engine not found in engine.modules; attempting direct fallback.")
            resources_module = None

    if resources_module is None:
        # Fallback to direct import for hermetic test harnesses passing a raw DB pool
        from nce.vertical_modules.resources.allocations import do_get_on_call_allocations

        resources_module = type(
            "FallbackResourcesModule",
            (),
            {"do_get_on_call_allocations": staticmethod(do_get_on_call_allocations)},
        )

    alloc_params = dict(params)
    alloc_params["namespace_id"] = str(ns_id)

    res = await resources_module.do_get_on_call_allocations(engine, alloc_params)

    return {
        "namespace_id": str(ns_id),
        "on_call": res.get("on_call", []),
        "count": res.get("count", 0),
        "query_time": query_time,
        "resources_available": True,
    }
