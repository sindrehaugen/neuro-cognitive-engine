import asyncio
import hashlib
import ipaddress
import logging
import os
import shutil
import socket
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("nce.net_safety")

# Global registry for DNS-rebinding prevention pinning
_PINNED_HOSTS: dict[str, str] = {}


def _apply_transport_patch() -> None:
    """
    Hook httpcore AsyncNetworkBackend / SyncBackend connect_tcp to reuse resolved IPs,
    effectively mitigating DNS rebinding (SSRF TOCTOU) by resolving hostnames once.
    """

    classes_to_patch: list[Any] = []
    try:
        from httpcore._backends.auto import AutoBackend

        classes_to_patch.append(AutoBackend)
    except ImportError:
        pass

    try:
        from httpcore._backends.anyio import AnyIOBackend

        classes_to_patch.append(AnyIOBackend)
    except ImportError:
        pass

    try:
        from httpcore._backends.trio import TrioBackend

        classes_to_patch.append(TrioBackend)
    except ImportError:
        pass

    try:
        from httpcore._backends.sync import SyncBackend

        classes_to_patch.append(SyncBackend)
    except ImportError:
        pass

    def make_patched_connect_tcp(original_method: Any) -> Any:
        async def patched_connect_tcp(
            self: Any, host: str, port: int, *args: Any, **kwargs: Any
        ) -> Any:
            pinned_ip = _PINNED_HOSTS.get(host.lower().strip())
            if pinned_ip:
                return await original_method(self, pinned_ip, port, *args, **kwargs)
            return await original_method(self, host, port, *args, **kwargs)

        return patched_connect_tcp

    def make_patched_connect_tcp_sync(original_method: Any) -> Any:
        def patched_connect_tcp_sync(
            self: Any, host: str, port: int, *args: Any, **kwargs: Any
        ) -> Any:
            pinned_ip = _PINNED_HOSTS.get(host.lower().strip())
            if pinned_ip:
                return original_method(self, pinned_ip, port, *args, **kwargs)
            return original_method(self, host, port, *args, **kwargs)

        return patched_connect_tcp_sync

    for cls in classes_to_patch:
        if hasattr(cls, "connect_tcp"):
            orig = cls.connect_tcp
            if not getattr(orig, "_is_patched", False):
                import inspect

                if inspect.iscoroutinefunction(orig):
                    patched = make_patched_connect_tcp(orig)
                else:
                    patched = make_patched_connect_tcp_sync(orig)
                patched._is_patched = True  # type: ignore[attr-defined]
                cls.connect_tcp = patched


_apply_transport_patch()


_MAX_URL_LEN: int = 4_096

# Explicit IPv6 CIDR denylist for SSRF (defense in depth next to ipaddress is_* flags).
_SSRF_DENIED_IPV6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(c)  # type: ignore[misc]
    for c in (
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "2001:db8::/32",
        "100::/64",
    )
)


class BridgeURLValidationError(ValueError):
    """Raised when a bridge-related URL fails safety checks."""


