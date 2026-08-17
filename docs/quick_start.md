> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Quick Start Guide

Welcome to **NCE v1.0**. This guide covers running the engine **from source** (Docker + Python), selecting a deployment posture, and connecting the MCP server to Cursor or Claude Desktop. Package installers, when available, layer on top of the same `server.py` and Compose stack.

## 1. Prerequisite: stack and repo

- Install **Docker Desktop** (or compatible engine) and **Python 3.10+**.
- Clone the repository and install dependencies (`pip install -r requirements.txt` or your env manager).

## 2. Deployment posture (conceptual)

### Local (default dev)

- Run **PostgreSQL**, **MongoDB**, **Redis**, and **MinIO** via the repo’s `docker-compose` (see [deploy/README.md](https://github.com/sindrehaugen/NCE/blob/main/deploy/README.md)).
- Copy `.env.example` → `.env` and set connection strings.

### Multi-user

- Same services, hosted for a team: enforce **namespace isolation**, **HMAC/JWT auth**, and **quotas** in production (see `admin_server.py`, tests under `tests/test_a2a.py` and `tests/test_quotas.py`).

### Cloud

- Use managed equivalents for each store; point `.env` at cloud URIs. No code changes required for the v1.0 paths.

## 3. Connect to your LLM client

NCE operates as a Model Context Protocol (MCP) server over standard input/output (`stdio`). Once installed, configure your client to point to the `server.py` entrypoint.

### Cursor

1. Open Cursor Settings -> **MCP** -> **Add Server**.
2. Set the type to `command`.
3. Configure the server:
   - **Name**: `nce-memory`
   - **Command**: `python`
   - **Args**: `/absolute/path/to/NCE/server.py` (Use double backslashes `\\` or forward slashes `/` on Windows).

### Claude Desktop

Edit your `claude_desktop_config.json` file:
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the following configuration:

```json
{
  "mcpServers": {
    "nce-memory": {
      "command": "python",
      "args": ["/absolute/path/to/NCE/server.py"]
    }
  }
}
```

*Note: In shared deployments, manage **secrets** via your platform; do not hardcode database passwords in MCP JSON when avoid.*

## 4. Verify installation

Once connected, restart your LLM client and ask:
> "What MCP tools do you have available for NCE?"

You should see tools such as `semantic_search` (with optional `as_of`), `graph_search`, `store_memory`, `index_code_file`, bridge tools, salience, contradictions, replay, and migration tools — see `nce/mcp_stdio_tools.py` (tool definitions and the `TOOLS` list) and `nce/tool_registry.py` (dispatch metadata and admin/cache flags) for the authoritative registry.

## 5. Administrative Operations

Certain tools in NCE (like `manage_namespace`, `rotate_signing_key`, or `get_health`) require administrative privileges. To use these tools via an MCP client:

1. **Local dev bypass** — set `NCE_ADMIN_OVERRIDE=true` in the server environment. This skips the `admin_api_key` check entirely (never enable in production).
2. **Production / normal operation** — set `NCE_ADMIN_API_KEY` on the server and pass `"admin_api_key": "<value>"` in each admin tool's arguments. The check is a constant-time comparison against `NCE_ADMIN_API_KEY` enforced in `nce/auth.py`.

Note: In production environments, admin tools are further protected by the `admin_server.py` HMAC layer and rate limiting. `NCE_ADMIN_OVERRIDE` is explicitly blocked when `NCE_ENV=prod`.

---

## Architecture reference

**v1.0 runtime** (temporal engine, A2A protocol, cognitive / background workers, Mermaid diagrams): [architecture-v1.md](./architecture-v1.md).

Phase **0.1** / **0.2** (multi-tenant model, signing): [multi_tenancy.md](./multi_tenancy.md) and [signing.md](./signing.md).

**Docker Compose** defaults: [deploy/README.md](https://github.com/sindrehaugen/NCE/blob/main/deploy/README.md).
