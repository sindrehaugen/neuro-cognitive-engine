"""Core security guards for external NCE connectors: SSRF, C8 redaction, XML/ZIP bombs."""

from __future__ import annotations

import io
import os
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterable
from typing import Any, Literal

import defusedxml.ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from nce.config import secret_env
from nce.net_safety import (
    BLOCKED_INTERNAL_HOSTNAMES,
    BridgeURLValidationError,
    is_ip_blocked,
    unwrap_ip,
)
from nce.net_safety import validate_outbound_url as _core_validate_outbound_url
from nce.net_safety import validate_outbound_url_async as _core_validate_outbound_url_async

DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
    ".vbs",
    ".js",
    ".msi",
    ".scr",
    ".com",
    ".pif",
    ".jar",
    ".hta",
    ".lnk",
)

_DEFAULT_C8_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "bearer_token",
        "api_key",
        "apikey",
        "client_secret",
        "private_key",
        "master_key",
        "signing_key",
        "encryption_key",
        "secret_key",
        "wrapped_dek",
        "encrypted_config",
        "authorization",
        "cookie",
        "set_cookie",
        "connection_string",
        "dsn",
        "pg_dsn",
        "mongo_uri",
        "redis_url",
        "credentials",
        "bearer",
        # C8 commercial / internal cost allow-list exclusions
        "unit_cost",
        "cost_price",
        "buy_price",
        "internal_cost",
        "purchase_price",
        "margin",
        "profit_margin",
        "markup",
        "kickback_pct",
        "discount_floor",
        "salary",
        "compensation",
        "internal_notes",
        "cost",
    }
)

_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "client_secret",
    "private_key",
    "master_key",
    "signing_key",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer_token",
    "wrapped_dek",
    "secret_config",
    "encrypted_config",
)

_HTML_SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SSRFBlockedError(BridgeURLValidationError):
    """Raised when an outbound URL violates Core SSRF safety requirements."""


def validate_outbound_url(
    url: str,
    *,
    allow_http: bool = True,
    require_https: bool | None = None,
    resolve_dns: bool = True,
) -> str:
    """Validate an outbound URL against Core SSRF rules synchronously."""
    req_https = (not allow_http) if require_https is None else bool(require_https)
    try:
        return _core_validate_outbound_url(
            url,
            require_https=req_https,
            resolve_dns=resolve_dns,
        )
    except SSRFBlockedError:
        raise
    except BridgeURLValidationError as exc:
        raise SSRFBlockedError(str(exc)) from exc


async def validate_outbound_url_async(
    url: str,
    *,
    allow_http: bool = True,
    require_https: bool | None = None,
    resolve_dns: bool = True,
) -> str:
    """Validate an outbound URL against Core SSRF rules asynchronously."""
    req_https = (not allow_http) if require_https is None else bool(require_https)
    try:
        return await _core_validate_outbound_url_async(
            url,
            require_https=req_https,
            resolve_dns=resolve_dns,
        )
    except SSRFBlockedError:
        raise
    except BridgeURLValidationError as exc:
        raise SSRFBlockedError(str(exc)) from exc


def read_nce_master_key_str() -> str:
    """Read NCE_MASTER_KEY safely via Core secret_env (supports *_FILE mounts)."""
    return secret_env("NCE_MASTER_KEY", "").strip()


def safe_parse_xml_bytes(raw: bytes | str, *, max_bytes: int = 50 * 1024 * 1024) -> Any:
    """Parse XML safely with defusedxml, blocking XXE, DTD, and entity expansion bombs."""
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("raw XML payload must be bytes or str")
    if len(payload) > max_bytes:
        raise ValueError(f"XML payload exceeds limit ({len(payload)} > {max_bytes} bytes)")
    try:
        return SafeET.fromstring(bytes(payload))
    except DefusedXmlException as exc:
        raise ValueError(f"Unsafe XML content rejected (XXE/DTD blocked): {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Invalid XML payload: {exc}") from exc


