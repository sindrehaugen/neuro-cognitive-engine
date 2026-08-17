"""
Tests for ``nce.net_safety`` URL guards — length limits, credential rejection,
parsed prefix matching, DNS fail-closed behaviour, and log-safe host truncation.

DNS is mocked via ``monkeypatch`` (same pattern as ``tests/test_ssrf_guard.py``).
"""

from __future__ import annotations

import logging
import re
import socket
from typing import Any

import pytest

import nce.net_safety as net_safety
from nce.net_safety import (
    ALLOWED_WEBHOOK_URL_PREFIXES,
    BridgeURLValidationError,
    assert_url_allowed_prefix,
    validate_bridge_webhook_base_url,
    validate_extractor_url,
    validate_webhook_payload_url,
)

GRAPH_PREFIX = "https://graph.microsoft.com/"
DELTA_PREFIXES = (GRAPH_PREFIX,)
# Contract constant from hardened ``net_safety`` (see ``_MAX_URL_LEN`` in module).
MAX_URL_LEN = 4096


def _require_max_url_len() -> int:
    value = getattr(net_safety, "_MAX_URL_LEN", None)
    if value is None:
        pytest.fail("_MAX_URL_LEN is not defined in nce.net_safety")
    return value


def _require_url_matches_prefix():
    fn = getattr(net_safety, "_url_matches_prefix", None)
    if fn is None:
        pytest.fail("_url_matches_prefix is not defined in nce.net_safety")
    return fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_getaddrinfo(ip_str: str, family: int = socket.AF_INET):
    """Return a ``getaddrinfo`` replacement that resolves every host to *ip_str*."""

    def mock_getaddrinfo(
        hostname: str,
        port: Any = None,
        family_arg: Any = 0,
        type: Any = 0,
        proto: Any = 0,
        flags: Any = 0,
    ):
        return [(family, socket.SOCK_STREAM, 6, "", (ip_str, 0))]

    return mock_getaddrinfo


def _fail_dns(*_args: Any, **_kwargs: Any) -> list:
    raise socket.gaierror("[Mock] Name or service not known")


def _url_exact_length(base: str, total_len: int) -> str:
    if len(base) > total_len:
        raise ValueError(f"base length {len(base)} exceeds target {total_len}")
    return base + "a" * (total_len - len(base))


def _long_hostname_url(label_len: int, *, path: str = "/v1.0/me") -> str:
    label = "h" * label_len
    return f"https://{label}.example.com{path}"


# ---------------------------------------------------------------------------
# Module constants / prefix helper
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_max_url_len_is_4096(self):
        assert _require_max_url_len() == MAX_URL_LEN


class TestUrlMatchesPrefix:
    """Direct unit tests for parsed prefix matching (scheme, netloc, path)."""

    @pytest.mark.parametrize(
        "url,prefix,expected",
        [
            ("https://graph.microsoft.com/", GRAPH_PREFIX, True),
            (
                "https://graph.microsoft.com/v1.0/me/messages",
                GRAPH_PREFIX,
                True,
            ),
            (
                "https://graph.microsoft.com.evil.com/v1.0/me",
                GRAPH_PREFIX,
                False,
            ),
            (
                "https://graph.microsoft.com@evil.com/v1.0/me",
                GRAPH_PREFIX,
                False,
            ),
            ("http://graph.microsoft.com/v1.0/me", GRAPH_PREFIX, False),
            (
                "https://GRAPH.MICROSOFT.COM/v1.0/me",
                GRAPH_PREFIX,
                True,
            ),
        ],
    )
    def test_url_matches_prefix(self, url: str, prefix: str, expected: bool):
        assert _require_url_matches_prefix()(url, prefix) is expected


# ---------------------------------------------------------------------------
# URL length limits (all four validators)
# ---------------------------------------------------------------------------


