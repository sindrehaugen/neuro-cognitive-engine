"""
Abstract document-bridge provider (NCE Enterprise §10.3, Appendix H).

Concrete bridges implement delta enumeration and optional OAuth refresh.
RQ workers import these classes to download files after webhook enqueueing.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from typing import Any

from nce.mtls import assert_bridge_mtls_configured

log = logging.getLogger("nce.bridges.base")


class BridgeAuthError(RuntimeError):
    """Raised when bridge credentials are missing or refresh fails."""


class BridgeProvider(ABC):
    """Provider abstraction: OAuth surface + delta walk + file download.

    mTLS enforcement: construction raises ``MTLSNotConfiguredError`` when
    ``NCE_MTLS_STRICT=true`` (default) and cert paths are not configured.
    Set ``NCE_MTLS_STRICT=false`` to downgrade to a warning in dev environments.
    """

    def __init__(self) -> None:
        """Enforce mTLS configuration at bridge construction time.

        Subclasses that override __init__() MUST call super().__init__() to
        ensure the mTLS security check runs before any connection attempt.
        Failure to call super().__init__() will silently bypass the mTLS
        assertion — this is a critical security contract.
        """
        assert_bridge_mtls_configured(service=self.__class__.__name__)

    @property
    @abstractmethod
    def provider_key(self) -> str:
        """One of: sharepoint | gdrive | dropbox."""

    def bearer_token(self) -> str:
        """Return a valid access token (env, cache, or refreshed)."""
        override = getattr(self, "_oauth_token_override", None)
        if override:
            return override

        from nce.config import cfg

        if self.provider_key == "sharepoint":
            token = (cfg.GRAPH_BRIDGE_TOKEN or "").strip()
        elif self.provider_key == "gdrive":
            token = (cfg.GDRIVE_BRIDGE_TOKEN or "").strip()
        elif self.provider_key == "dropbox":
            token = (cfg.DROPBOX_BRIDGE_TOKEN or "").strip()
        else:
            token = ""

        if not token:
            try:
                return self.refresh_oauth_token()
            except BridgeAuthError:
                raise
        return token

    def refresh_oauth_token(self) -> str:
        """
        Exchange refresh token / MSAL silent / etc.
        Default: no-op; subclasses override when wired to MSAL / google-auth / Dropbox SDK.
        """
        raise BridgeAuthError(
            f"{self.provider_key}: refresh_oauth_token not implemented; set *_BRIDGE_TOKEN env vars"
        )

    @abstractmethod
    def walk_delta(
        self, context: dict[str, Any]
    ) -> Iterator[dict[str, Any]] | AsyncIterator[dict[str, Any]]:
        """
        Yield change records from the provider delta API using `context`
        (webhook-derived; shape is provider-specific).
        """

    def download_file(self, file_ref: dict[str, Any]) -> bytes:
        """
        Fetch raw file bytes for indexing. `file_ref` is provider-specific metadata.
        """
        raise NotImplementedError(
            f"{self.provider_key}.download_file must be implemented for ingestion"
        )


def redis_client():
    """Shared sync Redis handle for cursors and dedupe (worker + bridges)."""
    from redis import Redis

    from nce.config import cfg

    return Redis.from_url(cfg.REDIS_URL)
