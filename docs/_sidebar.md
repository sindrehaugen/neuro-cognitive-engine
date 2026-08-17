<!-- docs/_sidebar.md -->

- [Welcome](README.md)

- Reference & Specifications
  - [API Reference](api_reference.md)
  - [API Surface (Auto-Generated)](API.md)
  - [MCP Tool Cookbook](mcp_tool_cookbook.md)
  - [API Usage Examples](api_usage_examples.md)
  - [Usage Modes & Payloads](usage_modes.md)
  - [Configuration Reference](configuration_reference.md)
  - [Frontend Readiness Specification](FRONTEND_READINESS.md)
  - [Data-Source Modes Architecture](DATA_SOURCE_MODES.md)
  - [Plugin Overview (Non-Technical)](plugin-overview-nontechnical.md)

- Getting Started
  - [Quick Start Guide](quick_start.md)
  - [Developer Onboarding](developer_onboarding.md)
  - [Frontend Integration Guide](frontend_integration_guide.md)

- Architecture & System Design
  - [Architecture v1.0 Spec](architecture-v1.md)
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

- Integrations & Bridges
  - [Service Webhook Integrations](service_integrations.md)
  - [Dynamics 365 Integration](d365_integration_reference.md)
  - [Bridge Setup Guide](bridge_setup_guide.md)
  - [Agent-to-Agent Protocol](a2a.md)

- Cognitive Layer
  - [Cognitive Features](cognitive_layer.md)
  - [LLM Providers & Schemas](llm_providers.md)
  - [NetBox & Cognitive Extensions](netbox_and_cognitive_extensions.md)

- Data Engineering & Simulation
  - [Memory Time Travel](time_travel.md)
  - [Memory Replay Engine](replay.md)
  - [System Migrations](migrations.md)

- Shared Core Foundation
  - [Shared Core Overview](shared-core/overview.md)
  - [Autonomy Governance Wrapper (C2)](shared-core/autonomy-governance.md)
  - [Entity-Resolution (C1)](shared-core/entity-resolution.md)
  - [External Scope RLS (C3)](shared-core/external-scope-rls.md)
  - [Allow-List Redactor (C8)](shared-core/redaction.md)
  - [Source Mode Divergence (C5)](shared-core/source-mode-divergence.md)
  - [Pricing, Signing & Grounding](shared-core/pricing-signing-grounding.md)

- Vertical Engine Guides
  - [Product User Guide](engines/product-user.md)
  - [Product Admin Guide](engines/product-admin.md)
  - [Procurement User Guide](engines/procurement-user.md)
  - [Procurement Admin Guide](engines/procurement-admin.md)
  - [Agreements User Guide](engines/agreements-user.md)
  - [Agreements Admin Guide](engines/agreements-admin.md)
  - [Vendors User Guide](engines/vendors-user.md)
  - [Vendors Admin Guide](engines/vendors-admin.md)
  - [Sales User Guide](engines/sales-user.md)
  - [Sales Admin Guide](engines/sales-admin.md)
  - [System Design User Guide](engines/system-design-user.md)
  - [System Design Admin Guide](engines/system-design-admin.md)
  - [Project User Guide](engines/project-user.md)
  - [Project Admin Guide](engines/project-admin.md)
  - [Economy User Guide](engines/economy-user.md)
  - [Economy Admin Guide](engines/economy-admin.md)
  - [Inventory User Guide](engines/inventory-user.md)
  - [Inventory Admin Guide](engines/inventory-admin.md)
  - [Field Tech User Guide](engines/field-tech-user.md)
  - [Field Tech Admin Guide](engines/field-tech-admin.md)
  - [HR & Academy User Guide](engines/hr-user.md)
  - [HR & Academy Admin Guide](engines/hr-admin.md)
  - [Diagnostics User Guide](engines/diagnostics-user.md)
  - [Diagnostics Admin Guide](engines/diagnostics-admin.md)

- Vertical Engine Specifications
  - [Engines Master Roadmap](vertical_engines/00-ENGINES-ROADMAP.md)
  - [Engine Delivery Status](vertical_engines/ENGINE_STATUS.md)
  - [Vertical Module Pattern](vertical_engines/VERTICAL_MODULE_PATTERN.md)
  - [Spec Review & Cross-Engine Gaps](vertical_engines/00b-spec-review-and-cross-engine-gaps.md)
  - [Shared Core Foundation Spec](vertical_engines/99-shared-core-foundation.md)
  - [C3 External Scope Threat Model](vertical_engines/_security/c3-external-scope-threat-model.md)
  - [01 Procurement Engine Spec](vertical_engines/01-procurement-engine.md)
  - [02 Product Engine Spec](vertical_engines/02-product-engine.md)
  - [03 Agreements Engine Spec](vertical_engines/03-agreements-engine.md)
  - [04 Vendors Engine Spec](vertical_engines/04-vendors-engine.md)
  - [05 Sales Engine Spec](vertical_engines/05-sales-engine.md)
  - [06 System Design Engine Spec](vertical_engines/06-system-design-engine.md)
  - [07 Project Engine Spec](vertical_engines/07-project-engine.md)
  - [08 Economy Engine Spec](vertical_engines/08-economy-engine.md)
  - [09 Assets Engine Spec](vertical_engines/09-assets-engine.md)
  - [10 Support Engine Spec](vertical_engines/10-support-engine.md)
  - [11 Inventory Engine Spec](vertical_engines/11-inventory-engine.md)
  - [12 Field Tech Engine Spec](vertical_engines/12-field-tech-engine.md)
  - [13 HR Engine Spec](vertical_engines/13-hr-engine.md)
  - [14 Marketing Engine Spec](vertical_engines/14-marketing-engine.md)
  - [15 Staff Resources Engine Spec](vertical_engines/15-staff-resources-engine.md)
  - [16 Business Insights Engine Spec](vertical_engines/16-business-insights-engine.md)
  - [17 Customer Portal Engine Spec](vertical_engines/17-customer-portal-engine.md)
  - [Network Ops Edge Overview](vertical_engines/NCE_network_ops_edge/README.md)
  - [Edge MCP Worker](vertical_engines/NCE_network_ops_edge/EDGE_MCP_WORKER.md)
  - [Remote Access & RMM Overview](vertical_engines/NCE_remote_access_rmm/README.md)
  - [19 Remote Access & RMM Engine](vertical_engines/NCE_remote_access_rmm/19-remote-access-rmm-engine.md)
  - [19b Smart RMM Features](vertical_engines/NCE_remote_access_rmm/19b-smart-features.md)
