<!-- docs/_sidebar.md -->

- [Welcome](README.md)

- Reference & Specifications
  - [API Reference](api_reference.md)
  - [API Surface (Auto-Generated)](API.md)
  - [Host Parity Matrix (Auto-Generated)](_generated/host_parity.md)
  - [MCP Tool Cookbook](mcp_tool_cookbook.md)
  - [API Usage Examples](api_usage_examples.md)
  - [Usage Modes & Payloads](usage_modes.md)
  - [Configuration Reference](configuration_reference.md)
  - [Frontend Readiness Specification](FRONTEND_READINESS.md)
  - [Data-Source Modes Architecture](DATA_SOURCE_MODES.md)
  - [Detector Limits & AST Ratchet](detector_limits.md)
  - [100k-Row Benchmark Report (v3.5.2)](benchmarks/PERF_REPORT_v3.5.2.md)
  - [Hardening Report (v3.5.3)](benchmarks/HARDENING_READINESS_v3.5.3.md)
  - [Pure-ASGI & ONNX Report (v3.5.4)](benchmarks/HARDENING_READINESS_v3.5.4.md)
  - [Connector SDK Report (v3.6.0)](benchmarks/HARDENING_READINESS_v3.6.0.md)
  - [Plugin & Connector SDK Guide](plugin_development.md)
  - [Plugin Overview (Non-Technical)](plugin-overview-nontechnical.md)

- Getting Started
  - [Quick Start Guide](quick_start.md)
  - [Developer Onboarding](developer_onboarding.md)
  - [Frontend Integration Guide](frontend_integration_guide.md)

- Architecture & System Design
  - [Architecture Spec v1.0](architecture-v1.md)
  - [Database Architecture](database_architecture.md)
  - [Recursive Indexing Flow](recursive_indexing_flow.md)
  - [Push Architecture](push_architecture.md)
  - [Architecture Decision Records](adr/README.md)
  - [Citus Multi-Node Descope](citus_descope.md)

- Configuration & Operations
  - [IT Admin Guide](it_admin_guide.md)
  - [Airgapped & Edge Deployment](airgapped_deployment.md)
  - [VRAM Monitoring](vram_monitoring.md)
  - [Observability Guide](observability.md)
  - [Performance Tuning](performance_tuning.md)
  - [Troubleshooting & FAQ](troubleshooting_faq.md)
  - [Backfill Chain Hash Guide](scripts/BACKFILL_CHAIN_HASH.md)

- Security & Multi-Tenancy
  - [Enterprise Security Guide](enterprise_security.md)
  - [Multi-Tenancy & Quotas](multi_tenancy.md)
  - [Cryptographic Signing](signing.md)
  - [PII Detection & Redaction](pii.md)
  - [AWS IAM Worker Isolation](aws_iam_worker_isolation.md)

- Integrations & Federation
  - [Service Webhook Integrations](service_integrations.md)
  - [Bridge Setup Guide](bridge_setup_guide.md)
  - [Cross-Namespace Federation (A2A)](a2a.md)

- Retrieval, Graph & Summarization
  - [Hybrid Retrieval & Summarization](cognitive_layer.md)
  - [LLM Providers & Schemas](llm_providers.md)

- Temporal Queries, Replay & Migrations
  - [Point-in-Time Queries (as_of)](time_travel.md)
  - [Deterministic Replay & Forking](replay.md)
  - [System Migrations & Re-embedding](migrations.md)

- Shared Core Foundation
  - [Shared Core Overview](shared-core/overview.md)
  - [C2 Action Governance (@governed)](shared-core/autonomy-governance.md)
  - [Entity Resolution (C1)](shared-core/entity-resolution.md)
  - [External Scope RLS (C3)](shared-core/external-scope-rls.md)
  - [Allow-List Redactor (C8)](shared-core/redaction.md)
  - [Source Mode Divergence (C5)](shared-core/source-mode-divergence.md)
  - [Pricing, Signing & Grounding](shared-core/pricing-signing-grounding.md)

- Experimental R&D Extension Repos
  - [App Connectors (nce-app-engines, Beta)](https://github.com/sindrehaugen/nce-app-engines)
  - [Business Domain Engines (nce-vertical-engines, Beta)](https://github.com/sindrehaugen/nce-vertical-engines)
