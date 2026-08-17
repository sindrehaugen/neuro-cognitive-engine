> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE IT Admin Guide

This guide provides IT administrators with the necessary instructions to deploy, configure, and maintain NCE in an enterprise environment. It focuses on infrastructure-as-code (IaC) deployments, network security, and identity integration.

## 1. Infrastructure Deployment (Cloud Mode)

NCE supports automated deployment to major cloud providers using Terraform (AWS/GCP) or Bicep (Azure).

### 1.1 AWS Deployment (Terraform)

The AWS deployment provisions RDS (PostgreSQL), DocumentDB (MongoDB), ElastiCache (Redis), S3 (Blob storage), and Fargate (Container Apps).

**Prerequisites:**
- Terraform >= 1.6.0
- AWS CLI configured with appropriate permissions

**Steps:**
1. Navigate to the AWS infrastructure directory:
   ```bash
   cd trimcp-infra/aws
   ```
2. Copy the example variables file and configure your parameters:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```
3. Initialize and apply the Terraform configuration:
   ```bash
   terraform init
   terraform apply
   ```

### 1.2 GCP Deployment (Terraform)

The GCP deployment provisions Cloud SQL (PostgreSQL), a MongoDB-compatible store secret reference, Memorystore (Redis), GCS (Blob storage), and Cloud Run (worker and webhook receiver).

**Prerequisites:**
- Terraform >= 1.6.0
- `gcloud` CLI authenticated with appropriate permissions

**Steps:**
1. Navigate to the GCP infrastructure directory:
   ```bash
   cd trimcp-infra/gcp
   ```
2. Copy the example variables file and configure your parameters:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```
3. Initialize and apply the Terraform configuration:
   ```bash
   terraform init
   terraform apply
   ```

### 1.3 Azure Deployment (Bicep) — Deferred

> **Planned seam:** The Azure Bicep scaffolding exists under `trimcp-infra/azure/` but is explicitly marked **deferred** in the IaC README (`trimcp-infra/README.md`): "placeholder Bicep only; not on the v1 production path." Use AWS or GCP for production deployments. The steps below are for reference when the Azure track is activated.

The Azure deployment provisions Azure Database for PostgreSQL, Cosmos DB (MongoDB API), Azure Cache for Redis, Azure Blob Storage, and Azure Container Apps.

**Prerequisites:**
- Azure CLI
- Bicep CLI

**Steps:**
1. Navigate to the Azure infrastructure directory:
   ```bash
   cd trimcp-infra/azure
   ```
2. Update the `parameters.example.json` with your specific values and rename it to `parameters.json`.
3. Deploy the Bicep template (subscription-scoped):
   ```bash
   az deployment sub create \
     --location <Your-Region> \
     --template-file main.bicep \
     --parameters @parameters.json
   ```

## 2. Network Security & Firewall Rules

Whether deploying on-premise (Multi-User Mode) or in the cloud, specific ports must be accessible for NCE components to communicate.

### 2.1 Internal Database Ports
These ports should **only** be accessible to the NCE application servers and workers. They must **never** be exposed to the public internet.

| Service | Port | Protocol | Description |
|---------|------|----------|-------------|
| PostgreSQL | `5432` | TCP | Relational database (Vector data via pgvector) |
| MongoDB | `27017` | TCP | Document database (Graph data) |
| Redis | `6379` | TCP | Queue and caching (RQ) |

### 2.2 Application Ports
These ports handle administrative, A2A, and webhook traffic.

| Service | Port | Protocol | Description |
|---------|------|----------|-------------|
| Admin UI + REST API | `8003` | TCP | Starlette Admin panel and admin REST endpoints. Expose to internal network/VPN only. |
| A2A Server | `8004` | TCP | Agent-to-Agent RPC endpoint. Expose to internal network/VPN or trusted peers only. |
| Webhook Receiver | `8080` | TCP | Container-internal webhook receiver. In production this is **not** directly exposed; TLS termination (port `443`) is handled by the Caddy reverse proxy (or equivalent cloud front-proxy). |

*Note: In Local mode, all services run on `localhost` and do not require inbound firewall rules.*

## 3. Active Directory & Identity Integration

NCE relies on accurate user identity to enforce document-level permissions and access controls.

### 3.1 UPN Resolution
NCE uses the User Principal Name (UPN) as the primary identifier (`user_id`). 
- Ensure that the UPN provided by your SAML/OIDC Identity Provider exactly matches the UPN used in your document libraries (e.g., SharePoint/OneDrive).
- If your organization uses alternate login IDs or email addresses that differ from the UPN, you must configure a mapping rule in your IdP to pass the correct UPN in the authentication token.

### 3.2 OAuth Configuration for Document Bridges
To enable the Document Bridge System (Push Architecture), you must register NCE as an application in your respective cloud providers.

**Microsoft Entra ID (SharePoint/OneDrive):**
1. Register a new application in the Entra ID portal.
2. Grant the following Application permissions: `Sites.Read.All`, `Files.Read.All`.
3. Grant admin consent for the tenant.
4. Configure the Webhook Receiver URL (`https://<your-domain>/webhooks/sharepoint`) in the application settings.

**Google Workspace:**
1. Create a Service Account in the Google Cloud Console.
2. Enable Domain-Wide Delegation.
3. Grant the `https://www.googleapis.com/auth/drive.readonly` scope.