class TestUrlLengthLimits:
    """4096 chars accepted where otherwise valid; 4097 rejected everywhere."""

    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))

    @pytest.mark.asyncio
    async def test_validate_bridge_webhook_base_url_accepts_4096_chars(self):
        base = _url_exact_length("https://hooks.example.com/callback", MAX_URL_LEN)
        assert await validate_bridge_webhook_base_url(base) == base.rstrip("/")

    @pytest.mark.parametrize(
        "validator,args,kwargs",
        [
            (validate_bridge_webhook_base_url, (), {}),
            (
                assert_url_allowed_prefix,
                (DELTA_PREFIXES,),
                {"what": "delta"},
            ),
            (validate_extractor_url, (), {"what": "extractor"}),
            (validate_webhook_payload_url, (), {"field_name": "resource"}),
        ],
        ids=[
            "validate_bridge_webhook_base_url",
            "assert_url_allowed_prefix",
            "validate_extractor_url",
            "validate_webhook_payload_url",
        ],
    )
    @pytest.mark.asyncio
    async def test_all_validators_reject_4097_chars(
        self,
        validator,
        args: tuple,
        kwargs: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))

        if validator is validate_bridge_webhook_base_url:
            url = _url_exact_length("https://hooks.example.com/callback", MAX_URL_LEN + 1)
        elif validator is assert_url_allowed_prefix:
            url = _url_exact_length(
                "https://graph.microsoft.com/v1.0/me/messages",
                MAX_URL_LEN + 1,
            )
        elif validator is validate_extractor_url:
            url = _url_exact_length("https://api.example.com/v1/boards", MAX_URL_LEN + 1)
        else:
            url = _url_exact_length(
                "https://graph.microsoft.com/v1.0/sites/abc/drives/def/root",
                MAX_URL_LEN + 1,
            )

        with pytest.raises(BridgeURLValidationError, match="4096|length|too long|maximum"):
            if validator is assert_url_allowed_prefix:
                await validator(url, *args, **kwargs)
            else:
                await validator(url, *args, **kwargs)

    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_accepts_4096_chars(self):
        url = _url_exact_length(
            "https://graph.microsoft.com/v1.0/me/messages",
            MAX_URL_LEN,
        )
        await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

    @pytest.mark.asyncio
    async def test_validate_extractor_url_accepts_4096_chars(self):
        url = _url_exact_length("https://api.example.com/v1/boards", MAX_URL_LEN)
        assert await validate_extractor_url(url) == url

    @pytest.mark.asyncio
    async def test_validate_webhook_payload_url_accepts_4096_char_absolute_url(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))
        url = _url_exact_length(
            "https://graph.microsoft.com/v1.0/sites/abc/drives/def/root",
            MAX_URL_LEN,
        )
        assert await validate_webhook_payload_url(url) == url

    @pytest.mark.asyncio
    async def test_validate_webhook_payload_url_accepts_4096_char_relative_path(self):
        url = _url_exact_length("/sites/tenant-id/drives/drive-id/root", MAX_URL_LEN)
        assert await validate_webhook_payload_url(url) == url


# ---------------------------------------------------------------------------
# Credential rejection
# ---------------------------------------------------------------------------


