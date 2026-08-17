"""
Tests for SSRF guard — ``validate_base_url()`` and ``validate_base_url_async()``.

Covers all 4 private IP ranges, loopback, HTTPS enforcement, HTTP/loopback
allowed flags, invalid URLs, and unresolvable hostnames.

All DNS lookups are mocked via ``monkeypatch`` so tests are deterministic
and do not require a live network.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from nce.net_safety import (
    BridgeURLValidationError,
    validate_extractor_url,
    validate_webhook_payload_url,
)
from nce.providers.base import LLMProviderError, validate_base_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_getaddrinfo(ip_str: str, family: int = socket.AF_INET):
    """Return a function that replaces ``socket.getaddrinfo`` and returns *ip_str*."""

    def mock_getaddrinfo(
        hostname: str,
        port: Any = None,
        family: Any = 0,
        type: Any = 0,
        proto: Any = 0,
        flags: Any = 0,
    ):
        return [(family, socket.SOCK_STREAM, 6, "", (ip_str, 0))]

    return mock_getaddrinfo


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------


class TestValidateBaseUrl:
    """Synchronous ``validate_base_url()``."""

    # (url, allow_http, allow_loopback, should_pass, msg_substring)
    # Use parametrize at the method level
    @pytest.mark.parametrize(
        "url, allow_http, allow_loopback, expect_pass, match",
        [
            # --- Public HTTPS URLs — should pass ---
            ("https://api.openai.com/v1", False, False, True, None),
            ("https://www.google.com:443", False, False, True, None),
            ("https://8.8.8.8", False, False, True, None),
            # --- HTTP without flag — should fail ---
            ("http://api.openai.com/v1", False, False, False, "must use HTTPS"),
            # --- HTTP with allow_http — should pass ---
            ("http://cognitive:11435", True, True, True, None),
            # --- Loopback — should fail without flag ---
            ("https://127.0.0.1:8000", False, False, False, "private IP|non-public IP|loopback"),
            ("https://localhost", False, False, False, "private IP|non-public IP|loopback"),
            ("https://[::1]:8000", False, False, False, "private IP|non-public IP|loopback"),
            # --- Loopback with allow_loopback — should pass ---
            ("https://127.0.0.1:8000", False, True, True, None),
            ("https://localhost:11435", False, True, True, None),
            # --- Private IPv4 — should fail ---
            ("https://10.0.0.1", False, False, False, "private IP"),
            ("https://10.255.255.255", False, False, False, "private IP"),
            ("https://172.16.0.1", False, False, False, "private IP"),
            ("https://172.31.255.255", False, False, False, "private IP"),
            ("https://192.168.1.1", False, False, False, "private IP"),
            ("https://192.168.255.255", False, False, False, "private IP"),
            # --- Private IPv6 — should fail ---
            ("https://[fd00::1]", False, False, False, "private IP"),
            # --- Invalid URL format ---
            ("not-a-url", False, False, False, "invalid base_url"),
            ("", False, False, False, "invalid base_url"),
            # --- Unresolvable hostname ---
            (
                "https://does-not-exist.example",
                False,
                False,
                False,
                "could not resolve",
            ),
        ],
    )
    def test_scenario(
        self,
        url: str,
        allow_http: bool,
        allow_loopback: bool,
        expect_pass: bool,
        match: str | None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Run a single SSRF guard scenario with mocked DNS."""

        # Map hostnames to fake IPs for deterministic mock
        host_to_ip = {
            "api.openai.com": "1.2.3.4",
            "www.google.com": "142.250.80.4",
            "8.8.8.8": "8.8.8.8",
            "cognitive": "127.0.0.1",
            "127.0.0.1": "127.0.0.1",
            "localhost": "127.0.0.1",
            "::1": "::1",
            "10.0.0.1": "10.0.0.1",
            "10.255.255.255": "10.255.255.255",
            "172.16.0.1": "172.16.0.1",
            "172.31.255.255": "172.31.255.255",
            "192.168.1.1": "192.168.1.1",
            "192.168.255.255": "192.168.255.255",
            "fd00::1": "fd00::1",
        }

        # Build the mock
        from urllib.parse import urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        if hostname == "does-not-exist.example":
            # Simulate DNS failure
            def _fail_getaddrinfo(*args: Any, **kwargs: Any) -> list:
                raise socket.gaierror("[Mock] Name or service not known")

            monkeypatch.setattr("socket.getaddrinfo", _fail_getaddrinfo)
        elif hostname in host_to_ip:
            ip = host_to_ip[hostname]
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            monkeypatch.setattr(
                "socket.getaddrinfo",
                _mock_getaddrinfo(ip, family),
            )
        elif not hostname:
            pass  # Invalid URL — fails before DNS
        else:
            # Fallback — mock to a public IP for unlisted hostnames
            monkeypatch.setattr(
                "socket.getaddrinfo",
                _mock_getaddrinfo("1.2.3.4"),
            )

        if expect_pass:
            validate_base_url(url, allow_http=allow_http, allow_loopback=allow_loopback)
        else:
            with pytest.raises(LLMProviderError, match=match or ""):
                validate_base_url(url, allow_http=allow_http, allow_loopback=allow_loopback)