def _validate_zip_member_name(name: str, forbidden_suffixes: tuple[str, ...]) -> None:
    """Reject zip-slip paths, drive prefixes, backslashes, and dangerous extensions."""
    if not name or "\\" in name or "\x00" in name:
        raise ValueError(f"Path traversal or illegal archive member name rejected: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"Path traversal absolute path rejected in archive: {name!r}")
    parts = [p for p in name.split("/") if p]
    if any(part == ".." for part in parts):
        raise ValueError(f"Path traversal '..' segment rejected in archive: {name!r}")
    normalized = posixpath.normpath(name)
    if normalized.startswith("..") or normalized.startswith("/"):
        raise ValueError(f"Path traversal normalized escape rejected: {name!r}")
    low = name.lower()
    if any(low.endswith(sfx.lower()) for sfx in forbidden_suffixes):
        raise ValueError(f"Forbidden executable/script archive entry rejected: {name!r}")


def safe_extract_zip_bytes(
    raw: bytes,
    *,
    max_files: int = 2000,
    max_members: int | None = None,
    max_total_bytes: int | None = None,
    max_uncompressed_bytes: int | None = None,
    max_ratio: float = 100.0,
    forbidden_suffixes: tuple[str, ...] = DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES,
) -> dict[str, bytes]:
    """Safely extract ZIP bytes in memory with zip-slip, symlink, and zip-bomb bounds."""
    member_limit = max_members if max_members is not None else max_files
    byte_limit = (
        max_uncompressed_bytes
        if max_uncompressed_bytes is not None
        else (max_total_bytes if max_total_bytes is not None else 100 * 1024 * 1024)
    )
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("ZIP archive payload must be bytes")

    extracted: dict[str, bytes] = {}
    total_uncompressed = 0

    with zipfile.ZipFile(io.BytesIO(bytes(raw)), mode="r") as zf:
        infos = zf.infolist()
        if len(infos) > member_limit:
            raise ValueError(
                f"Archive exceeds maximum member count ({len(infos)} > {member_limit})"
            )

        for info in infos:
            if info.is_dir():
                continue
            # Reject POSIX symlinks stored in external_attr.
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError(f"Archive symlink entry rejected: {info.filename!r}")
            _validate_zip_member_name(info.filename, forbidden_suffixes)

            if info.file_size > byte_limit:
                raise ValueError(
                    f"Archive entry {info.filename!r} exceeds max uncompressed size ({info.file_size} > {byte_limit})"
                )
            if info.compress_size > 0 and (info.file_size / info.compress_size) > max_ratio:
                raise ValueError(
                    f"Archive bomb ratio exceeded for {info.filename!r} "
                    f"({info.file_size}/{info.compress_size} > {max_ratio})"
                )

            with zf.open(info, mode="r") as member_fp:
                chunks: list[bytes] = []
                member_bytes = 0
                while True:
                    chunk = member_fp.read(65536)
                    if not chunk:
                        break
                    member_bytes += len(chunk)
                    total_uncompressed += len(chunk)
                    if total_uncompressed > byte_limit:
                        raise ValueError(
                            f"Archive total uncompressed bytes exceeded ({total_uncompressed} > {byte_limit})"
                        )
                    if info.compress_size > 0 and (member_bytes / info.compress_size) > max_ratio:
                        raise ValueError(
                            f"Archive bomb compression ratio exceeded while streaming {info.filename!r}"
                        )
                    chunks.append(chunk)
                extracted[info.filename] = b"".join(chunks)

    return extracted


def safe_inflate_raw_deflate(
    raw: bytes,
    *,
    max_bytes: int | None = None,
    max_output_bytes: int | None = None,
    wbits: int = -zlib.MAX_WBITS,
) -> bytes:
    """Bound-check raw deflate inflation against compression bombs."""
    limit = (
        max_bytes
        if max_bytes is not None
        else (max_output_bytes if max_output_bytes is not None else 50 * 1024 * 1024)
    )
    decomp = zlib.decompressobj(wbits)
    out = bytearray()
    view = memoryview(raw)
    step = 65536
    for offset in range(0, len(view), step):
        remaining = limit - len(out) + 1
        chunk = decomp.decompress(view[offset : offset + step], remaining)
        out.extend(chunk)
        if len(out) > limit:
            raise ValueError(f"Inflated deflate stream exceeds {limit} bytes")
    tail = decomp.flush(limit - len(out) + 1)
    out.extend(tail)
    if len(out) > limit:
        raise ValueError(f"Inflated deflate stream exceeds {limit} bytes")
    return bytes(out)


def sanitize_html_label(raw: str, *, max_len: int = 512) -> str:
    """Strip HTML tags, scripts/styles, and control characters from external diagram labels."""
    text = str(raw or "")
    text = _HTML_SCRIPT_STYLE_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _CTRL_CHARS_RE.sub("", text).strip()
    if len(text) > max_len:
        return text[:max_len]
    return text


class AllowListRedactor:
    """Recursive C8 allow-list and credential redactor for external connector payloads."""

    def __init__(
        self,
        *,
        forbidden_keys: Iterable[str] = (),
        mode: Literal["strip", "mask"] = "strip",
        mask_value: str = "[REDACTED]",
    ) -> None:
        self.forbidden_keys: frozenset[str] = _DEFAULT_C8_FORBIDDEN_KEYS | {
            str(k).strip().lower() for k in forbidden_keys if str(k).strip()
        }
        self.mode = mode
        self.mask_value = mask_value

    def is_forbidden_key(self, key: str, extra_forbidden: frozenset[str] = frozenset()) -> bool:
        """Return True if a dictionary key matches forbidden C8 or secret names."""
        low = str(key).strip().lower()
        if low in self.forbidden_keys or low in extra_forbidden:
            return True
        return any(sub in low for sub in _SENSITIVE_KEY_SUBSTRINGS)

    def redact(self, data: Any, extra_forbidden: Iterable[str] = ()) -> Any:
        """Return a deep copy with sensitive keys dropped (strip) or masked (mask)."""
        extra = frozenset(str(k).strip().lower() for k in extra_forbidden if str(k).strip())
        return self._walk(data, extra)

    def _walk(self, node: Any, extra: frozenset[str]) -> Any:
        if isinstance(node, dict):
            cleaned: dict[Any, Any] = {}
            for k, v in node.items():
                if self.is_forbidden_key(str(k), extra):
                    if self.mode == "mask":
                        cleaned[k] = self.mask_value
                    continue
                cleaned[k] = self._walk(v, extra)
            return cleaned
        if isinstance(node, list):
            return [self._walk(item, extra) for item in node]
        if isinstance(node, tuple):
            return tuple(self._walk(item, extra) for item in node)
        return node


_DEFAULT_REDACTOR = AllowListRedactor(mode="strip")


def redact_c8_allowlist(data: Any, extra_forbidden: Iterable[str] = ()) -> Any:
    """Strip C8 commercial/financial and credential keys recursively from connector payloads."""
    return _DEFAULT_REDACTOR.redact(data, extra_forbidden=extra_forbidden)


def resolve_connector_env_flag(name: str, default: bool = False) -> bool:
    """Parse boolean feature flags consistently across NCE connector runtimes."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


__all__ = [
    "AllowListRedactor",
    "BLOCKED_INTERNAL_HOSTNAMES",
    "BridgeURLValidationError",
    "DEFAULT_FORBIDDEN_ARCHIVE_SUFFIXES",
    "SSRFBlockedError",
    "is_ip_blocked",
    "read_nce_master_key_str",
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
