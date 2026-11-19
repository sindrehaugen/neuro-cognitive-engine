"""Core Connector SDK for NCE application and vertical plugins.

Provides vendor-neutral base contracts, SSRF & archive guards, Entra OIDC
verification, replay-sandbox fencing, RFC 8785 JCS governance, encrypted
tenant connection vaults, canonical entity/diagram envelopes, and tenant-scoped
substrate helpers.

Exports resolve lazily (PEP 562) so importing lightweight descriptors or CLI
metadata helpers (``ConnectorDescriptor``, ``BaseAppConnector``) never forces
server-runtime secret checks prematurely.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nce.connectors.base import (
        BaseAppConnector,
        ConnectorAuthKind,
        ConnectorDescriptor,
        ConnectorMaturity,
    )
    from nce.connectors.entra import (
        ALLOWED_ENTRA_ALGORITHMS,
        EntraConfig,
        EntraOIDCVerifier,
        EntraPrincipal,
        EntraVerificationError,
    )
    from nce.connectors.envelopes import (
        SIGNAL_DOMAIN_STYLES,
        CanonicalDiagramIR,
        CanonicalEntityEnvelope,
        DiagramEdgeIR,
        DiagramIR,
        DiagramNodeIR,
        DiagramPageIR,
        DiagramPortPinIR,
        DiagramSwimlaneIR,
        Iso7200TitleBlockIR,
        auto_layout_page,
        diff_diagram_ir,
        layout_sugiyama_orthogonal,
    )
    from nce.connectors.governance import (
        ReplaySandboxFenceError,
        assert_not_replay_sandbox,
        check_replay_sandbox_env,
        compute_jcs_sha256,
        governed_action,
        governed_action_response,
        record_connector_worm_event,
    )
    from nce.connectors.guards import (
        BLOCKED_INTERNAL_HOSTNAMES,
        DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES,
        AllowListRedactor,
        BridgeURLValidationError,
        SSRFBlockedError,
        is_ip_blocked,
        read_nce_master_key_str,
        redact_c8_allowlist,
        resolve_connector_env_flag,
        safe_extract_zip_bytes,
        safe_inflate_raw_deflate,
        safe_parse_xml_bytes,
        sanitize_html_label,
        unwrap_ip,
        validate_outbound_url,
        validate_outbound_url_async,
    )
    from nce.connectors.substrate import (
        ArtifactStoreAdapter,
        BackgroundJobRunner,
        SubstrateError,
        TenantSecretBox,
        clear_mem_substrate,
        emit_kg_and_worm_link,
        normalize_record_uuid,
        normalize_tenant_namespace,
        pg_tenant_list,
        pg_tenant_upsert,
    )
    from nce.connectors.vault import (
        AppConnectionMetadata,
        ConnectionStatus,
        TenantConnectionVault,
    )

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BaseAppConnector": ("nce.connectors.base", "BaseAppConnector"),
    "ConnectorAuthKind": ("nce.connectors.base", "ConnectorAuthKind"),
    "ConnectorDescriptor": ("nce.connectors.base", "ConnectorDescriptor"),
    "ConnectorMaturity": ("nce.connectors.base", "ConnectorMaturity"),
    "ALLOWED_ENTRA_ALGORITHMS": ("nce.connectors.entra", "ALLOWED_ENTRA_ALGORITHMS"),
    "EntraConfig": ("nce.connectors.entra", "EntraConfig"),
    "EntraOIDCVerifier": ("nce.connectors.entra", "EntraOIDCVerifier"),
    "EntraPrincipal": ("nce.connectors.entra", "EntraPrincipal"),
    "EntraVerificationError": ("nce.connectors.entra", "EntraVerificationError"),
    "SIGNAL_DOMAIN_STYLES": ("nce.connectors.envelopes", "SIGNAL_DOMAIN_STYLES"),
    "CanonicalDiagramIR": ("nce.connectors.envelopes", "CanonicalDiagramIR"),
    "CanonicalEntityEnvelope": ("nce.connectors.envelopes", "CanonicalEntityEnvelope"),
    "DiagramEdgeIR": ("nce.connectors.envelopes", "DiagramEdgeIR"),
    "DiagramIR": ("nce.connectors.envelopes", "DiagramIR"),
    "DiagramNodeIR": ("nce.connectors.envelopes", "DiagramNodeIR"),
    "DiagramPageIR": ("nce.connectors.envelopes", "DiagramPageIR"),
    "DiagramPortPinIR": ("nce.connectors.envelopes", "DiagramPortPinIR"),
    "DiagramSwimlaneIR": ("nce.connectors.envelopes", "DiagramSwimlaneIR"),
    "Iso7200TitleBlockIR": ("nce.connectors.envelopes", "Iso7200TitleBlockIR"),
    "auto_layout_page": ("nce.connectors.envelopes", "auto_layout_page"),
    "diff_diagram_ir": ("nce.connectors.envelopes", "diff_diagram_ir"),
    "layout_sugiyama_orthogonal": ("nce.connectors.envelopes", "layout_sugiyama_orthogonal"),
    "ReplaySandboxFenceError": ("nce.connectors.governance", "ReplaySandboxFenceError"),
    "assert_not_replay_sandbox": ("nce.connectors.governance", "assert_not_replay_sandbox"),
    "check_replay_sandbox_env": ("nce.connectors.governance", "check_replay_sandbox_env"),
    "compute_jcs_sha256": ("nce.connectors.governance", "compute_jcs_sha256"),
    "governed_action": ("nce.connectors.governance", "governed_action"),
    "governed_action_response": ("nce.connectors.governance", "governed_action_response"),
    "record_connector_worm_event": ("nce.connectors.governance", "record_connector_worm_event"),
    "BLOCKED_INTERNAL_HOSTNAMES": ("nce.connectors.guards", "BLOCKED_INTERNAL_HOSTNAMES"),
    "DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES": (
        "nce.connectors.guards",
        "DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES",
    ),
    "AllowListRedactor": ("nce.connectors.guards", "AllowListRedactor"),
    "BridgeURLValidationError": ("nce.connectors.guards", "BridgeURLValidationError"),
    "SSRFBlockedError": ("nce.connectors.guards", "SSRFBlockedError"),
    "is_ip_blocked": ("nce.connectors.guards", "is_ip_blocked"),
    "read_nce_master_key_str": ("nce.connectors.guards", "read_nce_master_key_str"),
    "redact_c8_allowlist": ("nce.connectors.guards", "redact_c8_allowlist"),
    "resolve_connector_env_flag": ("nce.connectors.guards", "resolve_connector_env_flag"),
    "safe_extract_zip_bytes": ("nce.connectors.guards", "safe_extract_zip_bytes"),
    "safe_inflate_raw_deflate": ("nce.connectors.guards", "safe_inflate_raw_deflate"),
    "safe_parse_xml_bytes": ("nce.connectors.guards", "safe_parse_xml_bytes"),
    "sanitize_html_label": ("nce.connectors.guards", "sanitize_html_label"),
    "unwrap_ip": ("nce.connectors.guards", "unwrap_ip"),
    "validate_outbound_url": ("nce.connectors.guards", "validate_outbound_url"),
    "validate_outbound_url_async": ("nce.connectors.guards", "validate_outbound_url_async"),
    "ArtifactStoreAdapter": ("nce.connectors.substrate", "ArtifactStoreAdapter"),
    "BackgroundJobRunner": ("nce.connectors.substrate", "BackgroundJobRunner"),
    "SubstrateError": ("nce.connectors.substrate", "SubstrateError"),
    "TenantSecretBox": ("nce.connectors.substrate", "TenantSecretBox"),
    "clear_mem_substrate": ("nce.connectors.substrate", "clear_mem_substrate"),
    "emit_kg_and_worm_link": ("nce.connectors.substrate", "emit_kg_and_worm_link"),
    "normalize_record_uuid": ("nce.connectors.substrate", "normalize_record_uuid"),
    "normalize_tenant_namespace": ("nce.connectors.substrate", "normalize_tenant_namespace"),
    "pg_tenant_list": ("nce.connectors.substrate", "pg_tenant_list"),
    "pg_tenant_upsert": ("nce.connectors.substrate", "pg_tenant_upsert"),
    "AppConnectionMetadata": ("nce.connectors.vault", "AppConnectionMetadata"),
    "ConnectionStatus": ("nce.connectors.vault", "ConnectionStatus"),
    "TenantConnectionVault": ("nce.connectors.vault", "TenantConnectionVault"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        mod_path, attr_name = target
        mod = importlib.import_module(mod_path)
        obj = getattr(mod, attr_name)
        globals()[name] = obj
        return obj
    try:
        mod = importlib.import_module(f"nce.connectors.{name}")
    except ModuleNotFoundError as exc:
        raise AttributeError(f"module 'nce.connectors' has no attribute {name!r}") from exc
    globals()[name] = mod
    return mod


__all__ = [
    "ALLOWED_ENTRA_ALGORITHMS",
    "AllowListRedactor",
    "AppConnectionMetadata",
    "ArtifactStoreAdapter",
    "BLOCKED_INTERNAL_HOSTNAMES",
    "BackgroundJobRunner",
    "BaseAppConnector",
    "BridgeURLValidationError",
    "CanonicalDiagramIR",
    "CanonicalEntityEnvelope",
    "ConnectionStatus",
    "ConnectorAuthKind",
    "ConnectorDescriptor",
    "ConnectorMaturity",
    "DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES",
    "DiagramEdgeIR",
    "DiagramIR",
    "DiagramNodeIR",
    "DiagramPageIR",
    "DiagramPortPinIR",
    "DiagramSwimlaneIR",
    "EntraConfig",
    "EntraOIDCVerifier",
    "EntraPrincipal",
    "EntraVerificationError",
    "Iso7200TitleBlockIR",
    "ReplaySandboxFenceError",
    "SIGNAL_DOMAIN_STYLES",
    "SSRFBlockedError",
    "SubstrateError",
    "TenantConnectionVault",
    "TenantSecretBox",
    "assert_not_replay_sandbox",
    "auto_layout_page",
    "check_replay_sandbox_env",
    "clear_mem_substrate",
    "compute_jcs_sha256",
    "diff_diagram_ir",
    "emit_kg_and_worm_link",
    "governed_action",
    "governed_action_response",
    "is_ip_blocked",
    "layout_sugiyama_orthogonal",
    "normalize_record_uuid",
    "normalize_tenant_namespace",
    "pg_tenant_list",
    "pg_tenant_upsert",
    "read_nce_master_key_str",
    "record_connector_worm_event",
    "redact_c8_allowlist",
    "resolve_connector_env_flag",
    "safe_extract_zip_bytes",
    "safe_inflate_raw_deflate",
    "safe_parse_xml_bytes",
    "sanitize_html_label",
    "unwrap_ip",
    "validate_outbound_url",
    "validate_outbound_url_async",
]