def _parse_ip_from_getaddrinfo(
    sockaddr_ip: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """
    Parse the host portion of a ``getaddrinfo`` sockaddr tuple.

    Normalizes IPv6 literals: optional ``[...]`` wrapping and zone identifiers
    (``fe80::1%eth0``) which ``ipaddress.ip_address`` does not accept verbatim.
    """
    s = sockaddr_ip.strip()
    if len(s) >= 2 and s[0] == "[" and s[-1] == "]":
        s = s[1:-1]
    if "%" in s:
        s = s.split("%", 1)[0]
    return ipaddress.ip_address(s)


async def _resolve_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            None, socket.getaddrinfo, hostname, None, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise BridgeURLValidationError(f"cannot resolve host {hostname!r}: {e}") from e
    for item in infos:
        sockaddr = item[4]
        ip_str = sockaddr[0]
        try:
            out.append(_parse_ip_from_getaddrinfo(ip_str))  # type: ignore[arg-type]
        except ValueError:
            continue
    if not out:
        raise BridgeURLValidationError(f"no usable IP addresses for host {hostname!r}")
    return out


def _all_loopback(ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> bool:
    return bool(ips) and all(ip.is_loopback for ip in ips)


def _ipv6_in_explicit_denylist(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if ip.version != 6:
        return False
    return any(ip in net for net in _SSRF_DENIED_IPV6_NETWORKS)


def _any_non_public(ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> bool:
    for ip in ips:
        if _ipv6_in_explicit_denylist(ip):
            return True
        if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_loopback:
            return True
    return False


def _url_matches_prefix(url: str, prefix: str) -> bool:
    """Compare scheme, netloc, and path separately — prevents subdomain/credential spoofing."""
    u = urlparse(url)
    p = urlparse(prefix)
    u_host = (u.hostname or "").lower().rstrip(".")
    p_host = (p.hostname or "").lower().rstrip(".")
    return (
        u.scheme == p.scheme
        and u_host == p_host
        and (u.port or 443) == (p.port or 443)
        and u.path.startswith(p.path)
    )


async def validate_bridge_webhook_base_url(raw: str) -> str:
    """
    Validate ``BRIDGE_WEBHOOK_BASE_URL`` for Microsoft/Google webhook registration.

    - Requires a valid http(s) URL with a host.
    - Rejects hosts whose *non-loopback* resolution includes private/link-local space.
    - Allows ``http`` only when the host resolves exclusively to loopback (local dev).
    - For internet-facing hosts, requires ``https``.
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        raise BridgeURLValidationError("BRIDGE_WEBHOOK_BASE_URL is empty")
    if len(base) > _MAX_URL_LEN:
        raise BridgeURLValidationError(
            f"BRIDGE_WEBHOOK_BASE_URL exceeds maximum length ({_MAX_URL_LEN} chars)"
        )
    parsed = urlparse(base)
    if parsed.username or parsed.password:
        raise BridgeURLValidationError("BRIDGE_WEBHOOK_BASE_URL must not contain credentials")
    if parsed.scheme not in ("http", "https"):
        raise BridgeURLValidationError(
            f"BRIDGE_WEBHOOK_BASE_URL must use http or https (got scheme={parsed.scheme!r})"
        )
    host = parsed.hostname
    if not host:
        raise BridgeURLValidationError("BRIDGE_WEBHOOK_BASE_URL is missing a hostname")

    ips = await _resolve_ips(host)
    if _all_loopback(ips):
        if parsed.scheme != "http" and parsed.scheme != "https":
            pass  # unreachable
        return base

    if _any_non_public(ips):
        raise BridgeURLValidationError(
            f"BRIDGE_WEBHOOK_BASE_URL host {host!r} resolves to a non-public address "
            "(private/link-local/reserved/multicast are not allowed)"
        )

    if parsed.scheme != "https":
        raise BridgeURLValidationError(
            "BRIDGE_WEBHOOK_BASE_URL must use https for non-loopback hosts"
        )

    _PINNED_HOSTS[host.lower().strip()] = str(ips[0])
    return base


async def assert_url_allowed_prefix(
    url: str, allowed_prefixes: tuple[str, ...], *, what: str
) -> None:
    """
    Ensure ``url`` is under one of ``allowed_prefixes`` (parsed scheme/host/port/path match).
    Used for delta / pagination links stored in Redis.
    """
    u = (url or "").strip()
    if not u:
        raise BridgeURLValidationError(f"{what}: empty URL")
    if len(u) > _MAX_URL_LEN:
        raise BridgeURLValidationError(f"{what}: URL exceeds maximum length ({_MAX_URL_LEN} chars)")
    parsed = urlparse(u)
    if parsed.username or parsed.password:
        raise BridgeURLValidationError(f"{what}: credentials in URL are not allowed")
    if parsed.scheme not in ("https",):
        raise BridgeURLValidationError(
            f"{what}: only https URLs are allowed (got {parsed.scheme!r})"
        )
    host = (parsed.hostname or "").lower()
    if host:
        try:
            ips = await _resolve_ips(host)
            if _any_non_public(ips) and not _all_loopback(ips):
                raise BridgeURLValidationError(
                    f"{what}: host {host!r} resolves to a non-public address"
                )
            _PINNED_HOSTS[host.lower().strip()] = str(ips[0])
        except BridgeURLValidationError:
            raise
        except Exception as e:
            log.warning(
                "%s: DNS resolution failed for %r: %s",
                what,
                host[:64],
                type(e).__name__,
            )
            raise BridgeURLValidationError(
                f"{what}: could not verify host {host!r} — DNS resolution failed"
            ) from e

    if not any(_url_matches_prefix(u, p) for p in allowed_prefixes):
        raise BridgeURLValidationError(
            f"{what}: URL not within allowed prefixes (got {u[:120]!r}...)"
        )


# ---------------------------------------------------------------------------
# Extractor URL validation — SSRF guard for ingestion extractor outbound calls
# ---------------------------------------------------------------------------


async def validate_extractor_url(url: str, *, what: str = "extractor") -> str:
    """
    Validate a URL before an extractor makes an outbound HTTP request.

    Extractor engines (Miro, Lucidchart, etc.) accept a ``base_url`` parameter
    that an attacker could point at internal network resources.  This guard
    rejects URLs that resolve to private, loopback, link-local, reserved,
    multicast, or AWS/cloud metadata IPs before any bytes leave the machine.

    .. warning::
        This validation is subject to a Time-of-Check to Time-of-Use (TOCTOU)
        DNS rebinding risk since HTTP clients (e.g., ``httpx``) perform their
        own DNS resolution subsequently. Pinning the resolved IP or utilizing
        a custom connection resolver is recommended in high-risk environments.

    Returns *url* unchanged on success.  Raises ``BridgeURLValidationError``
    on failure.
    """
    raw = (url or "").strip()
    if not raw:
        raise BridgeURLValidationError(f"{what}: empty URL")
    if len(raw) > _MAX_URL_LEN:
        raise BridgeURLValidationError(f"{what}: URL exceeds maximum length ({_MAX_URL_LEN} chars)")
    parsed = urlparse(raw)
    if parsed.username or parsed.password:
        raise BridgeURLValidationError(f"{what}: credentials in URL are not allowed")
    if parsed.scheme not in ("https",):
        raise BridgeURLValidationError(
            f"{what}: only https URLs are allowed (got scheme={parsed.scheme!r})"
        )
    host = parsed.hostname
    if not host:
        raise BridgeURLValidationError(f"{what}: URL missing hostname: {raw!r}")

    try:
        ips = await _resolve_ips(host)
    except BridgeURLValidationError:
        raise
    except Exception as e:
        log.warning(
            "%s: DNS resolution failed for %r: %s",
            what,
            host[:64],
            type(e).__name__,
        )
        raise BridgeURLValidationError(f"{what}: could not resolve host {host!r}: {e}") from e

    if _any_non_public(ips):
        raise BridgeURLValidationError(
            f"{what}: host {host!r} resolves to a non-public address "
            f"(private/link-local/reserved/multicast/loopback)"
        )

    _PINNED_HOSTS[host.lower().strip()] = str(ips[0])
    return raw


# ---------------------------------------------------------------------------
# Webhook payload URL validation — SSRF guard for incoming webhook payloads
# ---------------------------------------------------------------------------

# Known-safe Microsoft Graph resource path prefixes
GRAPH_RESOURCE_PREFIXES = (
    "/sites/",
    "/users/",
    "/groups/",
    "/drives/",
    "/me/",
)

ALLOWED_WEBHOOK_URL_PREFIXES = (
    "https://graph.microsoft.com/",
    "https://www.googleapis.com/",
    "https://content.dropboxapi.com/",
    "https://api.dropbox.com/",
)


async def validate_webhook_payload_url(
    url: str,
    *,
    field_name: str = "resource",
) -> str:
    """
    Validate a URL extracted from a webhook notification payload against SSRF
    attacks.  Accepts either a fully-qualified URL or a resource path.

    *Fully-qualified URLs* must use HTTPS and must not resolve to private,
    loopback, link-local, reserved, or multicast IP addresses.

    *Resource paths* (relative, no scheme) must match a known-safe Microsoft
    Graph resource prefix (``/sites/``, ``/users/``, ``/groups/``, ``/drives/``,
    ``/me/``).

    .. warning::
        This validation is subject to a Time-of-Check to Time-of-Use (TOCTOU)
        DNS rebinding risk since HTTP clients (e.g., ``httpx``) perform their
        own DNS resolution subsequently. Pinning the resolved IP or utilizing
        a custom connection resolver is recommended in high-risk environments.

    Returns the validated URL on success.  Raises ``BridgeURLValidationError``
    with a descriptive message on failure.
    """
    raw = (url or "").strip()
    if not raw:
        raise BridgeURLValidationError(f"webhook {field_name}: empty URL")
    if len(raw) > _MAX_URL_LEN:
        raise BridgeURLValidationError(
            f"webhook {field_name}: URL exceeds maximum length ({_MAX_URL_LEN} chars)"
        )
    parsed = urlparse(raw)
    if parsed.username or parsed.password:
        raise BridgeURLValidationError(f"webhook {field_name}: credentials in URL are not allowed")

    # Relative resource path — validate against known-safe prefixes
    if not parsed.scheme and not parsed.hostname:
        ok_prefix = any(raw.startswith(p) for p in GRAPH_RESOURCE_PREFIXES)
        if not ok_prefix:
            raise BridgeURLValidationError(
                f"webhook {field_name}: resource path {raw!r} does not match "
                f"any known-safe prefix {GRAPH_RESOURCE_PREFIXES!r}"
            )
        return raw

    # Fully-qualified URL — enforce HTTPS and SSRF IP checks
    if parsed.scheme not in ("https",):
        raise BridgeURLValidationError(
            f"webhook {field_name}: only https URLs are allowed, got scheme={parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host:
        raise BridgeURLValidationError(f"webhook {field_name}: URL missing hostname: {raw!r}")
    try:
        ips = await _resolve_ips(host)
        # Webhook payload URLs come from external sources — always reject
        # loopback, private, link-local, reserved, and multicast IPs.
        if _any_non_public(ips):
            raise BridgeURLValidationError(
                f"webhook {field_name}: host {host!r} resolves to a non-public "
                f"address (private/link-local/reserved/multicast/loopback)"
            )
        _PINNED_HOSTS[host.lower().strip()] = str(ips[0])
    except BridgeURLValidationError:
        raise
    except Exception as e:
        log.warning(
            "webhook %s: DNS resolution check failed for %r: %s",
            field_name,
            host[:64],
            type(e).__name__,
        )

    # Also verify against allowed URL prefixes
    if not any(_url_matches_prefix(raw, p) for p in ALLOWED_WEBHOOK_URL_PREFIXES):
        raise BridgeURLValidationError(
            f"webhook {field_name}: URL not within allowed prefixes (got {raw[:120]!r}...)"
        )
    _PINNED_HOSTS[host.lower().strip()] = str(ips[0])
    return raw


def _verify_binary_safety(executable: str, expected_hash: str | None) -> str | None:
    """
    Verify that the executable is an absolute path (or resolves to one),
    exists as a file, and matches the expected SHA-256 hash if configured.
    Returns the absolute path on success, or None on failure/mismatch.
    """
    if not executable:
        log.warning("binary_safety: empty executable")
        return None

    # Reject relative paths that contain directory separators
    if ("/" in executable or "\\" in executable) and not os.path.isabs(executable):
        log.warning(
            "binary_safety: relative path containing separators is not allowed: %s", executable
        )
        return None

    if os.path.isfile(executable):
        resolved: str | None = executable
    else:
        resolved = shutil.which(executable)

    if not resolved:
        log.warning("binary_safety: executable not found: %s", executable)
        return None

    abs_path = os.path.abspath(resolved)
    if not os.path.isabs(abs_path):
        log.warning("binary_safety: path is not absolute: %s", abs_path)
        return None

    if not os.path.isfile(abs_path):
        log.warning("binary_safety: path is not a file: %s", abs_path)
        return None

    if expected_hash:
        expected_hash = expected_hash.strip().lower()
        h = hashlib.sha256()
        try:
            with open(abs_path, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
            file_hash = h.hexdigest().lower()
            if file_hash != expected_hash:
                log.warning(
                    "binary_safety: hash mismatch for %s: expected %s, got %s",
                    abs_path,
                    expected_hash,
                    file_hash,
                )
                return None
        except Exception as e:
            log.warning("binary_safety: failed to hash %s: %s", abs_path, e)
            return None
    return abs_path
