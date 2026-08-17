"""Allow-list field redactor (C8) — pure projection, no DB/HTTP.

Contract:
    project(node, surface) -> dict

    Returns only the fields named in ``<surface>-redaction.json``.
    Fields absent from the allow-list are silently dropped (omission-safety).
    An unknown surface raises ``UnknownSurfaceError`` — never an open passthrough.

Security invariant (by construction, not by check):
    ``margin``, ``cost``, and ``internal-status`` are never present in any
    allow-list JSON.  The projection loop only copies what is *explicitly*
    listed; anything absent from the list — including any newly added node field
    — is hidden by default.
"""

import json
from pathlib import Path
from typing import Any


class UnknownSurfaceError(ValueError):
    """Raised when ``surface`` has no corresponding allow-list config file."""


# In-process cache: surface name -> frozenset of allowed field names
_ALLOW_LIST_CACHE: dict[str, frozenset[str]] = {}

_CONFIG_DIR = Path(__file__).parent.parent / "config_data" / "redaction"


def _load_allow_list(surface: str) -> frozenset[str]:
    """Load and cache the allow-list for *surface* from its JSON config file.

    Raises:
        UnknownSurfaceError: if no config file exists for *surface*.
        json.JSONDecodeError: if the config file is malformed.
    """
    if surface in _ALLOW_LIST_CACHE:
        return _ALLOW_LIST_CACHE[surface]

    config_path = _CONFIG_DIR / f"{surface}-redaction.json"
    if not config_path.exists():
        raise UnknownSurfaceError(
            f"No redaction config found for surface {surface!r}. Expected: {config_path}"
        )

    with open(config_path, encoding="utf-8") as fh:
        data = json.load(fh)

    allowed: frozenset[str] = frozenset(data.get("allowed_fields", []))
    _ALLOW_LIST_CACHE[surface] = allowed
    return allowed


def project(node: dict[str, Any], surface: str) -> dict[str, Any]:
    """Return a redacted view of *node* containing only the surface allow-list fields.

    This is a pure function: no DB, no HTTP, no side effects beyond the
    in-process config cache.

    Args:
        node:    Arbitrary dict of node fields (e.g. a ``kg_nodes`` row dict).
        surface: Surface identifier matching a ``<surface>-redaction.json`` file
                 (e.g. ``"partner"``, ``"public-quote"``).

    Returns:
        A new dict containing only the keys present in both *node* and the
        surface allow-list.  An empty dict when no allow-listed field is found.

    Raises:
        UnknownSurfaceError: if *surface* has no allow-list config.
    """
    allowed = _load_allow_list(surface)
    return {key: value for key, value in node.items() if key in allowed}
