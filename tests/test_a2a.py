"""
tests/test_a2a.py

Phase 3.1 — A2A protocol unit tests (multi-agent isolation, no shared DB).

Uses unique UUIDs per case so parallel pytest workers do not collide.
All Postgres access is mocked unless noted.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.a2a import (
    A2AAuthorizationError,
    A2AGrantRequest,
    A2AMTLSError,
    A2AScope,
    A2AScopeViolationError,
    _normalise_fingerprint,
    _parse_fingerprint_from_cert_dict,
    _parse_sans_from_cert_dict,
    create_grant,
    enforce_scope,
    mtls_enforce,
    parse_client_cert_from_headers,
    parse_client_cert_from_scope,
    validate_mtls_cert,
    verify_token,
)
from nce.auth import NamespaceContext


def _future_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _grant_row(
    *,
    owner_ns: uuid.UUID,
    owner_agent: str,
    consumer_ns: uuid.UUID | None,
    consumer_agent: str | None,
    scopes: list[dict],
    can_delegate: bool = False,
) -> dict:
    return {
        "id": uuid.uuid4(),
        "owner_namespace_id": owner_ns,
        "owner_agent_id": owner_agent,
        "target_namespace_id": consumer_ns,
        "target_agent_id": consumer_agent,
        "scopes": json.dumps(scopes),
        "expires_at": _future_expiry(),
        "status": "active",
        "can_delegate": can_delegate,
    }


class TestEnforceScopeMultiAgent:
    """Agent B must present a grant that covers the resource; memory grants are not wildcards."""

    def test_memory_not_covered_when_grant_is_different_memory(self) -> None:
        mem_a = str(uuid.uuid4())
        mem_b = str(uuid.uuid4())
        ns = str(uuid.uuid4())
        scopes = [A2AScope(resource_type="memory", resource_id=mem_a, permissions=["read"])]
        with pytest.raises(A2AScopeViolationError):
            enforce_scope(scopes, "memory", mem_b, ns)

    def test_memory_not_covered_when_grant_is_only_kg_node(self) -> None:
        node = str(uuid.uuid4())
        mem = str(uuid.uuid4())
        ns = str(uuid.uuid4())
        scopes = [A2AScope(resource_type="kg_node", resource_id=node, permissions=["read"])]
        with pytest.raises(A2AScopeViolationError):
            enforce_scope(scopes, "memory", mem, ns)

    def test_namespace_grant_allows_typed_memory_reads(self) -> None:
        """Namespace-shaped grant authorises memory / kg_node / subgraph resource_type checks."""
        ns = str(uuid.uuid4())
        scopes = [A2AScope(resource_type="namespace", resource_id=ns, permissions=["read"])]
        enforce_scope(scopes, "memory", str(uuid.uuid4()), ns)
        enforce_scope(scopes, "kg_node", str(uuid.uuid4()), ns)

    def test_exact_memory_grant_allows_that_memory(self) -> None:
        mem = str(uuid.uuid4())
        ns = str(uuid.uuid4())
        scopes = [A2AScope(resource_type="memory", resource_id=mem, permissions=["read"])]
        enforce_scope(scopes, "memory", mem, ns)


class TestVerifyTokenIsolation:
    """Token must match consumer namespace / agent bindings from the grant row."""

    @pytest.mark.asyncio
    async def test_wrong_consumer_namespace_raises(self) -> None:
        owner_ns = uuid.uuid4()
        bound_ns = uuid.uuid4()
        wrong_ns = uuid.uuid4()
        assert wrong_ns != bound_ns

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value=_grant_row(
                owner_ns=owner_ns,
                owner_agent="agent-a",
                consumer_ns=bound_ns,
                consumer_agent=None,
                scopes=[
                    {
                        "resource_type": "namespace",
                        "resource_id": str(owner_ns),
                        "permissions": ["read"],
                    }
                ],
            )
        )
        consumer = NamespaceContext(namespace_id=wrong_ns, agent_id="agent-b")
        with pytest.raises(A2AAuthorizationError, match="not valid for this namespace"):
            await verify_token(conn, "nce_a2a_x", consumer)

    @pytest.mark.asyncio
    async def test_unknown_token_raises(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        consumer = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="b")
        with pytest.raises(A2AAuthorizationError, match="Invalid or revoked"):
            await verify_token(conn, "nce_a2a_y", consumer)

    @pytest.mark.asyncio
    async def test_wrong_consumer_agent_raises(self) -> None:
        owner_ns = uuid.uuid4()
        consumer_ns = uuid.uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value=_grant_row(
                owner_ns=owner_ns,
                owner_agent="agent-a",
                consumer_ns=consumer_ns,
                consumer_agent="expected-bot",
                scopes=[
                    {
                        "resource_type": "namespace",
                        "resource_id": str(owner_ns),
                        "permissions": ["read"],
                    }
                ],
            )
        )
        consumer = NamespaceContext(namespace_id=consumer_ns, agent_id="other-bot")
        with pytest.raises(A2AAuthorizationError, match="not valid for this agent"):
            await verify_token(conn, "nce_a2a_za", consumer)

    @pytest.mark.asyncio
    async def test_verify_success_returns_owner(self) -> None:
        owner_ns = uuid.uuid4()
        consumer_ns = uuid.uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value=_grant_row(
                owner_ns=owner_ns,
                owner_agent="agent-a",
                consumer_ns=consumer_ns,
                consumer_agent=None,
                scopes=[
                    {
                        "resource_type": "namespace",
                        "resource_id": str(owner_ns),
                        "permissions": ["read"],
                    }
                ],
            )
        )
        consumer = NamespaceContext(namespace_id=consumer_ns, agent_id="agent-b")
        v = await verify_token(conn, "nce_a2a_zb", consumer)
        assert v.owner_namespace_id == owner_ns
        assert v.owner_agent_id == "agent-a"
        assert len(v.scopes) == 1


class TestVerifyTokenExpiry:
    @pytest.mark.asyncio
    async def test_expired_token_raises_and_marks_expired(self) -> None:
        owner_ns = uuid.uuid4()
        consumer_ns = uuid.uuid4()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "owner_namespace_id": owner_ns,
                "owner_agent_id": "agent-a",
                "target_namespace_id": consumer_ns,
                "target_agent_id": None,
                "scopes": json.dumps(
                    [
                        {
                            "resource_type": "namespace",
                            "resource_id": str(owner_ns),
                            "permissions": ["read"],
                        }
                    ]
                ),
                "expires_at": past,
                "status": "active",
            }
        )
        conn.execute = AsyncMock()
        consumer = NamespaceContext(namespace_id=consumer_ns, agent_id="agent-b")
        with pytest.raises(A2AAuthorizationError, match="expired"):
            await verify_token(conn, "nce_a2a_zc", consumer)
        conn.execute.assert_awaited()


class TestCreateGrantSqlShape:
    """ensure INSERT is invoked (still isolated — no real DB)."""

    @pytest.mark.asyncio
    async def test_create_grant_executes_insert(self) -> None:
        conn = AsyncMock()
        conn.execute = AsyncMock()
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=tx)
        owner = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="owner-agent")
        req = A2AGrantRequest(
            target_namespace_id=uuid.uuid4(),
            target_agent_id="visitor",
            scopes=[
                A2AScope(
                    resource_type="namespace",
                    resource_id=str(owner.namespace_id),
                    permissions=["read"],
                )
            ],
            expires_in_seconds=120,
        )
        with (
            patch("nce.a2a.set_namespace_context", new_callable=AsyncMock),
            patch("nce.event_log.append_event", new_callable=AsyncMock),
        ):
            resp = await create_grant(conn, owner, req)
        assert resp.sharing_token.startswith("nce_a2a_")
        conn.execute.assert_awaited_once()


class TestCreateGrantDelegation:
    """Tests for the can_delegate validation rules inside create_grant."""

    @pytest.mark.asyncio
    async def test_create_grant_other_namespace_without_delegable_grant_raises(self) -> None:
        conn = AsyncMock()
        # Mock database returning False for EXISTS check (no delegable grant exists)
        conn.fetchval = AsyncMock(return_value=False)
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=tx)

        owner = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="owner-agent")
        other_ns = uuid.uuid4()
        req = A2AGrantRequest(
            target_namespace_id=uuid.uuid4(),
            target_agent_id="visitor",
            scopes=[
                A2AScope(
                    resource_type="namespace",
                    resource_id=str(other_ns),
                    permissions=["read"],
                )
            ],
            expires_in_seconds=120,
        )
        with pytest.raises(A2AScopeViolationError, match="does not have delegable access"):
            await create_grant(conn, owner, req)

    @pytest.mark.asyncio
    async def test_create_grant_other_namespace_with_delegable_grant_succeeds(self) -> None:
        conn = AsyncMock()
        # Mock database returning True for EXISTS check (delegable grant exists)
        conn.fetchval = AsyncMock(return_value=True)
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=None)
        conn.transaction = MagicMock(return_value=tx)

        owner = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="owner-agent")
        other_ns = uuid.uuid4()
        req = A2AGrantRequest(
            target_namespace_id=uuid.uuid4(),
            target_agent_id="visitor",
            scopes=[
                A2AScope(
                    resource_type="namespace",
                    resource_id=str(other_ns),
                    permissions=["read"],
                )
            ],
            expires_in_seconds=120,
        )
        with (
            patch("nce.a2a.set_namespace_context", new_callable=AsyncMock),
            patch("nce.event_log.append_event", new_callable=AsyncMock),
        ):
            resp = await create_grant(conn, owner, req)
        assert resp.sharing_token.startswith("nce_a2a_")
        conn.execute.assert_awaited_once()


# ============================================================================
# mTLS Client Certificate Validation
# ============================================================================


class TestNormaliseFingerprint:
    def test_colon_separated_lowercase(self) -> None:
        result = _normalise_fingerprint("AA:BB:CC:DD:EE:FF")
        assert result == "aa:bb:cc:dd:ee:ff"

    def test_raw_hex(self) -> None:
        result = _normalise_fingerprint("aabbccddeeff00112233445566778899")
        assert result == "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"

    def test_mixed_case_with_dashes(self) -> None:
        result = _normalise_fingerprint("AA-BB-CC-DD-EE-FF-00-11")
        assert result == "aa:bb:cc:dd:ee:ff:00:11"

    def test_spaces_removed(self) -> None:
        result = _normalise_fingerprint("AA BB CC DD EE FF")
        assert result == "aa:bb:cc:dd:ee:ff"

    def test_invalid_raises(self) -> None:
        with pytest.raises(A2AMTLSError, match="Invalid fingerprint format"):
            _normalise_fingerprint("not-a-fingerprint!!")


class TestParseSansFromCertDict:
    def test_san_list_of_strings(self) -> None:
        cert = {"san": ["DNS:agent-a.nce.local", "DNS:agent-b.nce.local"]}
        sans = _parse_sans_from_cert_dict(cert)
        assert sans == {"agent-a.nce.local", "agent-b.nce.local"}

    def test_common_name_fallback(self) -> None:
        cert = {"commonName": "agent-c.nce.local"}
        sans = _parse_sans_from_cert_dict(cert)
        assert sans == {"agent-c.nce.local"}

    def test_empty_cert(self) -> None:
        sans = _parse_sans_from_cert_dict({})
        assert sans == set()

    def test_san_list_of_dicts(self) -> None:
        cert = {"subjectAltName": [{"DNS": "svc.internal"}, {"DNS": "svc.external"}]}
        sans = _parse_sans_from_cert_dict(cert)
        assert sans == {"svc.internal", "svc.external"}

    def test_uri_sans(self) -> None:
        cert = {"san": ["URI:spiffe://cluster.local/ns/default/sa/agent"]}
        sans = _parse_sans_from_cert_dict(cert)
        assert "spiffe://cluster.local/ns/default/sa/agent" in sans


class TestParseFingerprintFromCertDict:
    def test_sha256_fingerprint_key(self) -> None:
        cert = {"sha256_fingerprint": "aa:bb:cc:dd:ee:ff:00:11"}
        fp = _parse_fingerprint_from_cert_dict(cert)
        assert fp == "aa:bb:cc:dd:ee:ff:00:11"

    def test_sha256_key(self) -> None:
        cert = {"sha256": "AABBCCDDEEFF0011"}
        fp = _parse_fingerprint_from_cert_dict(cert)
        assert fp == "aa:bb:cc:dd:ee:ff:00:11"

    def test_generic_fingerprint_key(self) -> None:
        cert = {"fingerprint": "aa:bb:cc:dd:ee:ff"}
        fp = _parse_fingerprint_from_cert_dict(cert)
        assert fp == "aa:bb:cc:dd:ee:ff"

    def test_no_fingerprint_returns_none(self) -> None:
        fp = _parse_fingerprint_from_cert_dict({})
        assert fp is None

    def test_unparseable_fingerprint_returns_none(self) -> None:
        cert = {"fingerprint": "garbage!!!"}
        fp = _parse_fingerprint_from_cert_dict(cert)
        assert fp is None


class TestParseClientCertFromHeaders:
    def test_caddy_style_x_forwarded_client_cert(self) -> None:
        headers = {
            "x-forwarded-client-cert": (
                "Hash=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99;"
                "SAN=agent-a.internal;"
                "Subject=CN=agent-a.internal"
            )
        }
        cert = parse_client_cert_from_headers(headers)
        assert cert is not None
        assert cert["fingerprint"] == "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
        assert "agent-a.internal" in cert.get("san", [])

    def test_dedicated_headers(self) -> None:
        headers = {
            "x-client-cert-fingerprint": "aabbccddeeff0011223344556677889900112233445566778899001122334455aabbccddeeff0011223344556677889900112233445566778899001122334455aabbccddeeff00112233445566778899",
            "x-client-cert-san": "svc1.internal,svc2.internal",
            "x-client-cert-cn": "svc1.internal",
        }
        cert = parse_client_cert_from_headers(headers)
        assert cert is not None
        assert cert["fingerprint"].startswith("aa:bb")
        assert "svc1.internal" in cert.get("san", [])
        assert "svc2.internal" in cert.get("san", [])

    def test_no_proxy_headers_returns_none(self) -> None:
        cert = parse_client_cert_from_headers({})
        assert cert is None

    def test_unparseable_fingerprint_in_header_warns_but_proceeds(self) -> None:
        headers = {
            "x-forwarded-client-cert": "Hash=bad!!!;SAN=agent.internal",
        }
        cert = parse_client_cert_from_headers(headers)
        assert cert is not None
        assert "agent.internal" in cert.get("san", [])

    def test_traefik_x_forwarded_tls_client_cert_pem(self) -> None:
        """Traefik sends full PEM cert in X-Forwarded-Tls-Client-Cert."""
        from datetime import datetime, timedelta, timezone

        from cryptography import x509 as cx
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = cx.Name([cx.NameAttribute(cx.NameOID.COMMON_NAME, "agent.traefik.test")])
        san = cx.SubjectAlternativeName([cx.DNSName("agent.traefik.test")])
        now = datetime.now(timezone.utc)
        cert = (
            cx.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(cx.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=1))
            .add_extension(san, critical=False)
            .sign(key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

        result = parse_client_cert_from_headers({"x-forwarded-tls-client-cert": pem})
        assert result is not None
        assert "fingerprint" in result
        assert "agent.traefik.test" in result.get("san", [])
        assert result.get("commonName") == "agent.traefik.test"
        assert result.get("pem", "").strip() == pem.strip()

    def test_traefik_base64_encoded_pem(self) -> None:
        """Traefik may base64-encode the PEM cert."""
        import base64
        from datetime import datetime, timedelta, timezone

        from cryptography import x509 as cx
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = cx.Name([cx.NameAttribute(cx.NameOID.COMMON_NAME, "b64.agent.test")])
        now = datetime.now(timezone.utc)
        cert = (
            cx.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(cx.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        b64_pem = base64.b64encode(pem.encode("ascii")).decode("ascii")

        result = parse_client_cert_from_headers({"x-forwarded-tls-client-cert": b64_pem})
        assert result is not None
        assert result.get("commonName") == "b64.agent.test"

    def test_traefik_invalid_pem_returns_none(self) -> None:
        result = parse_client_cert_from_headers({"x-forwarded-tls-client-cert": "not-a-cert"})
        assert result is None


class TestParseClientCertFromScope:
    def test_no_ssl_object_returns_none(self) -> None:
        cert = parse_client_cert_from_scope({})
        assert cert is None

    def test_empty_dict_ssl_object_returns_none(self) -> None:
        cert = parse_client_cert_from_scope({"ssl_object": {}})
        assert cert is None

    def test_dict_ssl_object_with_data(self) -> None:
        scope = {
            "ssl_object": {
                "sha256_fingerprint": "aa:bb:cc:dd:ee:ff:00:11",
                "san": ["DNS:agent.internal"],
                "commonName": "agent.internal",
            }
        }
        cert = parse_client_cert_from_scope(scope)
        assert cert is not None
        assert cert["sha256_fingerprint"] == "aa:bb:cc:dd:ee:ff:00:11"
        assert "DNS:agent.internal" in cert["san"]


class TestValidateMTLSCert:
    def test_fingerprint_match(self) -> None:
        cert = {"sha256_fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"}
        result = validate_mtls_cert(
            cert,
            allowed_fingerprints=["AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"],
        )
        assert result.startswith("fp:")

    def test_san_match(self) -> None:
        cert = {"san": ["DNS:agent-a.internal"]}
        result = validate_mtls_cert(
            cert,
            allowed_sans=["agent-a.internal"],
        )
        assert result == "san:agent-a.internal"

    def test_no_match_raises(self) -> None:
        cert = {"sha256_fingerprint": "11:22:33:44:55:66:77:88:99:00:aa:bb:cc:dd:ee:ff"}
        with pytest.raises(A2AMTLSError, match="not in allowlist"):
            validate_mtls_cert(
                cert,
                allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"],
            )

    def test_no_allowlists_configured_raises(self) -> None:
        cert = {"san": ["agent.internal"]}
        with pytest.raises(A2AMTLSError, match="no allowed SANs or fingerprints"):
            validate_mtls_cert(cert)

    def test_case_insensitive_fingerprint(self) -> None:
        cert = {"fingerprint": "AA:BB:CC:DD:EE:FF:00:11"}
        result = validate_mtls_cert(
            cert,
            allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11"],
        )
        assert result == "fp:aa:bb:cc:dd:ee:ff:00:11"

    def test_case_insensitive_san(self) -> None:
        cert = {"san": ["Agent-A.INTERNAL"]}
        result = validate_mtls_cert(
            cert,
            allowed_sans=["agent-a.internal"],
        )
        assert result == "san:agent-a.internal"

    def test_self_signed_not_in_allowlist_rejected(self) -> None:
        """Self-signed cert with unknown fingerprint must be rejected."""
        cert = {
            "sha256_fingerprint": "de:ad:be:ef:00:00:00:00:00:00:00:00:00:00:00:01",
            "san": ["fake.evil.corp"],
        }
        with pytest.raises(A2AMTLSError, match="not in allowlist"):
            validate_mtls_cert(
                cert,
                allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"],
                allowed_sans=["agent-a.internal"],
            )

    def test_fingerprint_takes_precedence_over_san(self) -> None:
        """Fingerprint match should return fp: prefix even if SAN also matches."""
        cert = {
            "sha256_fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
            "san": ["also-matches.internal"],
        }
        result = validate_mtls_cert(
            cert,
            allowed_sans=["also-matches.internal"],
            allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"],
        )
        assert result.startswith("fp:")


class TestMTLSEnforce:
    def test_disabled_returns_none(self) -> None:
        result = mtls_enforce(
            scope={},
            headers={},
            enabled=False,
        )
        assert result is None

    def test_strict_mode_no_cert_raises(self) -> None:
        with pytest.raises(A2AMTLSError, match="no client certificate presented"):
            mtls_enforce(
                scope={},
                headers={},
                enabled=True,
                strict=True,
                trusted_proxy_hops=0,
                allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11"],
            )

    def test_non_strict_no_cert_returns_none(self) -> None:
        result = mtls_enforce(
            scope={},
            headers={},
            enabled=True,
            strict=False,
            trusted_proxy_hops=0,
            allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11"],
        )
        assert result is None

    def test_proxy_header_takes_precedence(self) -> None:
        """When trusted_proxy_hops > 0, headers are parsed first."""
        headers = {
            "x-client-cert-fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
        }
        result = mtls_enforce(
            scope={},
            headers=headers,
            enabled=True,
            strict=True,
            trusted_proxy_hops=1,
            allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"],
        )
        assert result is not None

    def test_falls_back_to_scope_when_proxy_headers_empty(self) -> None:
        """When proxy headers are present but empty, fall back to ASGI scope."""
        scope = {
            "ssl_object": {
                "sha256_fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
            }
        }
        result = mtls_enforce(
            scope=scope,
            headers={"x-forwarded-client-cert": ""},
            enabled=True,
            strict=True,
            trusted_proxy_hops=1,
            allowed_fingerprints=["aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"],
        )
        assert result is not None

    def test_no_allowlist_raises(self) -> None:
        with pytest.raises(A2AMTLSError, match="no allowed SANs or fingerprints"):
            mtls_enforce(
                scope={"ssl_object": {"sha256_fingerprint": "aa:bb:cc:dd:ee:ff:00:11"}},
                headers={},
                enabled=True,
                strict=True,
                trusted_proxy_hops=0,
                allowed_sans=[],
                allowed_fingerprints=[],
            )


class TestA2AMTLSError:
    def test_has_correct_code(self) -> None:
        exc = A2AMTLSError("test")
        assert exc.code == -32010

    def test_can_be_caught_as_exception(self) -> None:
        with pytest.raises(A2AMTLSError, match="denied"):
            raise A2AMTLSError("Access denied")
