"""Tests for SafeAsyncClient SSRF blocking."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import pytest

from nce._http_utils import SafeAsyncClient, UnsafeURLError, _resolve_safe_ip


class TestSafeAsyncClient:
    @pytest.mark.asyncio
    async def test_blocks_private_ipv4(self, monkeypatch: pytest.MonkeyPatch):
        """Requests to RFC 1918 private IPs are rejected before connection."""

        def _mock_getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("10.0.0.1", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="non-public IP"):
                await client.get("https://internal.example.com/secret")

    @pytest.mark.asyncio
    async def test_blocks_loopback(self, monkeypatch: pytest.MonkeyPatch):
        """Requests to loopback addresses are rejected."""

        def _mock_getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="not allowed|non-public IP"):
                await client.post("https://localhost:8000/api")

    @pytest.mark.asyncio
    async def test_allows_public_ip(self, monkeypatch: pytest.MonkeyPatch):
        """Requests to public IPs are allowed (connection may still fail)."""

        def _mock_getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            # Connection will fail because 1.2.3.4 is not a real server,
            # but the SSRF guard should NOT raise.
            with pytest.raises(Exception) as excinfo:
                await client.get("https://public.example.com/")
            assert not isinstance(excinfo.value, UnsafeURLError)

    @pytest.mark.asyncio
    async def test_allows_unresolvable_host(self, monkeypatch: pytest.MonkeyPatch):
        """Unresolvable hosts pass the guard and fail at connection time."""
        import socket

        _real_getaddrinfo = socket.getaddrinfo

        def _mock_getaddrinfo(host, port, *args, **kwargs):
            if host == "this-host-definitely-does-not-exist.invalid":
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return _real_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(Exception) as excinfo:
                await client.get("https://this-host-definitely-does-not-exist.invalid/")
            assert not isinstance(excinfo.value, UnsafeURLError)

    @pytest.mark.asyncio
    async def test_blocks_ipv6_loopback(self, monkeypatch: pytest.MonkeyPatch):
        """Requests to ::1 are rejected."""

        def _mock_getaddrinfo(host, port, *args, **kwargs):
            return [(10, 1, 6, "", ("::1", 0, 0, 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="not allowed|non-public IP"):
                await client.get("https://localhost:8000/")


class TestSsrfHardeningBatch6:
    """Batch 6 SSRF hardening — schemes, normalization, DNS, redirects, proxy."""

    @pytest.fixture(autouse=True)
    def _clear_resolve_cache(self) -> None:
        _resolve_safe_ip.cache_clear()
        yield
        _resolve_safe_ip.cache_clear()

    # --- Scheme abuse ---

    @pytest.mark.asyncio
    async def test_blocks_file_scheme(self) -> None:
        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="not allowed"):
                await client.get("file:///etc/passwd")

    @pytest.mark.asyncio
    async def test_blocks_gopher_scheme(self) -> None:
        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError):
                await client.get("gopher://evil.com/")

    @pytest.mark.asyncio
    async def test_blocks_ftp_scheme(self) -> None:
        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError):
                await client.get("ftp://files.example.com/")

    @pytest.mark.asyncio
    async def test_allows_https_with_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [(2, 1, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(Exception) as excinfo:
                await client.get("https://example.com/")
            assert not isinstance(excinfo.value, UnsafeURLError)

    # --- Hostname normalization ---

    @pytest.mark.asyncio
    async def test_blocks_uppercase_localhost(self) -> None:
        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="not allowed"):
                await client.get("https://LOCALHOST/")

    @pytest.mark.asyncio
    async def test_blocks_localhost_trailing_dot(self) -> None:
        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="not allowed"):
                await client.get("https://localhost./")

    @pytest.mark.asyncio
    async def test_blocks_localhost_localdomain(self) -> None:
        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="not allowed"):
                await client.get("https://localhost.localdomain/")

    # --- IPv4-mapped IPv6 ---

    @pytest.mark.asyncio
    async def test_blocks_ipv4_mapped_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [(10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="non-public IP"):
                await client.get("https://mapped-loopback.example.com/")

    @pytest.mark.asyncio
    async def test_blocks_ipv4_mapped_private(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [(10, 1, 6, "", ("::ffff:10.0.0.1", 0, 0, 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="non-public IP"):
                await client.get("https://mapped-private.example.com/")

    @pytest.mark.asyncio
    async def test_allows_ipv4_mapped_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [(10, 1, 6, "", ("::ffff:1.2.3.4", 0, 0, 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(Exception) as excinfo:
                await client.get("https://mapped-public.example.com/")
            assert not isinstance(excinfo.value, UnsafeURLError)

    # --- DNS timeout ---

    @pytest.mark.asyncio
    async def test_dns_resolution_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _slow_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            time.sleep(3)
            return [(2, 1, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _slow_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="timed out"):
                await client.get("https://slow-dns-batch6.example.com/")

    # --- DNS rebinding simulation ---

    @pytest.mark.asyncio
    async def test_dns_rebinding_cache_prevents_second_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0
        hostname = "rebind-cache-batch6.example.com"

        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            nonlocal call_count
            call_count += 1
            return [(2, 1, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async def _mock_super_request(
            self: httpx.AsyncClient,
            method: str,
            url: Any,
            **kwargs: Any,
        ) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request(method, str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_super_request)

        async with SafeAsyncClient() as client:
            await client.get(f"https://{hostname}/first")
            await client.get(f"https://{hostname}/second")

        assert call_count == 1

    # --- Redirect not followed ---

    @pytest.mark.asyncio
    async def test_redirect_not_followed_to_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [(2, 1, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        captured_kwargs: dict[str, Any] = {}
        captured_urls: list[str] = []

        async def _mock_super_request(
            self: httpx.AsyncClient,
            method: str,
            url: Any,
            **kwargs: Any,
        ) -> httpx.Response:
            captured_kwargs.update(kwargs)
            captured_urls.append(str(url))
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/"},
                request=httpx.Request(method, str(url)),
            )

        monkeypatch.setattr(httpx.AsyncClient, "request", _mock_super_request)

        async with SafeAsyncClient() as client:
            response = await client.request("GET", "https://redirect-batch6.example.com/")

        assert response.status_code == 302
        assert captured_kwargs.get("follow_redirects") is False
        assert len(captured_urls) == 1
        assert "127.0.0.1" not in captured_urls[0]
        # IP pinning is disabled under pytest (see SafeAsyncClient._skip_ip_pinning).
        assert "redirect-batch6.example.com" in captured_urls[0]

    # --- Proxy bypass ---

    @pytest.mark.asyncio
    async def test_trust_env_false_ignores_proxy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://evil-proxy.com")

        async with SafeAsyncClient() as client:
            assert client._trust_env is False

    # --- Logging ---

    @pytest.mark.asyncio
    async def test_blocking_private_ip_logs_hostname(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        hostname = "private-log-batch6.example.com"

        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [(2, 1, 6, "", ("10.0.0.1", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        caplog.set_level(logging.WARNING, logger="nce._http_utils")

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError):
                await client.get(f"https://{hostname}/")

        assert any(
            rec.levelname == "WARNING"
            and "SSRF guard blocked outbound request" in rec.message
            and getattr(rec, "hostname", "") == hostname
            for rec in caplog.records
        )

    # --- Multiple IPs — any unsafe blocks all ---

    @pytest.mark.asyncio
    async def test_multiple_ips_any_unsafe_blocks_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _mock_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any):
            return [
                (2, 1, 6, "", ("1.2.3.4", 0)),
                (2, 1, 6, "", ("10.0.0.1", 0)),
            ]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        async with SafeAsyncClient() as client:
            with pytest.raises(UnsafeURLError, match="non-public IP"):
                await client.get("https://multi-ip-batch6.example.com/")
