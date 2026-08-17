"""
Common utility functions shared among domain orchestrators.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("nce-orchestrator.utils")


def _metadata_as_dict(raw: Any) -> dict[str, Any]:
    """Coerce a raw metadata value (dict, JSON string, or None) to a dictionary."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid memories.metadata JSON (prefix=%r)", raw[:80])
            return {}
    return dict(raw)


def _shallow_metadata_delta(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compare two dictionaries and return keys added, removed, or changed."""
    delta: dict[str, dict[str, Any]] = {}
    for k, nv in new.items():
        ov = old.get(k)
        if ov != nv:
            delta[k] = {"from": ov, "to": nv}
    for k, ov in old.items():
        if k not in new:
            delta[k] = {"from": ov, "to": None}
    return delta


def _get_row_field(row: Any, key: str) -> Any:
    """Extract a field from a row dict or object, supporting fallback keys like id for memory_id."""
    if not isinstance(row, dict):
        if hasattr(row, key):
            return getattr(row, key)
        if key == "memory_id" and hasattr(row, "id"):
            return getattr(row, "id")

    if hasattr(row, "get"):
        val = row.get(key)
        if val is not None:
            return val
        if key == "memory_id":
            val = row.get("id")
            if val is not None:
                return val
        return None

    try:
        return row[key]
    except (KeyError, TypeError):
        if key == "memory_id":
            try:
                return row["id"]
            except (KeyError, TypeError):
                pass
        raise


def _build_lineage_modified(old_row: Any, new_row: Any) -> dict[str, Any]:
    """Build a standard lineage modification dictionary, including metadata differences."""
    keys = ("assertion_type", "memory_type", "pii_redacted", "salience")
    transitions: dict[str, dict[str, Any]] = {}
    for k in keys:
        o = _get_row_field(old_row, k)
        n_val = _get_row_field(new_row, k)

        # Coerce values to resolve Enum vs string comparison issues
        o_cmp = o.value if hasattr(o, "value") else o
        n_cmp = n_val.value if hasattr(n_val, "value") else n_val

        if k == "salience":
            o_cmp = float(o_cmp) if o_cmp is not None else None
            n_cmp = float(n_cmp) if n_cmp is not None else None

        if o_cmp != n_cmp:
            transitions[k] = {"from": o_cmp, "to": n_cmp}

    mo = _metadata_as_dict(_get_row_field(old_row, "metadata"))
    mn = _metadata_as_dict(_get_row_field(new_row, "metadata"))

    old_mid = _get_row_field(old_row, "memory_id")
    new_mid = _get_row_field(new_row, "memory_id")

    return {
        "kind": "lineage_linked",
        "source_memory_id": str(old_mid),
        "old_memory_id": str(old_mid),
        "new_memory_id": str(new_mid),
        "transitions": transitions,
        "metadata_delta": _shallow_metadata_delta(mo, mn),
    }


def _lineage_source_id(row: Any) -> str | None:
    """Identify predecessor memory ID from metadata or derived_from list."""
    meta = _metadata_as_dict(
        row.get("metadata") if hasattr(row, "get") else getattr(row, "metadata", None)
    )
    sid = meta.get("source_memory_id")
    if sid:
        return str(sid)
    df = row.get("derived_from") if hasattr(row, "get") else getattr(row, "derived_from", None)
    if df is None:
        return None
    if isinstance(df, str):
        try:
            df = json.loads(df)
        except json.JSONDecodeError:
            mem_id = row.get("memory_id") if hasattr(row, "get") else None
            log.warning(
                "Invalid derived_from JSON on memory %s (prefix=%r)",
                mem_id,
                df[:80],
            )
            return None
    if isinstance(df, (list, tuple)) and len(df) > 0:
        return str(df[0])
    return None


def _validate_path(filepath: str) -> None:
    """Strict OS-agnostic path traversal protection using pathlib.

    Resolves the supplied path and asserts it lies within the
    current working directory — absolute paths and path components (e.g. '..')
    that escape the CWD are rejected.
    """
    try:
        allowed_base = Path.cwd().resolve(strict=True)
        candidate = Path(filepath).resolve(strict=False)

        # Reject if the resolved path doesn't start with CWD
        if not candidate.is_relative_to(allowed_base):
            raise ValueError(f"Path traversal detected: {filepath!r}")

        # Secondary check: reject raw strings that try to escape before resolution
        if ".." in Path(filepath).parts:
            if not candidate.is_relative_to(allowed_base):
                raise ValueError(f"Path traversal detected (..): {filepath!r}")
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"Invalid filepath: {filepath!r}") from exc