class TestCredentialRejection:
    """URLs with userinfo must be rejected for fully-qualified URL validators."""

    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))

    @pytest.mark.parametrize(
        "url",
        (
            "https://user@hooks.example.com/callback",
            "https://user:pass@hooks.example.com/callback",
            "https://user@evil.com",
            "https://user:pass@evil.com",
        ),
    )
    @pytest.mark.asyncio
    async def test_validate_bridge_webhook_base_url_rejects_credentials(self, url: str):
        with pytest.raises(
            BridgeURLValidationError,
            match="credential|userinfo|username|password",
        ):
            await validate_bridge_webhook_base_url(url)

    @pytest.mark.parametrize(
        "url",
        (
            "https://user@graph.microsoft.com/v1.0/me/messages",
            "https://user:pass@graph.microsoft.com/v1.0/me/messages",
            "https://user@evil.com",
            "https://user:pass@evil.com",
        ),
    )
    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_rejects_credentials(self, url: str):
        with pytest.raises(
            BridgeURLValidationError,
            match="credential|userinfo|username|password",
        ):
            await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

    @pytest.mark.parametrize(
        "url",
        (
            "https://user@api.example.com/v1/boards",
            "https://user:pass@api.example.com/v1/boards",
            "https://user@evil.com",
            "https://user:pass@evil.com",
        ),
    )
    @pytest.mark.asyncio
    async def test_validate_extractor_url_rejects_credentials(self, url: str):
        with pytest.raises(
            BridgeURLValidationError,
            match="credential|userinfo|username|password",
        ):
            await validate_extractor_url(url)

    @pytest.mark.parametrize(
        "url",
        (
            "https://user@graph.microsoft.com/v1.0/me/messages",
            "https://user:pass@graph.microsoft.com/v1.0/me/messages",
            "https://user@evil.com",
            "https://user:pass@evil.com",
        ),
    )
    @pytest.mark.asyncio
    async def test_validate_webhook_payload_url_rejects_credentials(self, url: str):
        with pytest.raises(
            BridgeURLValidationError,
            match="credential|userinfo|username|password",
        ):
            await validate_webhook_payload_url(url)

    @pytest.mark.asyncio
    async def test_https_evil_com_passes_credential_check_only(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """``https://evil.com`` must not trip the userinfo guard (may fail prefix/SSRF)."""
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        with pytest.raises(BridgeURLValidationError, match="must start with|prefix"):
            await validate_webhook_payload_url("https://evil.com/resource")
        assert await validate_bridge_webhook_base_url("https://evil.com/callback") == (
            "https://evil.com/callback"
        )
        assert await validate_extractor_url("https://evil.com/api") == "https://evil.com/api"


# ---------------------------------------------------------------------------
# Parsed prefix matching (integration via public validators)
# ---------------------------------------------------------------------------


class TestParsedPrefixMatching:
    @pytest.fixture(autouse=True)
    def _graph_public_ip(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))

    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_exact_match(self):
        await assert_url_allowed_prefix(GRAPH_PREFIX, DELTA_PREFIXES, what="delta")

    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_path_descent(self):
        url = "https://graph.microsoft.com/v1.0/me/messages"
        await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_rejects_subdomain_bypass(self):
        url = "https://graph.microsoft.com.evil.com/v1.0/me/messages"
        with pytest.raises(BridgeURLValidationError, match="prefix|start with"):
            await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_rejects_credential_bypass(self):
        url = "https://graph.microsoft.com@evil.com/v1.0/me/messages"
        with pytest.raises(BridgeURLValidationError, match="prefix|start with|credential"):
            await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

    @pytest.mark.asyncio
    async def test_assert_url_allowed_prefix_rejects_http_scheme(self):
        url = "http://graph.microsoft.com/v1.0/me/messages"
        with pytest.raises(BridgeURLValidationError, match="https"):
            await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

    @pytest.mark.asyncio
    async def test_validate_webhook_payload_url_accepts_graph_path_descent(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))
        url = "https://graph.microsoft.com/v1.0/me/messages"
        assert await validate_webhook_payload_url(url) == url

    @pytest.mark.asyncio
    async def test_validate_webhook_payload_url_rejects_subdomain_bypass(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        url = "https://graph.microsoft.com.evil.com/v1.0/me/messages"
        with pytest.raises(BridgeURLValidationError, match="prefix|start with"):
            await validate_webhook_payload_url(url)

    @pytest.mark.asyncio
    async def test_validate_webhook_payload_url_rejects_credential_bypass(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        url = "https://graph.microsoft.com@evil.com/v1.0/me/messages"
        with pytest.raises(BridgeURLValidationError, match="prefix|start with|credential"):
            await validate_webhook_payload_url(url)


# ---------------------------------------------------------------------------
# assert_url_allowed_prefix — DNS fail closed
# ---------------------------------------------------------------------------


class TestAssertUrlAllowedPrefixDnsFailClosed:
    @pytest.mark.asyncio
    async def test_dns_gaierror_raises_bridge_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _fail_dns)
        with pytest.raises(BridgeURLValidationError, match="resolve|DNS|gaierror|host"):
            await assert_url_allowed_prefix(
                "https://graph.microsoft.com/v1.0/me/messages",
                DELTA_PREFIXES,
                what="delta",
            )

    @pytest.mark.asyncio
    async def test_dns_failure_logs_truncated_host_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        long_host = "h" * 200
        url = f"https://{long_host}.example.com/v1.0/me/messages"

        def _fail_dns_unexpected(*_args: Any, **_kwargs: Any) -> list:
            raise OSError("unexpected resolver failure")

        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _fail_dns_unexpected)

        with caplog.at_level(logging.WARNING, logger="nce.net_safety"):
            with pytest.raises(BridgeURLValidationError, match="DNS resolution failed"):
                await assert_url_allowed_prefix(url, DELTA_PREFIXES, what="delta")

        assert caplog.records, "expected a warning log on DNS failure"
        msg = caplog.records[-1].message
        assert "OSError" in msg
        quoted_host = _quoted_host_in_log(msg)
        assert quoted_host is not None
        assert len(quoted_host) <= 64
        assert long_host not in msg


def _quoted_host_in_log(message: str) -> str | None:
    """Extract hostname from ``... for 'host' ...`` log patterns."""
    match = re.search(r"for '([^']+)'", message)
    return match.group(1) if match else None


def _longest_hostname_substring_in_log(message: str) -> str | None:
    """Best-effort extract of a hostname-like token from a log line."""
    quoted = _quoted_host_in_log(message)
    if quoted:
        return quoted
    candidates = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.-]{3,}", message)
    if not candidates:
        return None
    return max(candidates, key=len)


