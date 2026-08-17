> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Troubleshooting FAQ

This guide covers the most common errors encountered when deploying and running NCE across Local, Multi-User, and Cloud modes.

---

## 1. Docker Desktop Missing (Local Mode)

**Symptom:** NCE installer or shim reports "Docker daemon not found" or "Docker Desktop is required."

**Resolution:**
In Local mode, NCE relies on Docker to run the database stack. Install Docker Desktop for your OS. If your organization exceeds 250 employees, you may require a paid Docker license, or you can configure NCE to use Podman Desktop as an alternative.

---

## 2. Webhook 401 Unauthorized (Document Bridges)

**Symptom:** Cloud providers (SharePoint, Google Drive, Dropbox) report webhook delivery failures with `401 Unauthorized`.

**Resolution:**
This indicates a failure in signature or token validation at the FastAPI receiver.

| Provider | Header / Secret | Config var |
|---|---|---|
| SharePoint | `clientState` in payload | must match your configured secret |
| Google Drive | `X-Goog-Channel-Token` header | `DRIVE_CHANNEL_TOKEN` |
| Dropbox | `X-Dropbox-Signature` HMAC-SHA256 | `DROPBOX_APP_SECRET` |

---

## 3. VPN Disconnected / Cannot Reach Server (Multi-User Mode)

**Symptom:** Client machines report "Connection refused" or "Timeout" when attempting to connect to the central NCE server.

**Resolution:**
Ensure the client is connected to the corporate VPN. The Multi-User server is typically hosted on-premise and is not exposed to the public internet. Verify that the client can ping the server IP and that port **8003** (Admin API; default `ADMIN_PORT=8003`) is accessible.

```text
# Confirm the admin port from docker-compose:
# deploy/multiuser/docker-compose.yml  →  "${ADMIN_PORT:-8003}:8003"
# admin_server.py                      →  uvicorn.run(app, host="0.0.0.0", port=8003)
```

---

## 4. Port Conflicts (5432, 27017, 6379)

**Symptom:** `docker-compose up` fails with "bind: address already in use."

**Resolution:**
NCE requires specific ports for its database stack:

| Service | Default port |
|---|---|
| PostgreSQL | 5432 |
| MongoDB | 27017 |
| Redis | 6379 |

If you already have any of these services running locally, stop them or remap the ports in your `docker-compose.yml` and `.env` files.

---

## 5. Out of Memory (OOM) Errors during File Extraction

**Symptom:** The RQ Worker crashes or restarts when processing large PDFs or Excel files.

**Resolution:**
File extraction (especially OCR fallback) can be memory-intensive. Ensure your worker container has at least 4 GB of RAM allocated. You can also lower `NCE_MAX_ATTACHMENT_BYTES` (default **20 MB**; env var `NCE_MAX_ATTACHMENT_BYTES`) to reject oversized payloads before any I/O begins. Additional relevant limits:

| Env var | Default | Effect |
|---|---|---|
| `NCE_MAX_ATTACHMENT_BYTES` | `20971520` (20 MB) | Rejects blobs larger than this before extraction |
| `NCE_MAX_OCR_PAGES` | `10` | Caps OCR page count per document |

---

## 6. Missing Python 3.10+

**Symptom:** "Python version 3.10 or higher is required."

**Resolution:**
`pyproject.toml` declares `requires-python = ">=3.10"`. While the NCE installer bundles Python, manual deployments or custom scripts require Python 3.10+. Install the correct version and ensure it is in your system `PATH`.

---

## 7. CUDA / Hardware Accelerator Not Detected

**Symptom:** NCE falls back to CPU processing, resulting in slow embedding generation.

**Resolution:**
The Go shim attempts to auto-detect hardware (NVIDIA, AMD, Intel NPU, Apple Silicon). If it fails, ensure your GPU drivers are up to date. For NVIDIA, verify that the CUDA toolkit is installed and `nvidia-smi` returns valid output. You can manually override the backend in your configuration.

---

## 8. SharePoint Subscription Expired

**Symptom:** New documents in SharePoint are no longer being indexed automatically.

**Resolution:**
SharePoint webhook subscriptions have a maximum lifetime of **3 days** (`bridge_setup_guide.md`). NCE renews them automatically via the **`bridge_subscription_renewal`** cron job (defined in `nce/cron.py`), which runs every `BRIDGE_CRON_INTERVAL_MINUTES` minutes (default **45**).

```bash
# Check logs for the renewal job:
grep "bridge_subscription_renewal" /var/log/nce/cron.log

# Adjust renewal frequency (default 45 min):
BRIDGE_CRON_INTERVAL_MINUTES=30  # in .env or docker-compose environment
```

If the renewal job has failed, you can:
1. Check the cron error log for `"Cron Job Failed: bridge_subscription_renewal"`.
2. Manually trigger a full resync using the **`force_resync_bridge`** MCP tool.

---

## 9. Active Directory UPN Mismatch

**Symptom:** Users cannot authenticate in Multi-User mode, or their document permissions do not match.

**Resolution:**
Ensure the User Principal Name (UPN) provided by your Identity Provider matches the UPN format expected by NCE. Check your AD sync configuration and ensure the `user_id` passed to the API matches the directory UPN.

---

## 10. Tree-sitter Grammar Compilation Fails

**Symptom:** Errors related to `tree-sitter` or missing C++ compilers during setup.

**Resolution:**
NCE uses the pre-compiled `tree-sitter-language-pack` (see `nce/ast_parser.py`). Ensure you are using the latest version of the codebase. If you are adding custom grammars via the `add_custom_grammar` script, you must have a valid C++ build environment (e.g., Visual Studio Build Tools on Windows) installed.

---

## 11. Resource Quota Exceeded (-32013)

**Symptom:** MCP tool calls fail with error code `-32013` — "Resource quota exceeded."

**Resolution:**
The namespace or agent has reached its usage limit (tokens, storage, or memory count). This is raised as `QuotaExceededError` (code `MCP_QUOTA_EXCEEDED = -32013` in `nce/mcp_errors.py`).

| Action | How |
|---|---|
| Check current status | Call the `get_health` MCP tool to view consumption |
| Increase limit | Update the `resource_quotas` table in PostgreSQL for that namespace |
| Cleanup | Call `forget_memory` or wait for the garbage collector to reclaim space from temporary sessions |
| Disable quotas | Set `NCE_QUOTAS_ENABLED=false` (skips quota checks on the tool hot path) |

```sql
-- Example: increase memory limit for a namespace
INSERT INTO resource_quotas (namespace_id, resource_type, limit_amount)
VALUES ('your-namespace-id', 'memory_count', 10000)
ON CONFLICT (namespace_id, resource_type)
DO UPDATE SET limit_amount = EXCLUDED.limit_amount;
```

---

## 12. Cryptographic Signature Mismatch

**Symptom:** A memory or event is flagged as "invalid signature" or "tampered."

**Resolution:**
This indicates that the record's hash does not match the stored signature, or the signing key has changed.

- **Key mismatch:** Ensure the `NCE_MASTER_KEY` in your `.env` matches the one used when the data was originally written.
- **Tampering:** Investigate the database audit logs to see if a manual `UPDATE` was performed on the `memories` or `event_log` table outside of the application layer.
- **Key rotation:** If a key was recently rotated, verify that the `signature_key_id` in the record correctly maps to a key in the `signing_keys` table.
