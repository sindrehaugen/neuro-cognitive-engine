"""Unit tests for the v3.6.0 Core Connector SDK, plugin maturity contract & migration 041."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import nce.plugins as plugins_mod
from nce.connectors import (
    AllowListRedactor,
    BaseAppConnector,
    CanonicalDiagramIR,
    CanonicalEntityEnvelope,
    ConnectorDescriptor,
    DiagramEdgeIR,
    DiagramNodeIR,
    DiagramPageIR,
    EntraConfig,
    EntraOIDCVerifier,
    EntraVerificationError,
    ReplaySandboxFenceError,
    SSRFBlockedError,
    TenantConnectionVault,
    diff_diagram_ir,
    governed_action,
    governed_action_response,
    redact_c8_allowlist,
    safe_extract_zip_bytes,
    safe_inflate_raw_deflate,
    safe_parse_xml_bytes,
    sanitize_html_label,
    validate_outbound_url,
)
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.plugins import _normalize_descriptor, list_plugin_engine_descriptors

REPO_ROOT = Path(__file__).resolve().parents[1]


class _SampleBetaConnector(BaseAppConnector):
    descriptor = ConnectorDescriptor(
        slug="sample_cad_app",
        name="Sample CAD App",
        description="Reference connector built on Core SDK.",
        types=["app-connector"],
        categories=["operations-delivery"],
        auth_kind="oauth2",
        capabilities=["diagram.read", "diagram.write"],
        tool_count=2,
        route_count=1,
    )


def test_connector_descriptor_defaults_to_beta_and_normalizes_in_plugins() -> None:
    conn = _SampleBetaConnector()
    raw_desc = conn.to_engine_descriptor(plugin_id="nce_apps")
    assert raw_desc["maturity"] == "beta"
    assert raw_desc["vendor_specific"] is True
    assert raw_desc["auth_kind"] == "oauth2"

    # Plugin normalization defaults missing/invalid maturity to stable & preserves valid values.
    assert _normalize_descriptor({"slug": "legacy"}, "legacy")["maturity"] == "stable"
    assert _normalize_descriptor({"slug": "legacy"}, "legacy")["vendor_specific"] is False
    assert (
        _normalize_descriptor({"slug": "invalid", "maturity": "bogus"}, "p")["maturity"] == "stable"
    )
    beta_norm = _normalize_descriptor(
        {"slug": "app_x", "maturity": "BETA", "vendor_specific": True}, "nce_apps"
    )
    assert beta_norm["maturity"] == "beta"
    assert beta_norm["vendor_specific"] is True


def test_list_plugin_engine_descriptors_preserves_plugin_maturity_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FallbackBetaPlugin:
        plugin_id = "demo_beta"
        name = "Demo Beta"
        description = "Beta plugin without get_plugin_descriptors"
        maturity = "beta"
        vendor_specific = True

    monkeypatch.setattr(plugins_mod, "_DESCRIPTOR_CACHE", {})
    monkeypatch.setattr(plugins_mod, "load_plugins", lambda: {"demo_beta": _FallbackBetaPlugin()})
    descs = list_plugin_engine_descriptors()
    assert len(descs) == 1
    assert descs[0]["maturity"] == "beta"
    assert descs[0]["vendor_specific"] is True


@pytest.mark.asyncio
async def test_governed_action_replay_fence_dry_run_and_redaction() -> None:
    executed: list[str] = []

    @governed_action("demo_app", "push_changes", mutation=True)
    async def _push(arguments: dict[str, Any]) -> dict[str, Any]:
        executed.append("ran")
        return {
            "status": "ok",
            "api_key": "should-be-dropped",
            "unit_cost": 42.5,
            "pushed_id": arguments["target_id"],
        }

    # Replay sandbox blocks external mutations.
    with pytest.raises(ReplaySandboxFenceError):
        await _push(arguments={"target_id": "T-1", "is_replay_sandbox": True})
    assert executed == []

    # dry_run returns pending approval preview with C8 redaction & JCS hash without side effects.
    preview = await _push(
        arguments={"target_id": "T-1", "dry_run": True, "password": "pw", "margin": 0.4}
    )
    assert preview["status"] == "pending_approval"
    assert preview["confirm_required"] is True
    assert preview["jcs_sha256"]
    assert "password" not in preview["preview"]
    assert "margin" not in preview["preview"]
    assert executed == []

    # Normal execution strips C8/secret fields and appends RFC 8785 JCS SHA-256.
    res = await _push(arguments={"target_id": "T-2"})
    assert executed == ["ran"]
    assert res["pushed_id"] == "T-2"
    assert "api_key" not in res
    assert "unit_cost" not in res
    assert len(res["jcs_sha256"]) == 64

    # Direct redactor supports strip and mask modes.
    masked = AllowListRedactor(mode="mask").redact({"token": "abc", "safe": 1})
    assert masked["token"] == "[REDACTED]"
    assert masked["safe"] == 1
    assert "unit_cost" not in redact_c8_allowlist({"unit_cost": 10, "ok": "y"})
    pending = governed_action_response("op", False, {"visibility": "public", "cost": 1})
    assert pending["visibility"] == "public"
    assert "cost" not in pending["preview"]


def test_ssrf_and_archive_xml_guards() -> None:
    for bad_url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://[::ffff:10.0.0.1]/secret",
        "http://localhost:9000/api",
        "https://user:pass@example.com/resource",
    ):
        with pytest.raises(SSRFBlockedError):
            validate_outbound_url(bad_url)

    assert validate_outbound_url(
        "https://api.example.com/v1/items", allow_http=False, resolve_dns=False
    )

    # XXE rejection via defusedxml.
    xxe_payload = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><r>&xxe;</r>'
    with pytest.raises(ValueError, match="Unsafe XML"):
        safe_parse_xml_bytes(xxe_payload)

    # Zip-slip path traversal rejection.
    slip_buf = io.BytesIO()
    with zipfile.ZipFile(slip_buf, "w") as zf:
        zf.writestr("../../etc/cron.d/pwn", "bad")
    with pytest.raises(ValueError, match="Path traversal"):
        safe_extract_zip_bytes(slip_buf.getvalue())

    # Forbidden executable suffix rejection.
    exe_buf = io.BytesIO()
    with zipfile.ZipFile(exe_buf, "w") as zf:
        zf.writestr("payload.ps1", "Write-Host 1")
    with pytest.raises(ValueError, match="Forbidden"):
        safe_extract_zip_bytes(exe_buf.getvalue())

    # Raw deflate bomb rejection.
    import zlib

    comp = zlib.compressobj(level=9, wbits=-15)
    bomb_bytes = comp.compress(b"A" * 4096) + comp.flush()
    with pytest.raises(ValueError, match="exceeds"):
        safe_inflate_raw_deflate(bomb_bytes, max_bytes=128)

    assert sanitize_html_label("<script>alert(1)</script><b>Rack-A</b>") == "Rack-A"


@pytest.mark.asyncio
async def test_entra_oidc_verifier_rs256_valid_and_forged_rejection() -> None:
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_dict = jwt.algorithms.RSAAlgorithm.to_jwk(priv_key.public_key(), as_dict=True)
    jwk_dict["kid"] = "k1"
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"

    tenant_id = str(uuid.uuid4())
    cfg = EntraConfig(tenant_id=tenant_id, audience="api://nce-test", enforce_mode="strict")
    verifier = EntraOIDCVerifier(cfg)

    now = datetime.now(timezone.utc)
    claims = {
        "iss": cfg.expected_issuer,
        "aud": "api://nce-test",
        "tid": tenant_id,
        "sub": "user-123",
        "oid": "oid-123",
        "preferred_username": "alice@example.com",
        "name": "Alice Engineer",
        "roles": ["Connector.Admin"],
        "scp": "Connectors.ReadWrite",
        "nbf": int((now - timedelta(seconds=5)).timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    valid_token = jwt.encode(claims, priv_key, algorithm="RS256", headers={"kid": "k1"})
    principal = await verifier.verify_token(
        f"Bearer {valid_token}",
        required_roles=["Connector.Admin"],
        jwks=[jwk_dict],
    )
    assert principal.oid == "oid-123"
    assert principal.upn == "alice@example.com"
    assert "Connector.Admin" in principal.roles

    # Reject wrong/missing required role.
    with pytest.raises(EntraVerificationError, match="Missing required Entra role"):
        await verifier.verify_token(valid_token, required_roles=["Global.Owner"], jwks=[jwk_dict])

    # Reject disallowed HS256/none algorithms.
    hs_token = jwt.encode(
        claims,
        "hmac-secret-at-least-32-bytes-long-for-hs256-test",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(EntraVerificationError, match="Disallowed JWT signing algorithm"):
        await verifier.verify_token(hs_token, jwks=[jwk_dict])


@pytest.mark.asyncio
async def test_tenant_connection_vault_roundtrip_and_zero_secret_leak() -> None:
    vault = TenantConnectionVault(pool=None)
    ns_a = uuid.uuid4()
    ns_b = uuid.uuid4()

    meta = await vault.upsert_connection(
        ns_a,
        "kicad",
        {"api_token": "secret-token-123", "refresh_secret": "shhh"},
        auth_kind="api_key",
        endpoint_url="https://api.example.com/kicad",
        scopes=["projects.read"],
        metadata={"workspace": "nordics", "client_secret": "must-be-stripped"},
        resolve_dns=False,
    )
    assert meta.app_slug == "kicad"
    assert meta.status == "connected"
    assert "client_secret" not in meta.metadata
    assert meta.metadata["workspace"] == "nordics"

    listed_a = await vault.list_connections(ns_a)
    assert len(listed_a) == 1
    dumped = listed_a[0].model_dump_json()
    assert "secret-token-123" not in dumped
    assert "encrypted_config" not in dumped

    assert await vault.list_connections(ns_b) == []
    assert await vault.get_connected_app_slugs(ns_a) == {"kicad"}

    decrypted = await vault.get_decrypted_secret(ns_a, "kicad")
    assert decrypted == {"api_token": "secret-token-123", "refresh_secret": "shhh"}

    # Cross-tenant isolation check.
    assert await vault.get_decrypted_secret(ns_b, "kicad") is None
    assert await vault.delete_connection(ns_a, meta.connection_id) is True
    assert await vault.list_connections(ns_a) == []


def test_canonical_envelopes_and_migration_041_contract() -> None:
    ns_id = uuid.uuid4()
    env = CanonicalEntityEnvelope(
        app_slug="figma",
        namespace_id=ns_id,
        entity_type="design_frame",
        external_id="frame-99",
        title="Rack Front View",
    )
    assert env.provenance_tier == "external_untrusted"
    assert env.kg_uri() == f"urn:nce:figma:{ns_id}:design_frame:frame-99"
    assert env.to_kg_node_payload()["metadata"]["provenance_tier"] == "external_untrusted"

    ir1 = CanonicalDiagramIR(
        diagram_id="d1",
        pages=[DiagramPageIR(name="P1", nodes=[DiagramNodeIR(node_id="n1", label="A")])],
    )
    ir2 = CanonicalDiagramIR(
        diagram_id="d1",
        pages=[
            DiagramPageIR(
                name="P1",
                nodes=[
                    DiagramNodeIR(node_id="n1", label="A-renamed"),
                    DiagramNodeIR(node_id="n2", label="B"),
                ],
                edges=[DiagramEdgeIR(edge_id="e1", source_id="n1", target_id="n2")],
            )
        ],
    )
    diff = diff_diagram_ir(ir1, ir2)
    assert diff["has_changes"] is True
    assert diff["added_nodes"] == ["P1:n2"]
    assert diff["added_edges"] == ["P1:e1"]

    # Migration 041 & EXPECTED_TENANT_RLS_TABLES parity check.
    assert EXPECTED_TENANT_RLS_TABLES["app_connections"] == "namespace_id"
    mig_path = REPO_ROOT / "nce" / "migrations" / "041_app_connections_vault.sql"
    mig_sql = mig_path.read_text(encoding="utf-8")
    assert mig_sql.splitlines()[0].strip() == "-- 041_app_connections_vault.sql"
    assert "ENABLE ROW LEVEL SECURITY" in mig_sql
    assert "FORCE ROW LEVEL SECURITY" in mig_sql
    assert "(SELECT get_nce_namespace())" in mig_sql
    assert "REVOKE ALL ON TABLE app_connections FROM PUBLIC" in mig_sql
