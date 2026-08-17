"""
Tests for DNS-rebinding SSRF mitigation and binary pinning checks.
"""

from __future__ import annotations

import hashlib
import os
import socket
import tempfile
from typing import Any

import httpx
import pytest
from httpcore._backends.anyio import AnyIOBackend

from nce.net_safety import _PINNED_HOSTS, _verify_binary_safety, validate_extractor_url


@pytest.mark.asyncio
async def test_dns_rebinding_mock_cannot_redirect_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Setup DNS resolver that changes on subsequent calls (simulating DNS rebinding)
    dns_calls = 0
    hostname = "rebinding-target-batch22.example.com"
    public_ip = "8.8.8.8"
    private_ip = "127.0.0.1"

    def mock_getaddrinfo(host: str, port: Any = None, *args: Any, **kwargs: Any) -> list:
        nonlocal dns_calls
        dns_calls += 1
        if dns_calls == 1:
            # First call during validate_extractor_url: returns public IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_ip, 0))]
        else:
            # Subsequent calls (rebound): returns private IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (private_ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

    # Validate the URL
    url = f"https://{hostname}/api"
    await validate_extractor_url(url)

    # Verify it is registered in pinned hosts
    assert _PINNED_HOSTS.get(hostname) == public_ip

    # 2. Intercept AnyIOBackend.connect_tcp to assert connection is made to the pinned IP (8.8.8.8)
    captured_hosts = []

    async def spy_connect_tcp(self: Any, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        captured_hosts.append(host)
        raise RuntimeError(f"Connection intercepted to {host}")

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", spy_connect_tcp)

    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError) as excinfo:
            await client.get(url)
        assert f"Connection intercepted to {public_ip}" in str(excinfo.value)

    # Verify connection was redirected to pinned IP, ignoring the rebound private IP
    assert public_ip in captured_hosts
    assert private_ip not in captured_hosts


def test_verify_binary_safety() -> None:
    # Test relative path rejection
    assert _verify_binary_safety("relative/path/to/bin", None) is None
    assert _verify_binary_safety("./bin", None) is None
    assert _verify_binary_safety("sub\\bin", None) is None

    # Test non-existent file
    assert _verify_binary_safety("/non/existent/path/to/bin", None) is None

    # Test correct hash matching
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(b"fake binary contents")
        temp_path = tf.name

    try:
        # Hash matching
        expected_hash = hashlib.sha256(b"fake binary contents").hexdigest()
        verified = _verify_binary_safety(temp_path, expected_hash)
        assert verified is not None
        assert os.path.abspath(verified) == os.path.abspath(temp_path)

        # Hash mismatch
        wrong_hash = hashlib.sha256(b"wrong contents").hexdigest()
        assert _verify_binary_safety(temp_path, wrong_hash) is None
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
