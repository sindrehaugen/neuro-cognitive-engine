"""Streamable HTTP /mcp client proxy (zero-secret client CLI).

Connects an MCP client expecting a stdio transport to the zero-trust
Streamable HTTP /mcp gateway via HTTP POST/SSE streaming, without
requiring or accepting any infrastructure secrets (database credentials
or master encryption keys).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

logger = logging.getLogger("nce.mcp_client_proxy")

FORBIDDEN_BACKEND_SECRETS: tuple[str, ...] = (
    "NCE_MASTER_KEY",
    "PG_DSN",
    "DATABASE_URL",
    "MONGO_URI",
    "MINIO_SECRET_KEY",
)


def validate_proxy_env(env: Mapping[str, str] | None = None) -> None:
    """Validate that client proxy environment is free of infrastructure secrets.

    Raises:
        RuntimeError: If any backend database URI or master key is found in the
            proxy process environment or configuration map.
    """
    if env is None:
        env = os.environ

    leaked = [secret for secret in FORBIDDEN_BACKEND_SECRETS if env.get(secret)]
    if leaked:
        raise RuntimeError(
            f"Zero-secret client proxy violation: forbidden backend secrets present in env: {leaked}. "
            "Clients must not hold database connection strings or master encryption keys."
        )


def main() -> int:
    """Entrypoint for nce-mcp-proxy CLI stdio-to-HTTP bridge.

    Reads lines of JSON-RPC from stdin, forwards to NCE_MCP_HTTP_URL via
    HTTP POST with Bearer token authentication and session headers, and writes
    the JSON-RPC response back to stdout.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    validate_proxy_env()

    http_url = os.environ.get("NCE_MCP_HTTP_URL")
    if not http_url:
        sys.stderr.write("Error: NCE_MCP_HTTP_URL environment variable is required\n")
        return 1

    client_token = os.environ.get("NCE_MCP_CLIENT_TOKEN")
    if not client_token:
        sys.stderr.write("Error: NCE_MCP_CLIENT_TOKEN environment variable is required\n")
        return 1

    session_id = os.environ.get("NCE_MCP_SESSION_ID")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {client_token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    for line in sys.stdin:
        line_str = line.strip()
        if not line_str:
            continue

        try:
            payload = json.loads(line_str)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error", "data": str(exc)},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()
            continue

        data_bytes = json.dumps(payload).encode("utf-8")
        parsed = urllib.parse.urlsplit(http_url)
        if parsed.scheme not in ("http", "https"):
            err_resp = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32600, "message": "Invalid scheme"},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()
            continue

        req_path = parsed.path or "/"
        if parsed.query:
            req_path += f"?{parsed.query}"

        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=30)
            else:
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
            conn.request("POST", req_path, body=data_bytes, headers=headers)
            resp = conn.getresponse()
            resp_headers = dict(resp.getheaders())
            resp_session = resp_headers.get("Mcp-Session-Id") or resp_headers.get("mcp-session-id")
            if resp_session and not session_id:
                session_id = resp_session
                headers["Mcp-Session-Id"] = session_id

            content_type = resp_headers.get("Content-Type", resp_headers.get("content-type", ""))
            resp_bytes = resp.read()
            conn.close()

            if "text/event-stream" in content_type:
                # Parse SSE events looking for event: message or data: ...
                lines = resp_bytes.decode("utf-8", errors="replace").splitlines()
                for sse_line in lines:
                    if sse_line.startswith("data:"):
                        raw_data = sse_line[5:].strip()
                        if raw_data:
                            sys.stdout.write(raw_data + "\n")
                            sys.stdout.flush()
            else:
                resp_str = resp_bytes.decode("utf-8", errors="replace").strip()
                if resp_str:
                    sys.stdout.write(resp_str + "\n")
                    sys.stdout.flush()

        except urllib.error.HTTPError as http_err:
            body = http_err.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                sys.stdout.write(json.dumps(err_data) + "\n")
            except Exception:
                req_id = payload.get("id") if isinstance(payload, dict) else None
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32603,
                        "message": f"HTTP {http_err.code}: {http_err.reason}",
                        "data": body,
                    },
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            req_id = payload.get("id") if isinstance(payload, dict) else None
            err_resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": f"Client proxy gateway connection error: {exc}",
                },
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