Provide the resulting client IDs and secrets to the NCE configuration via the `.env` file or your cloud provider's secret management service (e.g., AWS Secrets Manager, Azure Key Vault).

## 4. Dynamic Tools & Skills Management Console

NCE features a dynamic administration console within the Starlette Admin panel (`admin/index.html` serviced by `admin_server.py`) for managing local stdio Model Context Protocol (MCP) tools and public Agent-to-Agent (A2A) network skills.

### 4.1 System Architecture
- **Control Plane**: Administrators mutate toggle states via Basic/HMAC-secured REST endpoints `/api/admin/tools` and `/api/admin/tools/toggle`.
- **Data Plane (Redis Registry)**: Toggle states are stored inside the Redis hash key `nce:tools:disabled`.
  - When a tool is **disabled**, its name is registered in the hash with a value of `1`.
  - When a tool is **enabled**, its key is deleted from the hash.
- **Routing Interceptors**:
  - **Stdio MCP Layer**: Calls are intercepted inside `mcp_stdio_dispatch.py` via `GOVERNANCE.is_disabled(...)`. Disabled tools return JSON-RPC error code `-32005` (Scope forbidden).
  - **A2A Network Layer**: Skills are intercepted inside `_dispatch_skill` in `a2a_server.py`. Disabled skills raise `A2AScopeViolationError`, returning JSON-RPC error code `-32011` / HTTP 403 (Scope violation).

### 4.2 Fail-Closed Governance & Last-Known-Good Cache Model
Batch 100 (`4da8a4e`, addressing CWE-636 / CWE-1188) overhauled tool and skill governance to eliminate silent un-revoke vulnerabilities caused by fail-open error handling during Redis outages. Governance is now strictly **fail-closed** backed by a process-local last-known-good cache (`ToolGovernanceCache` in `nce/tool_governance.py`).

#### 4.2.1 Three-State Cache Lifecycle
The governance cache evaluates state using monotonic time (`time.monotonic()`):

1. **INITIALIZED (Fresh — `STALE_OK`)**:
   - Condition: Snapshot age < `NCE_TOOL_GOVERNANCE_STALE_OK_SEC` (default: `30` seconds).
   - Behavior: Serves directly from the in-memory snapshot (`_snapshot`) with zero Redis overhead.
2. **INITIALIZED (Stale — `STALE_HARD`)**:
   - Condition: Snapshot age between `30` seconds and `NCE_TOOL_GOVERNANCE_STALE_HARD_SEC` (default: `300` seconds / 5 minutes).
   - Behavior: Attempts a live refresh against Redis (`hkeys nce:tools:disabled`). If Redis is unreachable or times out, it continues serving and enforcing the last-known-good snapshot.
3. **INITIALIZED (Cache Exhausted / Hard Stale)**:
   - Condition: Snapshot age > `300` seconds.
   - Behavior: Snapshot is no longer trusted. The cache raises `GovernanceUnavailable`, failing closed. Dispatch is blocked immediately.
4. **NEVER-INITIALIZED (Cold Boot)**:
   - Condition: Process has started but no Redis fetch has ever succeeded (`_snapshot is None`).
   - In **Production** (`cfg.IS_PROD`): Fails closed immediately by raising `GovernanceUnavailable`. This closes the cold-boot un-revoke hole (preventing revoked tools from executing if process boots during a Redis outage).
   - In **Dev/Test** (`not cfg.IS_PROD`): Logs a warning and defaults to permitted to facilitate local testing without a running Redis instance.

#### 4.2.2 Error Mapping & Observability
When governance cannot be evaluated or the cache is exhausted, `GovernanceUnavailable` is raised and translated into strict scope errors rather than generic internal errors:
- **Stdio MCP surface (`mcp_stdio_dispatch.py`)**: Catches `GovernanceUnavailable` and returns JSON-RPC error `-32005` (`MCP_SCOPE_FORBIDDEN` / "Scope forbidden", `detail="Tool governance registry unavailable; dispatch blocked."`). It never returns `-32603` (Internal error) or leaks internal tracebacks.
- **A2A network surface (`a2a_server.py`)**: Catches `GovernanceUnavailable` and raises `A2AScopeViolationError("A2A skill governance registry unavailable; dispatch blocked.")`, which maps to JSON-RPC error `-32011` (`MCP_A2A_SCOPE_VIOLATION` / `A2A_CODE_SCOPE_VIOLATION`, HTTP 403).
- **Observability**: Any degraded governance decision (hard-stale fail-closed, cold-boot prod block, or dev allow) increments the Prometheus counter `nce_tool_governance_degraded_total` (`GOVERNANCE_DEGRADED_TOTAL`).

### 4.3 Administration Operations
1. **Accessing the Console**: Open the Starlette Admin panel (default port `8003`) and click the **Tools** tab in the sidebar navigation.
2. **Reviewing Operational Impact**: Each tool card displays a customized description alongside an amber warning block explaining downstream consequences (e.g., disabling `store_memory` disables agent write paths and entity extraction pipelines, resulting in potential data loss for new sessions).
3. **Toggling States**: Toggling a dynamic switch immediately updates the Redis hash (`nce:tools:disabled`), which propagates to all active workers and servers within `STALE_OK` (30s) or upon next cache refresh.
