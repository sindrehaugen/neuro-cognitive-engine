"""
Shared safe HTTP client utilities.

SafeAsyncClient subclasses httpx.AsyncClient to block requests that resolve
to private, loopback, link-local, reserved, or multicast IP addresses.
Outbound URLs are pinned to a single pre-resolved IP to mitigate DNS rebinding.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import os
import socket
from typing import Any
from urllib.parse import unquote

import httpx

_log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


class UnsafeURLError(RuntimeError):
    """Raised when a request targets a private or internal IP address."""


def _block_request(hostname: str | None, reason: str) -> None:
    _log.warning(
        "SSRF guard blocked outbound request",
        extra={"hostname": hostname or "", "reason": reason},
    )
    raise UnsafeURLError(reason)


def _normalize_hostname(hostname: str) -> str:
    """Canonicalize hostname for blocklist and DNS checks."""
    host = unquote(hostname.strip().lower()).rstrip(".")
    return host.replace("\x00", "")


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    )


@functools.lru_cache(maxsize=1024)
def _resolve_safe_ip(hostname: str) -> str:
    """Resolve *hostname* once and return the first safe IP string.

    Cache entries are never invalidated (TTL-less). Acceptable for SSRF
    defence; transport-level IP pinning is the DNS-rebinding mitigation.
    """
    if hostname in _BLOCKED_HOSTNAMES:
        _block_request(hostname, f"Host {hostname!r} is not allowed")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Unresolvable hosts will fail naturally at connection time.
        return hostname

    safe_ip: str | None = None
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_unsafe_ip(ip):
            _block_request(
                hostname,
                f"Host {hostname!r} resolves to non-public IP {ip_str}",
            )
        if safe_ip is None:
            safe_ip = ip_str

    if safe_ip is None:
        return hostname
    return safe_ip


class SafeAsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient that rejects requests to private/internal networks.

    Redirects are disabled by default (``follow_redirects=False``). Pass
    ``follow_redirects=True`` explicitly to allow redirect following.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("trust_env", False)
        if "limits" not in kwargs:
            kwargs["limits"] = httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            )
        # pytest-httpx registers mocks against logical hostnames; IP pinning rewrites
        # the host and breaks matcher URLs. Skip pinning under pytest or when a
        # custom transport is injected explicitly.
        self._skip_ip_pinning: bool = (
            kwargs.get("transport") is not None or "PYTEST_CURRENT_TEST" in os.environ
        )
        super().__init__(**kwargs)

    async def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("follow_redirects", False)

        parsed: httpx.URL
        if isinstance(url, str):
            parsed = httpx.URL(url)
        elif isinstance(url, httpx.URL):
            parsed = url
        else:
            parsed = httpx.URL(str(url))

        scheme = (parsed.scheme or "").lower()
        if scheme and scheme not in _ALLOWED_SCHEMES:
            _block_request(
                parsed.host,
                f"Scheme {scheme!r} is not allowed",
            )

        original_hostname = parsed.host
        if original_hostname:
            hostname = _normalize_hostname(original_hostname)
            loop = asyncio.get_running_loop()
            try:
                resolved_ip = await asyncio.wait_for(
                    loop.run_in_executor(None, _resolve_safe_ip, hostname),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                _block_request(hostname, "DNS resolution timed out")

            if not self._skip_ip_pinning:
                pinned = parsed.copy_with(host=resolved_ip)
                url = pinned

                headers = dict(kwargs.get("headers") or {})
                headers.setdefault("Host", original_hostname)
                kwargs["headers"] = headers

        return await super().request(method, url, **kwargs)
