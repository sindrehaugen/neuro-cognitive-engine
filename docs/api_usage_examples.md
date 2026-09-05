> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# API Usage Examples

End-to-end copy-paste examples for the NCE HTTP REST API.  
Every endpoint, field name, and header shown here is verified against `main` @ 7304330.

---

## Contents

- [Authentication scheme](#authentication-scheme)
- [Signing helper implementations](#signing-helper-implementations)
- [Example 1 — Health check (`GET /api/health`)](#example-1--health-check-get-apihealth)
- [Example 2 — Semantic search (`POST /api/search`)](#example-2--semantic-search-post-apisearch)
- [Example 3 — List namespaces (`GET /api/admin/namespaces`)](#example-3--list-namespaces-get-apiadminnamespaces)
- [Common error shapes](#common-error-shapes)
- [Environment variables reference](#environment-variables-reference)

---

## Authentication scheme

All paths under `/api/` are protected by `HMACAuthMiddleware` (see `nce/auth.py`).  
Three headers are required on every request:

| Header | Format | Notes |
|---|---|---|
| `X-NCE-Timestamp` | Unix epoch seconds (integer UTC) | Must be within ±90 s of server clock (`NCE_CLOCK_SKEW_TOLERANCE_S`) |
| `Authorization` | `HMAC-SHA256 <hex_signature>` | Lowercase hex; constant-time compared |
| `X-NCE-Nonce` | Unique string (UUIDv4 or random token) | Stored atomically in Redis SETNX; rejected on reuse |

### Canonical message

```
canonical_message = METHOD\nPATH\nTIMESTAMP[\nSHA256_HEX(raw_body)]
```

- `METHOD` — uppercase HTTP verb, e.g. `GET`, `POST`
- `PATH` — URL path only, no query string, e.g. `/api/search`
- `TIMESTAMP` — the same integer value sent in `X-NCE-Timestamp`
- `SHA256_HEX(raw_body)` — hex digest of the raw request body bytes; **omitted entirely** for requests with an empty body (GET, DELETE, etc.)

> **Note on Nonce.** `X-NCE-Nonce` is sent as a standalone header and checked via Redis SETNX for distributed replay protection. It is **not** part of the HMAC `canonical_message`.

### Signature

```
signature = HMAC-SHA256(key=NCE_API_KEY, msg=canonical_message)
           encoded as lowercase hexadecimal
```

The shared secret is the value of the `NCE_API_KEY` environment variable.

### Replay protection & Nonce Store Mechanics

1. **Timestamp tolerance**: Timestamps outside ±90 seconds (`NCE_CLOCK_SKEW_TOLERANCE_S`, default `90`, shrunk from 300 s in Batch 116) are rejected with JSON-RPC error code `-32002` (`replay_or_clock_skew`).
2. **Distributed Nonce Store (`NonceStore`)**: The caller-supplied `X-NCE-Nonce` value is atomically recorded in Redis via `SET key 1 NX PX ttl` (key `nce:nonce:<nonce>`, TTL = 2 × drift window = 180 s); a replayed nonce within the drift window is rejected with `-32002` (`replay_nonce_conflict`).

> [!WARNING]
> **Production Default Configuration Hazard (Default Prod Rejections)**:  
> - `NCE_HMAC_NONCE_REQUIRED` defaults to **`true`**.  
> - `optional_hmac_nonce_store()` returns `None` unless `NCE_DISTRIBUTED_REPLAY` is explicitly set to **`true`** (default: `false`) and `REDIS_URL` is configured.  
> - When `NCE_ENV=prod` (or `production`), `_check_nonce` in `nce/auth.py` enforces a strict **fail-closed** policy: if `_nonce_store` is `None` or Redis is unreachable, it raises `nonce_store_unavailable` (code `-32002`, returning HTTP 401 / JSON-RPC error).  
> 
> **Result**: Under default settings in production, **all `/api/*` requests will be rejected** unless `NCE_DISTRIBUTED_REPLAY=true` and `REDIS_URL` are configured, or `NCE_HMAC_NONCE_REQUIRED=false` is explicitly set during non-distributed or transitional deployments.

### Optional namespace/agent headers

| Header | Format | Effect |
|---|---|---|
| `X-NCE-Namespace-ID` | UUID string | Scopes request to a tenant namespace; enforces RLS |
| `X-NCE-Agent-ID` | String ≤ 128 chars | Agent identifier; defaults to `"default"` |

---

## Signing helper implementations

### Shell function (bash)

```bash
# Usage: nce_sign METHOD PATH [body_file] [nonce]
# Requires: openssl, xxd, GNU coreutils, (uuidgen or openssl for nonce)
nce_sign() {
  local method="${1:?METHOD required}"
  local path="${2:?PATH required}"
  local body_file="${3:-}"
  local nonce="${4:-}"
  local ts
  ts=$(date +%s)

  if [[ -z "$nonce" ]]; then
    if command -v uuidgen >/dev/null 2>&1; then
      nonce=$(uuidgen | tr '[:upper:]' '[:lower:]')
    else
      nonce=$(openssl rand -hex 16)
    fi
  fi

  local parts
  parts="${method^^}\n${path}\n${ts}"

  if [[ -n "$body_file" && -s "$body_file" ]]; then
    local body_hash
    body_hash=$(openssl dgst -sha256 -hex < "$body_file" | awk '{print $2}')
    parts="${parts}\n${body_hash}"
  fi

  local canonical
  canonical=$(printf "%b" "$parts")

  local sig
  sig=$(printf "%s" "$canonical" \
        | openssl dgst -sha256 -hmac "${NCE_API_KEY:?NCE_API_KEY not set}" \
        | awk '{print $2}')

  echo "X-NCE-Timestamp: ${ts}"
  echo "Authorization: HMAC-SHA256 ${sig}"
  echo "X-NCE-Nonce: ${nonce}"
}
```

### Python helper (`httpx` / `requests`)

```python
import hashlib
import hmac
import os
import time
import uuid


def nce_sign_headers(
    method: str,
    path: str,
    body: bytes = b"",
    *,
    api_key: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return HMAC auth headers for one NCE REST request.

    Args:
        method:  HTTP verb, e.g. "GET" or "POST".
        path:    URL path only, e.g. "/api/search".
        body:    Raw request body bytes. Pass b"" for bodyless requests.
        api_key: Override NCE_API_KEY env-var (useful in tests).
        nonce:   Optional custom nonce string; defaults to a fresh UUIDv4 hex.

    Returns:
        Dict with three keys: "X-NCE-Timestamp", "Authorization", and "X-NCE-Nonce".
    """
    key = (api_key or os.environ["NCE_API_KEY"]).encode()
    ts = int(time.time())
    nonce_val = nonce or uuid.uuid4().hex

    parts = [method.upper(), path, str(ts)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())

    canonical = "\n".join(parts).encode()
    sig = hmac.new(key, canonical, hashlib.sha256).hexdigest()

    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
        "X-NCE-Nonce": nonce_val,
    }
```

### TypeScript helper (`fetch`)

```typescript
async function nceSignHeaders(
  method: string,
  path: string,
  body: string | null = null,
  apiKey?: string,
  nonce?: string,
): Promise<Record<string, string>> {
  const key = apiKey ?? process.env.NCE_API_KEY;
  if (!key) throw new Error("NCE_API_KEY is not set");

  const ts = Math.floor(Date.now() / 1000);
  const nonceVal =
    nonce ??
    (typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : Math.random().toString(36).substring(2) + Math.random().toString(36).substring(2));
  const parts: string[] = [method.toUpperCase(), path, String(ts)];

  if (body) {
    // SHA-256 the raw body string (UTF-8 encoded)
    const encoder = new TextEncoder();
    const bodyBuf = encoder.encode(body);
    const hashBuf = await crypto.subtle.digest("SHA-256", bodyBuf);
    const hashHex = Array.from(new Uint8Array(hashBuf))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    parts.push(hashHex);
  }

  const canonical = parts.join("\n");
  const encoder = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sigBuf = await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(canonical));
  const sig = Array.from(new Uint8Array(sigBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  return {
    "X-NCE-Timestamp": String(ts),
    Authorization: `HMAC-SHA256 ${sig}`,
    "X-NCE-Nonce": nonceVal,
  };
}
```

---

## Example 1 — Health check (`GET /api/health`)

**Handler:** `get_health` in `nce/admin_handlers/health.py`  
**Auth required:** yes (HMAC headers)  
**Body:** none

### curl

```bash
TS=$(date +%s)
NONCE=$(openssl rand -hex 16)
CANONICAL=$(printf 'GET\n/api/health\n%s' "$TS")
SIG=$(printf '%s' "$CANONICAL" | openssl dgst -sha256 -hmac "$NCE_API_KEY" | awk '{print $2}')

curl -s \
  -H "X-NCE-Timestamp: $TS" \
  -H "Authorization: HMAC-SHA256 $SIG" \
  -H "X-NCE-Nonce: $NONCE" \
  "http://localhost:8003/api/health"
```

### Python (httpx)

```python
import httpx

BASE_URL = "http://localhost:8003"

headers = nce_sign_headers("GET", "/api/health")
resp = httpx.get(f"{BASE_URL}/api/health", headers=headers)
resp.raise_for_status()
print(resp.json())
```

### Python (requests)

```python
import requests

BASE_URL = "http://localhost:8003"

headers = nce_sign_headers("GET", "/api/health")
resp = requests.get(f"{BASE_URL}/api/health", headers=headers)
resp.raise_for_status()
print(resp.json())
```

### TypeScript (fetch)

```typescript
const BASE_URL = "http://localhost:8003";

async function getHealth() {
  const headers = await nceSignHeaders("GET", "/api/health");
  const res = await fetch(`${BASE_URL}/api/health`, { headers });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

getHealth().then(console.log);
```

### Response shape

```json
{
  "status": "ok",
  "timestamp": "2026-06-20T12:00:00+00:00",
  "security": {
    "master_key": "valid",
    "signing_key_decryption": "valid",
    "bounded_chain_sample": "valid"
  },
  "databases": {
    "mongo": "up",
    "postgres": "up",
    "redis": "up",
    "rls_read": "valid"
  },
  "queues": {
    "default": "0 pending jobs",
    "high_priority": "0 pending jobs",
    "batch_processing": "0 pending jobs"
  },
  "cognitive": {
    "backend": "auto",
    "backend_type": "OllamaBackend",
    "engine": "up"
  }
}
```

`databases` values are `"up"` or `"down"` for connectivity probes; `rls_read` is added dynamically and is `"valid"` or `"failed"`.
`security` values degrade to `"missing/invalid"`, `"failed"`, `"corrupted"`, or `"no_active_key"` under faults.
`queues` values are per-lane pending job counts (e.g. `"3 pending jobs"`); the entire `queues` probe is skipped and values remain `"unknown"` if the sync Redis client (`redis_sync_client`) is unavailable.
`cognitive.backend_type` is the embedding backend class name (e.g. `"OllamaBackend"`) and is only present when the embedding probe succeeds — it is absent if `get_backend()` or the HTTP health check raises.
`cognitive.engine` may be `"unknown"`, `"up"`, `"down (<status>)"`, or `"unreachable (<ExcType>)"`.
If any database reports `"down"` the engine dispatches an alert and the HTTP status may be `200` — check the `databases` map, not just the HTTP code.

---

## Example 2 — Semantic search (`POST /api/search`)

**Handler:** `api_search` in `nce/admin_handlers/health.py`  
**Auth required:** yes (HMAC headers)  
**Body:** JSON

### Request body fields

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `namespace_id` | string (UUID) | yes | — | Tenant namespace UUID |
| `agent_id` | string | yes | — | Agent identifier |
| `query` | string | yes | — | Free-text semantic query |
| `top_k` | integer | no | `5` | Maximum results (alias for `limit`) |
| `limit` | integer | no | `5` | Maximum results |
| `offset` | integer | no | `0` | Pagination offset |
| `as_of` | string (ISO 8601 UTC) | no | — | Time-travel: return memories valid at this timestamp |

### curl

```bash
NS_ID="00000000-0000-0000-0000-000000000001"
BODY='{"namespace_id":"'"$NS_ID"'","agent_id":"my-agent","query":"network topology","top_k":3}'

TS=$(date +%s)
NONCE=$(openssl rand -hex 16)
BODY_HASH=$(printf '%s' "$BODY" | openssl dgst -sha256 | awk '{print $2}')
CANONICAL=$(printf 'POST\n/api/search\n%s\n%s' "$TS" "$BODY_HASH")
SIG=$(printf '%s' "$CANONICAL" | openssl dgst -sha256 -hmac "$NCE_API_KEY" | awk '{print $2}')

curl -s \
  -X POST \
  -H "Content-Type: application/json" \
  -H "X-NCE-Timestamp: $TS" \
  -H "Authorization: HMAC-SHA256 $SIG" \
  -H "X-NCE-Nonce: $NONCE" \
  -d "$BODY" \
  "http://localhost:8003/api/search"
```

### Python (httpx)

```python
import json
import httpx

BASE_URL = "http://localhost:8003"

payload = {
    "namespace_id": "00000000-0000-0000-0000-000000000001",
    "agent_id": "my-agent",
    "query": "network topology",
    "top_k": 3,
}
body_bytes = json.dumps(payload).encode()

headers = nce_sign_headers("POST", "/api/search", body=body_bytes)
headers["Content-Type"] = "application/json"

resp = httpx.post(f"{BASE_URL}/api/search", content=body_bytes, headers=headers)
resp.raise_for_status()
data = resp.json()
for hit in data["results"]:
    print(hit["memory_id"], hit["score"], hit["raw_data"])
```

### TypeScript (fetch)

```typescript
const BASE_URL = "http://localhost:8003";

async function semanticSearch(namespaceId: string, query: string, topK = 5) {
  const body = JSON.stringify({
    namespace_id: namespaceId,
    agent_id: "my-agent",
    query,
    top_k: topK,
  });

  const authHeaders = await nceSignHeaders("POST", "/api/search", body);
  const res = await fetch(`${BASE_URL}/api/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json() as Promise<{ results: SearchHit[] }>;
}

interface SearchHit {
  memory_id: string;
  payload_ref: string;
  score: number;
  raw_data: string | null;
  salience_score: number;
  last_reinforced_at: string | null;
  confidence: number;
  stale: boolean;
  reranker_score: number | null;
}
```

### Response shape

```json
{
  "results": [
    {
      "memory_id": "11111111-1111-1111-1111-111111111111",
      "payload_ref": "abcdef1234567890abcdef1234567890abcdef12",
      "score": 0.92,
      "raw_data": "Core switch M4350 at 10.1.0.1 serves VLAN 100.",
      "salience_score": 0.88,
      "last_reinforced_at": "2026-06-19T08:30:00+00:00",
      "confidence": 0.81,
      "stale": false,
      "reranker_score": null
    }
  ]
}
```

> **Execution note.** `POST /api/search` requires a running NCE engine with PostgreSQL, MongoDB, and an embedding model configured. In environments without those services the handler returns `503 {"error": "Engine not connected"}`.

---

## Example 3 — List namespaces (`GET /api/admin/namespaces`)

**Handler:** `api_admin_namespaces_list` in `nce/admin_handlers/fleet.py`  
**Auth required:** yes (HMAC headers)  
**Body:** none

### Query parameters

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `slug_prefix` | string | — | Filter by namespace slug prefix (ILIKE) |
| `page` | integer | `1` | 1-based page number |
| `limit` | integer | `500` | Max 500 |

### curl

```bash
TS=$(date +%s)
NONCE=$(openssl rand -hex 16)
PATH_QS="/api/admin/namespaces?page=1&limit=10"
# Note: canonical PATH does not include query string
CANONICAL=$(printf 'GET\n/api/admin/namespaces\n%s' "$TS")
SIG=$(printf '%s' "$CANONICAL" | openssl dgst -sha256 -hmac "$NCE_API_KEY" | awk '{print $2}')

curl -s \
  -H "X-NCE-Timestamp: $TS" \
  -H "Authorization: HMAC-SHA256 $SIG" \
  -H "X-NCE-Nonce: $NONCE" \
  "http://localhost:8003${PATH_QS}"
```

> **Important.** The HMAC canonical path is the URL path **without** the query string — `/api/admin/namespaces`, not `/api/admin/namespaces?page=1&limit=10`.

### Python (httpx)

```python
import httpx

BASE_URL = "http://localhost:8003"

headers = nce_sign_headers("GET", "/api/admin/namespaces")
resp = httpx.get(
    f"{BASE_URL}/api/admin/namespaces",
    params={"page": 1, "limit": 10},
    headers=headers,
)
resp.raise_for_status()
data = resp.json()
print(f"Total namespaces: {data['total']}")
for ns in data["namespaces"]:
    print(ns["id"], ns["slug"])
```

### TypeScript (fetch)

```typescript
const BASE_URL = "http://localhost:8003";

async function listNamespaces(page = 1, limit = 10) {
  // Sign using the path only — no query string in canonical
  const authHeaders = await nceSignHeaders("GET", "/api/admin/namespaces");
  const url = `${BASE_URL}/api/admin/namespaces?page=${page}&limit=${limit}`;
  const res = await fetch(url, { headers: authHeaders });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<NamespaceListResponse>;
}

interface Namespace {
  id: string;
  slug: string;
  parent_id: string | null;
  created_at: string | null;
  metadata: Record<string, unknown>;
}

interface NamespaceListResponse {
  namespaces: Namespace[];
  items: Namespace[];   // same slice, kept for UI compatibility
  page: number;
  limit: number;
  total: number;
}
```

### Response shape

```json
{
  "namespaces": [
    {
      "id": "00000000-0000-0000-0000-000000000001",
      "slug": "acme-corp",
      "parent_id": null,
      "created_at": "2026-01-15T08:00:00+00:00",
      "metadata": {}
    }
  ],
  "items": [ /* same slice */ ],
  "page": 1,
  "limit": 10,
  "total": 1
}
```

---

## Common error shapes

All auth errors return JSON-RPC 2.0 error objects (defined in `nce/auth.py`):

| HTTP status | Error code | `data` | Cause |
|---|---|---|---|
| `401` | `-32001` | `{"reason":"missing_auth_headers"}` | `X-NCE-Timestamp` or `Authorization` header absent |
| `401` | `-32001` | `{"reason":"invalid_authorization_scheme"}` | Scheme is not `HMAC-SHA256` |
| `401` | `-32001` | `{"reason":"malformed_auth_headers"}` | Timestamp is not a valid integer |
| `401` | `-32001` | `{"reason":"invalid_signature"}` | HMAC signature mismatch |
| `401` | `-32001` | `{"reason":"nonce_missing"}` | `X-NCE-Nonce` header absent when nonce is required and Redis is up |
| `401` | `-32002` | `{"reason":"replay_or_clock_skew"}` | Timestamp outside ±90 s window |
| `401` | `-32002` | `{"reason":"replay_nonce_conflict"}` | Nonce reused within drift window (Redis NonceStore) |
| `401` | `-32002` | `{"reason":"nonce_store_unavailable"}` | Nonce required in production (`NCE_ENV=prod`) but `NonceStore` is not configured or Redis is unreachable |
| `401` | `-32003` | `{"reason":"..."}` | `X-NCE-Namespace-ID` is not a valid UUID |
| `422` | — | — | Request body missing required fields (`namespace_id`, `agent_id`, `query`) |
| `429` | — | — | Quota exceeded for the calling namespace |
| `503` | — | — | Engine not connected (service starting up) |

Example auth error response body:

```json
{
  "jsonrpc": "2.0",
  "id": null,
  "error": {
    "code": -32002,
    "message": "Request timestamp out of acceptable range",
    "data": {"reason": "replay_or_clock_skew"}
  }
}
```

---

## Environment variables reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `NCE_API_KEY` | yes (production) | `""` | Shared HMAC secret; empty key makes all `/api/` routes return `401` |
| `NCE_CLOCK_SKEW_TOLERANCE_S` | no | `90` | Replay window in seconds (±N from server clock, default 90 s) |
| `NCE_HMAC_NONCE_REQUIRED` | no | `true` | When true, enforces `X-NCE-Nonce` header and replay store check |
| `NCE_DISTRIBUTED_REPLAY` | no | `false` | When true, enables Redis `NonceStore` for cluster-wide replay prevention (requires `REDIS_URL`) |

The admin HTTP server listens on port **8003** by default (`admin_server.py` → `uvicorn.run(app, host="0.0.0.0", port=8003)`).

---

*Source files verified: `nce/auth.py`, `nce/admin_handlers/health.py`, `nce/admin_handlers/fleet.py`, `nce/models.py`, `nce/config.py`, `nce/admin_app.py`, `docs/API.md` — all on `main` @ 7304330.*
