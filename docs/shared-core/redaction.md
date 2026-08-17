> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Doc 62 — Shared Core Redaction Guide

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The NCE Shared Core Redaction model (component **C8**) provides field-level security projection for external-facing data boundaries. Working in tandem with Row-Level Security (RLS), the redactor guarantees that sensitive internal fields—such as margins, cost figures, and internal workflow statuses—never cross to external surfaces (partners, public quotes, customer portals, or marketing drafts).

This guide outlines the system design, the default-deny posture, JSON configuration files, RLS integration mechanics, and log-level database connection string (DSN) masking.

---

## 1. System Overview & Contract C8

As defined in the Shared-Core Foundation, **Contract C8** governs the field-level half of external-surface security:
1. **Default-Deny Posture**: No field is permitted on an external surface unless it is explicitly allow-listed. There are no deny-lists.
2. **Omission Safety**: Any newly added database columns or node properties are automatically hidden by default until an administrator explicitly updates the corresponding surface schema.
3. **Internal Data Sanitization**: High-sensitivity fields (such as `margin`, `cost`, and `internal-status`) are prohibited from public/partner schema allow-lists by construction.
4. **Pure Projection**: The core redactor is a side-effect-free, I/O-free utility. It performs standard memory-to-memory dict filtering and uses thread-safe, in-process configuration caching.

### Dual-Layer Security Boundary
The security posture of NCE combines row isolation (C3) with field isolation (C8):

```mermaid
graph TD
    Query[External Request] --> SQL[Database Query]
    SQL --> RLS{C3: Row-Level Security}
    RLS -- Reject --> Err[Access Denied]
    RLS -- Allow --> Rows[(Filtered Rows)]
    Rows --> Load[Retrieve Dict / kg_node]
    Load --> Redact{C8: project node, surface}
    Redact --> AllowList[Check surface-redaction.json]
    AllowList --> Projection[Drop unlisted fields]
    Projection --> SafeJSON[Sanitized Output Payload]
```

- **Row-Level Security (C3)**: Filters *which* rows a principal can access based on tenant (`namespace_id`) and external scope (`nce.external_scope_id` / `get_nce_external_scope()`).
- **Field-Level Redactor (C8)**: Filters *what* columns/keys are returned within those rows, preventing structural information leaks (e.g. IDOR defense-in-depth).

---

## 2. Architectural Design & Code Implementation

The C8 field redactor is implemented in the core directory:
- **Module Location**: `nce.redaction.redactor`
- **Configuration Directory**: `nce/config_data/redaction/`

### Python Implementation (`redactor.py`)

The core redactor exposes the `project()` function and raises `UnknownSurfaceError` when an unrecognized surface is requested.

```python
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
```

### Key Safety Behaviors
- **Config Loading Failures**: If the requested `<surface>-redaction.json` file is missing, the redactor raises `UnknownSurfaceError`. It *never* defaults to a full copy (no open passthrough).
- **Novel Field Isolation**: If a database migration adds a column (e.g., `future_secret_field`) to a node dictionary, it is immediately ignored by the redactor since it cannot exist in cached allow-lists.
- **No In-Flight Fabrication**: If a field is present in the allow-list configuration but does not exist in the source dictionary, it is omitted from the return payload (no empty key generation or null placeholder injection).

---

## 3. Configuration Surfaces

Surface properties are declared inside static JSON arrays located in `nce/config_data/redaction/`.

### 1. Partner Surface (`partner-redaction.json`)
The Partner surface is used to render hardware and network inventory data back to downstream vendors and contractors (e.g., in Field Tech or Vendor management modules).

| Allowed Field | Type | Description |
| :--- | :--- | :--- |
| `id` | String | Unique identifier of the graph node |
| `node_type` | String | Type of the entity (e.g., `device`) |
| `label` | String | User-facing display label |
| `description` | String | Operational description |
| `category` | String | Broad classification group |
| `manufacturer` | String | Hardware manufacturer |
| `model` | String | Model designation |
| `part_number` | String | Part identifier / SKU |
| `serial_number` | String | Unique hardware serial number |
| `status` | String | Lifecycle state (e.g., `active`) |
| `location` | String | Logical or spatial location name |
| `site` | String | Physical campus or facility |
| `rack` | String | Rack identifier |
| `unit` | String | Rack unit placement |
| `interface` | String | Network interface identifier |
| `ip_address` | String | Assigned IPv4 or IPv6 address |
| `mac_address` | String | Physical hardware address |
| `firmware_version`| String | Running software version |
| `hardware_version`| String | Physical build revision |
| `warranty_expiry` | String | Expiration date of active coverage |
| `install_date` | String | Initial deployment date |
| `tags` | Array | Categorization tags |
| `namespace_id` | String | Scope identifier (RLS metadata tag) |

### 2. Public Quote Surface (`public-quote-redaction.json`)
The Public Quote surface is exposed when generating commercial estimates for external customers. It excludes all internal pricing structures, serial numbers, and physical infrastructure fields.