# ---------------------------------------------------------------------------
# Async variant smoke test
# ---------------------------------------------------------------------------


class TestValidateBaseUrlAsync:
    """Quick smoke test for the async variant."""

    @pytest.mark.asyncio
    async def test_public_https_passes(self, monkeypatch: pytest.MonkeyPatch):
        from nce.providers.base import validate_base_url_async

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        await validate_base_url_async("https://api.openai.com/v1")

    @pytest.mark.asyncio
    async def test_loopback_rejected(self, monkeypatch: pytest.MonkeyPatch):
        from nce.providers.base import validate_base_url_async

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1"))
        with pytest.raises(LLMProviderError, match="private IP|non-public IP|loopback"):
            await validate_base_url_async("https://localhost:8000")


# ---------------------------------------------------------------------------
# Webhook payload URL validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestValidateWebhookPayloadUrl:
    """``validate_webhook_payload_url()`` — SSRF guard for incoming webhook payloads."""

    # --- Relative resource paths — accept known-safe Graph prefixes ---

    async def test_accepts_sites_resource_path(self):
        assert (
            await validate_webhook_payload_url("/sites/abc-123/drives/def-456/root")
            == "/sites/abc-123/drives/def-456/root"
        )

    async def test_accepts_users_resource_path(self):
        assert (
            await validate_webhook_payload_url("/users/user@tenant/drive/root")
            == "/users/user@tenant/drive/root"
        )

    async def test_accepts_drives_resource_path(self):
        assert (
            await validate_webhook_payload_url("/drives/drive-id/root/delta")
            == "/drives/drive-id/root/delta"
        )

    async def test_accepts_groups_resource_path(self):
        assert (
            await validate_webhook_payload_url("/groups/group-id/conversations")
            == "/groups/group-id/conversations"
        )

    async def test_accepts_me_resource_path(self):
        assert await validate_webhook_payload_url("/me/drive/root") == "/me/drive/root"

    async def test_rejects_arbitrary_resource_path(self):
        with pytest.raises(BridgeURLValidationError, match="does not match"):
            await validate_webhook_payload_url("/internal/admin/panel")

    async def test_rejects_path_traversal_resource(self):
        with pytest.raises(BridgeURLValidationError, match="does not match"):
            await validate_webhook_payload_url("/../../internal/admin")

    # --- Fully-qualified URLs — enforce HTTPS and SSRF IP checks ---

    async def test_accepts_public_graph_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))
        result = await validate_webhook_payload_url(
            "https://graph.microsoft.com/v1.0/sites/abc/drives/def/root"
        )
        assert result.startswith("https://graph.microsoft.com/")

    async def test_accepts_googleapis_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("142.250.80.4"))
        result = await validate_webhook_payload_url("https://www.googleapis.com/drive/v3/changes")
        assert result.startswith("https://www.googleapis.com/")

    async def test_rejects_http_scheme(self):
        with pytest.raises(BridgeURLValidationError, match="only https"):
            await validate_webhook_payload_url("http://evil.internal/admin")

    async def test_rejects_private_ip_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("10.0.0.1"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_webhook_payload_url("https://internal.secret/admin")

    async def test_rejects_loopback_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_webhook_payload_url("https://localhost:8000/admin")

    async def test_rejects_unknown_url_prefix(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        with pytest.raises(BridgeURLValidationError, match="not within allowed prefixes"):
            await validate_webhook_payload_url("https://evil.example.com/hack")

    async def test_rejects_empty_url(self):
        with pytest.raises(BridgeURLValidationError, match="empty URL"):
            await validate_webhook_payload_url("")

    async def test_rejects_blank_url(self):
        with pytest.raises(BridgeURLValidationError, match="empty URL"):
            await validate_webhook_payload_url("   ")


# ---------------------------------------------------------------------------
# Extractor URL validation — SSRF guard for ingestion extractors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestValidateExtractorUrl:
    """``validate_extractor_url()`` — SSRF guard for diagram API extractors."""

    # --- Happy path: public HTTPS URLs ---

    async def test_accepts_miro_base_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))
        result = await validate_extractor_url("https://api.miro.com/v2")
        assert result == "https://api.miro.com/v2"

    async def test_accepts_lucid_base_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("52.32.1.10"))
        result = await validate_extractor_url("https://api.lucid.co")
        assert result == "https://api.lucid.co"

    async def test_accepts_generic_public_https_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        result = await validate_extractor_url("https://public-api.example.com/v1")
        assert result == "https://public-api.example.com/v1"

    # --- Scheme enforcement ---

    async def test_rejects_http_scheme(self):
        with pytest.raises(BridgeURLValidationError, match="only https"):
            await validate_extractor_url("http://api.miro.com/v2")

    async def test_rejects_ftp_scheme(self):
        with pytest.raises(BridgeURLValidationError, match="only https"):
            await validate_extractor_url("ftp://files.internal/export")

    async def test_rejects_no_scheme(self):
        with pytest.raises(BridgeURLValidationError, match="only https"):
            await validate_extractor_url("api.miro.com/v2")

    # --- Private IPv4 ranges ---

    @pytest.mark.parametrize(
        "url,label",
        [
            ("https://10.0.0.1/api", "10.x"),
            ("https://10.255.255.255/api", "10.x upper"),
            ("https://172.16.0.1/api", "172.16.x"),
            ("https://172.31.255.255/api", "172.31.x upper"),
            ("https://192.168.1.1/api", "192.168.x"),
            ("https://192.168.255.255/api", "192.168.x upper"),
        ],
    )
    async def test_rejects_private_ipv4(
        self, url: str, label: str, monkeypatch: pytest.MonkeyPatch
    ):
        """All RFC 1918 private IPv4 ranges are blocked."""
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo(host))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url(url)

    # --- Loopback ---

    @pytest.mark.parametrize(
        "url,ip",
        [
            ("https://127.0.0.1:8000/api", "127.0.0.1"),
            ("https://localhost/admin", "127.0.0.1"),
            ("https://[::1]:8000/api", "::1"),
        ],
    )
    async def test_rejects_loopback(self, url: str, ip: str, monkeypatch: pytest.MonkeyPatch):
        """All loopback addresses are blocked."""
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo(ip, family))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url(url)

    # --- Link-local ---

    async def test_rejects_link_local(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://169.254.169.254/latest/meta-data")

    # --- AWS / cloud metadata ---

    async def test_rejects_aws_metadata_hostname(self, monkeypatch: pytest.MonkeyPatch):
        """Block resolution even if hostname is not a literal IP."""
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://metadata.aws.internal/latest/meta-data")

    # --- Private IPv6 ---

    async def test_rejects_private_ipv6(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("fd00::1", socket.AF_INET6))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://[fd00::1]/api")

    @pytest.mark.parametrize(
        "ip_literal",
        [
            "fc00::1",
            "fe80::2",
            "fec0::1",
            "2001:db8::1",
            "100::42",
        ],
    )
    async def test_rejects_explicit_ipv6_ssrf_subnets(
        self, ip_literal: str, monkeypatch: pytest.MonkeyPatch
    ):
        """ULA, link-local, site-local, documentation, discard prefixes (CIDR denylist)."""
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo(ip_literal, socket.AF_INET6))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url(f"https://[{ip_literal}]/api")

    async def test_rejects_ipv6_zone_id_sockaddr(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            _mock_getaddrinfo("fe80::1%eth0", socket.AF_INET6),
        )
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://internal.example/api")

    async def test_accepts_public_ipv6_bracket_sockaddr(self, monkeypatch: pytest.MonkeyPatch):
        """Bracket-wrapped IPv6 in sockaddr is normalized and can be non-public-checked."""

        def mock_getaddrinfo(
            hostname: str,
            port: Any = None,
            family: Any = 0,
            type: Any = 0,
            proto: Any = 0,
            flags: Any = 0,
        ):
            return [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("[2606:4700:4700::1111]", 0),
                )
            ]

        monkeypatch.setattr("socket.getaddrinfo", mock_getaddrinfo)
        result = await validate_extractor_url("https://one.one.one.one/api")
        assert result.startswith("https://")

    # --- Multicast ---

    async def test_rejects_multicast(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("224.0.0.1"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://224.0.0.1/api")

    # --- Invalid / empty URLs ---

    async def test_rejects_empty_url(self):
        with pytest.raises(BridgeURLValidationError, match="empty URL"):
            await validate_extractor_url("")

    async def test_rejects_blank_url(self):
        with pytest.raises(BridgeURLValidationError, match="empty URL"):
            await validate_extractor_url("   ")

    async def test_rejects_invalid_url(self):
        with pytest.raises(BridgeURLValidationError, match="only https"):
            await validate_extractor_url("not-a-valid-url")

    # --- Unresolvable hostname ---

    async def test_rejects_unresolvable_hostname(self, monkeypatch: pytest.MonkeyPatch):
        def _fail_dns(*args: Any, **kwargs: Any) -> list:
            raise socket.gaierror("[Mock] Name or service not known")

        monkeypatch.setattr("socket.getaddrinfo", _fail_dns)
        with pytest.raises(BridgeURLValidationError, match="cannot resolve"):
            await validate_extractor_url("https://does-not-exist.invalid/api")


# ---------------------------------------------------------------------------
# Integration: diagram_api extractors reject malicious base_url
# ---------------------------------------------------------------------------


class TestDiagramApiExtractorSSRF:
    """Verify that ``miro_extract_board`` and ``lucidchart_extract_document``
    gracefully reject a malicious ``base_url`` parameter pointing at internal
    network resources."""

    @pytest.mark.asyncio
    async def test_miro_rejects_private_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Miro extractor must return empty_skipped when base_url resolves to private IP."""
        from nce.extractors.diagram_api import miro_extract_board

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("10.0.0.1"))
        monkeypatch.setenv("NCE_MIRO_ACCESS_TOKEN", "fake-token")

        result = await miro_extract_board(
            "board-123",
            base_url="https://internal.secret/api",
        )
        assert result.skipped is True
        assert result.skip_reason == "ssrf_blocked"
        assert any("non-public" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_miro_rejects_loopback_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Miro extractor must reject loopback base_url."""
        from nce.extractors.diagram_api import miro_extract_board

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1"))
        monkeypatch.setenv("NCE_MIRO_ACCESS_TOKEN", "fake-token")

        result = await miro_extract_board(
            "board-123",
            base_url="https://localhost:8000",
        )
        assert result.skipped is True
        assert result.skip_reason == "ssrf_blocked"

    @pytest.mark.asyncio
    async def test_miro_rejects_http_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Miro extractor must reject HTTP base_url (no downgrade attack)."""
        from nce.extractors.diagram_api import miro_extract_board

        monkeypatch.setenv("NCE_MIRO_ACCESS_TOKEN", "fake-token")

        result = await miro_extract_board(
            "board-123",
            base_url="http://api.miro.com/v2",
        )
        assert result.skipped is True
        assert result.skip_reason == "ssrf_blocked"
        assert any("only https" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_miro_accepts_default_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Miro extractor must accept the default base_url (no SSRF false positive)."""
        from nce.extractors.diagram_api import miro_extract_board

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))
        monkeypatch.setenv("NCE_MIRO_ACCESS_TOKEN", "fake-token")

        # The default base_url passes validation; the actual HTTP call will
        # fail because we haven't mocked httpx — but SSRF guard must not block.
        result = await miro_extract_board("board-123")
        # It should NOT be ssrf_blocked (it'll fail downstream, but not here)
        assert result.skip_reason != "ssrf_blocked"

    @pytest.mark.asyncio
    async def test_lucid_rejects_private_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Lucid extractor must return empty_skipped when base_url resolves to private IP."""
        from nce.extractors.diagram_api import lucidchart_extract_document

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("192.168.1.1"))
        monkeypatch.setenv("NCE_LUCID_ACCESS_TOKEN", "fake-token")

        result = await lucidchart_extract_document(
            "doc-456",
            base_url="https://admin-panel.internal",
        )
        assert result.skipped is True
        assert result.skip_reason == "ssrf_blocked"
        assert any("non-public" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_lucid_rejects_aws_metadata_url(self, monkeypatch: pytest.MonkeyPatch):
        """Lucid extractor must block AWS metadata endpoint (169.254.169.254)."""
        from nce.extractors.diagram_api import lucidchart_extract_document

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254"))
        monkeypatch.setenv("NCE_LUCID_ACCESS_TOKEN", "fake-token")

        result = await lucidchart_extract_document(
            "doc-456",
            base_url="https://169.254.169.254/latest/meta-data",
        )
        assert result.skipped is True
        assert result.skip_reason == "ssrf_blocked"

    @pytest.mark.asyncio
    async def test_lucid_rejects_http_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Lucid extractor must reject HTTP base_url."""
        from nce.extractors.diagram_api import lucidchart_extract_document

        monkeypatch.setenv("NCE_LUCID_ACCESS_TOKEN", "fake-token")

        result = await lucidchart_extract_document(
            "doc-456",
            base_url="http://api.lucid.co",
        )
        assert result.skipped is True
        assert result.skip_reason == "ssrf_blocked"

    @pytest.mark.asyncio
    async def test_lucid_accepts_default_base_url(self, monkeypatch: pytest.MonkeyPatch):
        """Lucid extractor must accept the default base_url."""
        from nce.extractors.diagram_api import lucidchart_extract_document

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo("52.32.1.10"))
        monkeypatch.setenv("NCE_LUCID_ACCESS_TOKEN", "fake-token")

        result = await lucidchart_extract_document("doc-456")
        assert result.skip_reason != "ssrf_blocked"
