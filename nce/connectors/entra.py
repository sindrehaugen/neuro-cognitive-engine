"""Standalone Microsoft Entra ID (Azure AD) OIDC / JWKS verifier for NCE connectors.

Performs cryptographic RS256/ES256 verification against tenant JWKS metadata
with explicit algorithm pinning, SSRF-validated outbound discovery/JWKS URLs,
and production fail-closed checks — without depending on vertical SQL schema.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from nce.config import cfg
from nce.connectors.guards import SSRFBlockedError, validate_outbound_url_async

log = logging.getLogger("nce.connectors.entra")

ALLOWED_ENTRA_ALGORITHMS: tuple[str, ...] = ("RS256", "ES256")
_MICROSOFT_HOST_SUFFIXES: tuple[str, ...] = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "sts.windows.net",
)


class EntraVerificationError(PermissionError):
    """Raised when an Entra OIDC token or JWKS configuration fails validation."""


@dataclass(frozen=True)
class EntraConfig:
    """Configuration contract for Core Entra OIDC token verification."""

    tenant_id: str = field(
        default_factory=lambda: (
            os.getenv("NCE_ENTRA_TENANT_ID", "") or getattr(cfg, "NCE_ENTRA_TENANT_ID", "")
        ).strip()
    )
    audience: str = field(
        default_factory=lambda: (
            os.getenv("NCE_ENTRA_AUDIENCE", "") or os.getenv("NCE_ENTRA_CLIENT_ID", "")
        ).strip()
    )
    enforce_mode: str = field(
        default_factory=lambda: (
            (os.getenv("NCE_ENTRA_ENFORCE_MODE", "") or getattr(cfg, "NCE_ENTRA_ENFORCE_MODE", ""))
            .strip()
            .lower()
        )
    )
    issuer_override: str | None = field(
        default_factory=lambda: (os.getenv("NCE_ENTRA_ISSUER", "") or "").strip() or None
    )
    jwks_uri_override: str | None = field(
        default_factory=lambda: (os.getenv("NCE_ENTRA_JWKS_URI", "") or "").strip() or None
    )
    jwks_ttl_seconds: float = 3600.0
    leeway_seconds: int = 30

    @property
    def expected_issuer(self) -> str:
        """Return canonical Entra v2.0 issuer URL unless overridden."""
        if self.issuer_override:
            return self.issuer_override.rstrip("/")
        if not self.tenant_id:
            return ""
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def jwks_uri(self) -> str:
        """Return JWKS discovery URL for the configured tenant."""
        if self.jwks_uri_override:
            return self.jwks_uri_override
        if not self.tenant_id:
            return ""
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    def validate_startup_posture(self, *, is_prod: bool | None = None) -> None:
        """Fail closed in production if Entra is configured without strict mode or tenant/audience."""
        prod = bool(getattr(cfg, "IS_PROD", False)) if is_prod is None else bool(is_prod)
        if not self.tenant_id:
            if prod and self.enforce_mode == "strict":
                raise EntraVerificationError(
                    "Entra strict mode requires NCE_ENTRA_TENANT_ID in production"
                )
            return
        if prod and self.enforce_mode != "strict":
            raise EntraVerificationError(
                "CRITICAL SECURITY FAILURE: NCE_ENTRA_ENFORCE_MODE must be 'strict' "
                "when NCE_ENTRA_TENANT_ID is set in production."
            )


@dataclass(frozen=True)
class EntraPrincipal:
    """Verified Entra ID principal claims."""

    oid: str
    tid: str
    sub: str
    upn: str
    name: str
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    claims: dict[str, Any] = field(default_factory=dict)


class EntraOIDCVerifier:
    """Cryptographic JWT / JWKS verifier for Microsoft Entra ID tokens."""

    def __init__(self, config: EntraConfig | None = None) -> None:
        self.config = config or EntraConfig()
        self._jwks_cache: list[dict[str, Any]] | None = None
        self._jwks_fetched_at: float = 0.0

    def invalidate_jwks_cache(self) -> None:
        """Clear cached JWKS keys so the next verification fetches fresh keys."""
        self._jwks_cache = None
        self._jwks_fetched_at = 0.0

    async def get_jwks(
        self,
        *,
        force_refresh: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[dict[str, Any]]:
        """Return cached JWKS keys or fetch them over SSRF-validated HTTPS."""
        now = time.monotonic()
        if (
            not force_refresh
            and self._jwks_cache is not None
            and (now - self._jwks_fetched_at) < self.config.jwks_ttl_seconds
        ):
            return self._jwks_cache

        jwks_url = self.config.jwks_uri
        if not jwks_url:
            raise EntraVerificationError("Cannot fetch JWKS: NCE_ENTRA_TENANT_ID is not set")

        try:
            safe_url = await validate_outbound_url_async(
                jwks_url, allow_http=False, resolve_dns=True
            )
        except SSRFBlockedError as exc:
            raise EntraVerificationError(f"Blocked JWKS endpoint {jwks_url!r}: {exc}") from exc

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
            transport=transport,
        ) as client:
            resp = await client.get(safe_url)
            if resp.status_code != 200:
                raise EntraVerificationError(f"JWKS endpoint returned HTTP {resp.status_code}")
            data = resp.json()
        keys = data.get("keys")
        if not isinstance(keys, list) or not keys:
            raise EntraVerificationError("JWKS response contained no signing keys")
        self._jwks_cache = [k for k in keys if isinstance(k, dict)]
        self._jwks_fetched_at = now
        return self._jwks_cache

    async def verify_token(
        self,
        token: str,
        *,
        required_roles: tuple[str, ...] | list[str] | None = None,
        jwks: list[dict[str, Any]] | dict[str, Any] | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> EntraPrincipal:
        """Verify an Entra ID JWT against JWKS keys and return an EntraPrincipal."""
        raw = (token or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw or raw.count(".") != 2:
            raise EntraVerificationError("Missing or malformed Entra JWT")

        try:
            header = jwt.get_unverified_header(raw)
        except jwt.InvalidTokenError as exc:
            raise EntraVerificationError(f"Invalid JWT header: {exc}") from exc

        alg = str(header.get("alg") or "").strip()
        if alg not in ALLOWED_ENTRA_ALGORITHMS:
            raise EntraVerificationError(
                f"Disallowed JWT signing algorithm {alg!r}; allowed={ALLOWED_ENTRA_ALGORITHMS}"
            )

        kid = str(header.get("kid") or "").strip()
        key_list: list[dict[str, Any]]
        if jwks is not None:
            key_list = list(jwks["keys"]) if isinstance(jwks, dict) else list(jwks)
        else:
            key_list = await self.get_jwks(transport=transport)

        selected_jwk: dict[str, Any] | None = None
        for entry in key_list:
            if not kid or entry.get("kid") == kid:
                selected_jwk = entry
                break
        if selected_jwk is None and jwks is None:
            # Refresh once in case keys rotated.
            key_list = await self.get_jwks(force_refresh=True, transport=transport)
            for entry in key_list:
                if not kid or entry.get("kid") == kid:
                    selected_jwk = entry
                    break

        if selected_jwk is None:
            raise EntraVerificationError(f"No matching JWKS signing key found for kid={kid!r}")

        try:
            signing_key = jwt.PyJWK.from_dict(selected_jwk).key
        except Exception as exc:
            raise EntraVerificationError(f"Invalid JWKS key material: {exc}") from exc

        expected_aud = audience if audience is not None else (self.config.audience or None)
        expected_iss = issuer if issuer is not None else (self.config.expected_issuer or None)
        decode_options: dict[str, Any] = {
            "require": ["exp", "sub"],
            "verify_signature": True,
            "verify_exp": True,
            "verify_nbf": True,
            "verify_iat": False,
            "verify_aud": bool(expected_aud),
            "verify_iss": bool(expected_iss),
        }

        try:
            claims: dict[str, Any] = jwt.decode(
                raw,
                key=signing_key,
                algorithms=list(ALLOWED_ENTRA_ALGORITHMS),
                audience=expected_aud,
                issuer=expected_iss,
                leeway=self.config.leeway_seconds,
                options=decode_options,
            )
        except jwt.PyJWTError as exc:
            raise EntraVerificationError(f"JWT verification failed: {exc}") from exc

        token_tid = str(claims.get("tid") or "").strip()
        if self.config.tenant_id and token_tid and token_tid != self.config.tenant_id:
            raise EntraVerificationError(
                f"Tenant ID mismatch: expected {self.config.tenant_id!r}, got {token_tid!r}"
            )

        raw_roles = claims.get("roles") or ()
        roles = (
            tuple(str(r) for r in raw_roles) if isinstance(raw_roles, (list, tuple, set)) else ()
        )
        raw_scp = str(claims.get("scp") or "").strip()
        scopes = tuple(part for part in raw_scp.split() if part)

        if required_roles:
            missing = set(required_roles) - set(roles)
            if missing:
                raise EntraVerificationError(f"Missing required Entra role(s): {sorted(missing)}")

        oid = str(claims.get("oid") or claims.get("sub") or "")
        sub = str(claims.get("sub") or oid)
        upn = str(
            claims.get("preferred_username") or claims.get("upn") or claims.get("email") or sub
        )
        name = str(claims.get("name") or upn)

        return EntraPrincipal(
            oid=oid,
            tid=token_tid,
            sub=sub,
            upn=upn,
            name=name,
            roles=roles,
            scopes=scopes,
            claims=dict(claims),
        )


__all__ = [
    "ALLOWED_ENTRA_ALGORITHMS",
    "EntraConfig",
    "EntraOIDCVerifier",
    "EntraPrincipal",
    "EntraVerificationError",
]