| Allowed Field | Type | Description |
| :--- | :--- | :--- |
| `id` | String | Unique identifier of the quote line |
| `node_type` | String | Node classification (e.g., `quote_line`) |
| `label` | String | Commercial line-item label |
| `description` | String | Sales description of the item |
| `category` | String | Product grouping category |
| `manufacturer` | String | Hardware vendor |
| `model` | String | Model identifier |
| `part_number` | String | Manufacturer part number |
| `quantity` | Number | Quantity ordered |
| `unit_price` | Number | Customer-facing sales unit price |
| `currency` | String | Operational currency ISO code (e.g., `NOK`) |
| `lead_time_days` | Number | Estimated fulfillment window |
| `availability` | String | Stock status (e.g., `in-stock`) |
| `tags` | Array | Public tagging fields |
| `namespace_id` | String | Scope identifier (RLS metadata tag) |

---

## 4. Row-Level Security (RLS) Metadata Interaction

A critical design pattern in the C8 redactor is the inclusion of `namespace_id` in both allow-list configuration files. 

### Why `namespace_id` is Exposed
- **Tenant Validation**: Client applications (such as portals or partner integrations) must verify tenant alignment. Returning `namespace_id` allows clients to check that the records match their designated tenancy.
- **IDOR Prevention**: If a client receives a payload, they can verify that the returned tenant ID matches their own authentication token scope, providing a client-side sanity check.

### Separation of Enforcement
Although `namespace_id` is safe to expose, RLS at the database engine serves as the absolute boundary:
1. **The Row Boundary**: A SQL session sets `get_nce_namespace()` based on the verified tenant token. The query cannot retrieve any row belonging to a different `namespace_id`, preventing data traversal across tenants.
2. **The Field Boundary**: The redactor strips out unlisted fields (such as vendor cost markup or inner tracking state).
3. **Cross-Tenant IDOR Protection**: Even if a developer accidentally leaks an internal primary key or maps a route without scoping it to the active tenant in SQL, RLS blocks the query at the DB driver level. Redaction is applied strictly to rows that successfully clear the C3 RLS engine.

---

## 5. Log & Exception Credentials Scrubbing

To prevent database password leakage through system logs and stack traces, the core config subsystem (`nce.config`) executes automated DSN and URI sanitization.

### DSN Redaction (`redact_dsn`)
Any connection strings parsed during configuration loading or system diagnostics are passed through `redact_dsn()` to replace password elements with a masked string (`***`).

```python
from urllib.parse import urlparse, urlunparse

def redact_dsn(dsn: str) -> str:
    """Mask the password component of a database/service URI.

    Handles the standard ``scheme://user:password@host/path`` format
    (including ``mongodb+srv``, ``redis://:password@host``).
    Returns the URI with the password replaced by ``***``.
    If parsing fails, returns ``<redacted>`` so the raw DSN is never
    accidentally surfaced in log or exception messages.
    """
    try:
        parsed = urlparse(dsn)
        if parsed.password:
            # Rebuild with masked password
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            if parsed.username:
                netloc = f"{parsed.username}:***@{netloc}"
            else:
                # Password-only auth (Redis format: redis://:pass@host)
                netloc = f":***@{netloc}"
            return urlunparse(parsed._replace(netloc=netloc))
        return dsn
    except Exception:
        return "<redacted>"
```

### Inline Exception Scrubbing (`redact_secrets_in_text`)
Some third-party drivers (such as `asyncpg`, `redis-py`, or `pymongo`) append the fully-detailed connection DSN inside error exception texts. The config engine parses log and exception payloads through `redact_secrets_in_text()` to catch inline URI credentials.

```python
import re

# Match ``scheme://user:password@`` for common datastore / cache URI schemes.
_RE_URI_CREDS = re.compile(
    r"(?P<prefix>(?:mongodb\+srv|mongodb|postgresql|postgres|redis|rediss)://)"
    r"(?P<user>[^:/?#\s]+):(?P<password>[^@/?#\s]+)@",
    re.IGNORECASE,
)
# Redis/Mongo ``scheme://:password@host`` (no username).
_RE_URI_PASS_ONLY = re.compile(
    r"(?P<prefix>(?:mongodb\+srv|mongodb|postgresql|postgres|redis|rediss)://)"
    r":(?P<password>[^@/?#\s]+)@",
    re.IGNORECASE,
)

def redact_secrets_in_text(text: str) -> str:
    """Scrub ``user:password@`` fragments from arbitrary log/exception strings.

    Database clients sometimes echo the full DSN in connection errors. This
    regex pass catches embedded URIs that redact_dsn would parse in
    isolation but appear inside longer messages.
    """
    if not text:
        return text
    scrubbed = _RE_URI_CREDS.sub(r"\g<prefix>\g<user>:***@", text)
    return _RE_URI_PASS_ONLY.sub(r"\g<prefix>:***@", scrubbed)
```

### Behavior in Practice
- **Standard DSN format**: `postgresql://mcp_user:secret_pass@db.example.com:5432/memory_meta`
  - Sanitized: `postgresql://mcp_user:***@db.example.com:5432/memory_meta`
- **Redis password-only format**: `redis://:myredissecret@redis.internal:6379/0`
  - Sanitized: `redis://:***@redis.internal:6379/0`
- **Fail-Closed**: If `urlparse` crashes on a malformed, invalid, or corrupted connection string, `redact_dsn` immediately aborts and returns `"<redacted>"`, ensuring that no un-parseable string containing raw credentials can leak.