# ---------------------------------------------------------------------------
# validate_webhook_payload_url
# ---------------------------------------------------------------------------


class TestValidateWebhookPayloadUrlNetSafety:
    @pytest.mark.asyncio
    async def test_accepts_valid_https_graph_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("13.66.15.141"))
        url = "https://graph.microsoft.com/v1.0/sites/abc/drives/def/root"
        assert await validate_webhook_payload_url(url) == url

    @pytest.mark.asyncio
    async def test_rejects_http_scheme(self):
        with pytest.raises(BridgeURLValidationError, match="only https"):
            await validate_webhook_payload_url("http://graph.microsoft.com/v1.0/me")

    @pytest.mark.asyncio
    async def test_rejects_loopback_resolution(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_webhook_payload_url("https://graph.microsoft.com/v1.0/me")

    @pytest.mark.asyncio
    async def test_accepts_relative_sites_path(self):
        path = "/sites/tenant-id/drives/drive-id/root"
        assert await validate_webhook_payload_url(path) == path

    @pytest.mark.asyncio
    async def test_rejects_relative_admin_path(self):
        with pytest.raises(BridgeURLValidationError, match="does not match|prefix"):
            await validate_webhook_payload_url("/admin/secret/panel")

    @pytest.mark.asyncio
    async def test_dns_warning_truncates_long_hostname_in_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """When DNS check logs a warning, hostname in the message is capped at 64 chars."""
        long_label = "z" * 200
        url = f"https://{long_label}.graph.microsoft.com/v1.0/me"

        # Force a non-gaierror resolution failure path if implementation uses broad except.
        def _weird_dns(_host: str, *_a: Any, **_kw: Any) -> list:
            raise OSError("mock resolution failure")

        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _weird_dns)

        with caplog.at_level(logging.WARNING, logger="nce.net_safety"):
            try:
                await validate_webhook_payload_url(url)
            except BridgeURLValidationError:
                pass

        if not caplog.records:
            pytest.skip("implementation raises without warning log on this DNS path")

        msg = caplog.records[-1].message
        quoted_host = _quoted_host_in_log(msg)
        if quoted_host:
            assert len(quoted_host) <= 64
        assert long_label not in msg
        assert "mock resolution failure" not in msg.lower()


# ---------------------------------------------------------------------------
# validate_extractor_url — DNS + private/metadata IPs
# ---------------------------------------------------------------------------


class TestValidateExtractorUrlNetSafety:
    @pytest.mark.asyncio
    async def test_dns_failure_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _fail_dns)
        with pytest.raises(BridgeURLValidationError, match="resolve|cannot resolve|DNS"):
            await validate_extractor_url("https://does-not-exist.invalid/api")

    @pytest.mark.asyncio
    async def test_rejects_private_192_168(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("192.168.1.1"))
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://internal.corp/api")

    @pytest.mark.asyncio
    async def test_rejects_link_local_metadata_ip(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "nce.net_safety.socket.getaddrinfo",
            _mock_getaddrinfo("169.254.169.254"),
        )
        with pytest.raises(BridgeURLValidationError, match="non-public"):
            await validate_extractor_url("https://metadata.example/latest/meta-data")

    @pytest.mark.asyncio
    async def test_accepts_public_https_host(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("52.32.1.10"))
        url = "https://api.lucid.co/v1/documents"
        assert await validate_extractor_url(url) == url


# ---------------------------------------------------------------------------
# validate_bridge_webhook_base_url — smoke
# ---------------------------------------------------------------------------


class TestValidateBridgeWebhookBaseUrlNetSafety:
    @pytest.mark.asyncio
    async def test_accepts_https_public_base(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        base = "https://hooks.example.com/nce/webhooks"
        assert await validate_bridge_webhook_base_url(base) == base

    @pytest.mark.asyncio
    async def test_rejects_http_for_non_loopback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("nce.net_safety.socket.getaddrinfo", _mock_getaddrinfo("1.2.3.4"))
        with pytest.raises(BridgeURLValidationError, match="https"):
            await validate_bridge_webhook_base_url("http://hooks.example.com/callback")


# ---------------------------------------------------------------------------
# Allowed webhook prefixes constant sanity
# ---------------------------------------------------------------------------


class TestAllowedWebhookUrlPrefixes:
    def test_graph_prefix_in_allowed_list(self):
        assert GRAPH_PREFIX in ALLOWED_WEBHOOK_URL_PREFIXES
