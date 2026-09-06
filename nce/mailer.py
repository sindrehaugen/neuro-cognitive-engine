"""Small and boring mailer interface for NCE document delivery (Wave Q-3).

Enforces Sindre's non-negotiable recipient isolation constraint:
- Every email is dispatched individually to exactly one recipient.
- CC and BCC are strictly forbidden and rejected at the domain layer
  to prevent cross-boundary recipient address disclosure.
- Configured via ``secret_env()`` so Docker/K8s ``*_FILE`` secrets work transparently.
- Includes ``RecordingFakeMailer`` for offline tests and assertions.
"""

from __future__ import annotations

import email.message
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from nce.config import secret_env

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EmailDelivery Value Object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailDelivery:
    """An outbound email message.

    Strict Constraints:
    - ``to`` must be exactly ONE valid email address.
    - ``cc`` and ``bcc`` MUST be empty. Any attempt to provide CC or BCC
      raises ValueError immediately.
    """

    to: str
    subject: str
    body_text: str
    body_html: str | None = None
    attachments: Sequence[tuple[str, bytes, str]] = field(
        default_factory=tuple
    )  # (filename, data, mimetype)
    cc: Sequence[str] = field(default_factory=tuple)
    bcc: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.to or not isinstance(self.to, str):
            raise ValueError("Recipient 'to' must be a non-empty string email address")

        to_clean = self.to.strip()
        if not to_clean:
            raise ValueError("Recipient 'to' must not be blank")

        if (
            "," in to_clean
            or ";" in to_clean
            or " " in to_clean
            or "\n" in to_clean
            or "\r" in to_clean
        ):
            raise ValueError(
                f"Recipient 'to' must contain exactly one email address, got {self.to!r}. "
                "Separate emails are required; never bundle multiple recipients."
            )

        if "@" not in to_clean:
            raise ValueError(f"Recipient 'to' must be a valid email address, got {self.to!r}")

        user_part, _, domain_part = to_clean.partition("@")
        if not user_part or not domain_part or "." not in domain_part:
            raise ValueError(f"Recipient 'to' must be a valid email address, got {self.to!r}")

        if self.cc:
            raise ValueError(
                "CC is strictly forbidden; each email must be dispatched separately without CC"
            )

        if self.bcc:
            raise ValueError(
                "BCC is strictly forbidden; each email must be dispatched separately without BCC"
            )


# ---------------------------------------------------------------------------
# Mailer Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Mailer(Protocol):
    """Abstract mailer interface."""

    async def send(self, message: EmailDelivery) -> bool:
        """Send an email delivery message."""
        ...


# ---------------------------------------------------------------------------
# RecordingFakeMailer (Testing & Verification)
# ---------------------------------------------------------------------------


class RecordingFakeMailer:
    """In-memory recording fake mailer for tests and verification.

    Captures all sent EmailDelivery objects and allows inspecting exact
    recipients, subjects, bodies, and attachments.
    """

    def __init__(self) -> None:
        self.sent_messages: list[EmailDelivery] = []
        self.send_error: Exception | None = None

    async def send(self, message: EmailDelivery) -> bool:
        if self.send_error is not None:
            raise self.send_error
        self.sent_messages.append(message)
        log.info(
            "RecordingFakeMailer: sent message to %s (subject: %r, attachments: %d)",
            message.to,
            message.subject,
            len(message.attachments),
        )
        return True

    def clear(self) -> None:
        """Clear recorded messages and reset error state."""
        self.sent_messages.clear()
        self.send_error = None


# ---------------------------------------------------------------------------
# SMTPMailer (Production SMTP Delivery via aiosmtplib)
# ---------------------------------------------------------------------------


class SMTPMailer:
    """SMTP mailer backed by aiosmtplib.

    Configuration is resolved via ``secret_env()`` to honor standard
    environment variables and Docker/K8s ``*_FILE`` secrets:
    - ``NCE_SMTP_HOST``: Hostname of the SMTP server.
    - ``NCE_SMTP_PORT``: Port (default: 587).
    - ``NCE_SMTP_USER``: Authentication username (optional).
    - ``NCE_SMTP_PASS``: Authentication password (optional, supports ``*_FILE``).
    - ``NCE_SMTP_FROM``: Envelope 'From' address (default: ``"noreply@nce.internal"``).
    - ``NCE_SMTP_STARTTLS``: Whether to enable STARTTLS (default: ``"true"``).
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_address: str | None = None,
        start_tls: bool | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from_address = from_address
        self._start_tls = start_tls

    def _get_config(self) -> dict[str, Any]:
        host = (self._host or secret_env("NCE_SMTP_HOST", "") or "").strip()
        port_raw = self._port if self._port is not None else secret_env("NCE_SMTP_PORT", "587")
        try:
            port = int(str(port_raw).strip() or 587)
        except (ValueError, TypeError):
            port = 587

        user = (
            self._user
            if self._user is not None
            else (secret_env("NCE_SMTP_USER", "") or "").strip() or None
        )
        password = (
            self._password
            if self._password is not None
            else (secret_env("NCE_SMTP_PASS", "") or "").strip() or None
        )
        from_addr = (
            self._from_address
            if self._from_address is not None
            else (secret_env("NCE_SMTP_FROM", "") or "noreply@nce.internal").strip()
        )
        start_tls = (
            self._start_tls
            if self._start_tls is not None
            else secret_env("NCE_SMTP_STARTTLS", "true").strip().lower() in ("1", "true", "yes")
        )

        return {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "from_address": from_addr,
            "start_tls": start_tls,
        }

    async def send(self, message: EmailDelivery) -> bool:
        """Send an email delivery over SMTP via aiosmtplib."""
        cfg = self._get_config()
        host = cfg["host"]
        if not host:
            raise RuntimeError("SMTP host is not configured (NCE_SMTP_HOST is empty)")

        try:
            import aiosmtplib
        except ImportError as exc:
            raise RuntimeError(
                "aiosmtplib is required for SMTP delivery; install dependencies or configure a fake mailer"
            ) from exc

        msg = email.message.EmailMessage()
        msg["From"] = cfg["from_address"]
        msg["To"] = message.to.strip()
        msg["Subject"] = message.subject
        msg.set_content(message.body_text)

        if message.body_html:
            msg.add_alternative(message.body_html, subtype="html")

        for filename, data, content_type in message.attachments:
            maintype, _, subtype = content_type.partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(
                data,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

        log.info(
            "SMTPMailer: connecting to %s:%d to deliver email to %s (subject: %r)",
            host,
            cfg["port"],
            message.to,
            message.subject,
        )

        await aiosmtplib.send(
            msg,
            hostname=host,
            port=cfg["port"],
            username=cfg["user"],
            password=cfg["password"],
            start_tls=cfg["start_tls"],
            timeout=10.0,
        )
        return True


# ---------------------------------------------------------------------------
# Global Mailer Factory and Registry
# ---------------------------------------------------------------------------

_global_mailer: Mailer | None = None


def get_mailer(engine: Any = None) -> Mailer:
    """Resolve the active Mailer instance.

    Precedence:
    1. Explicit ``engine.mailer`` attribute if present on engine (safely guarded
       against auto-synthesized MagicMock children per Kaizen K-11).
    2. Overridden ``_global_mailer`` (e.g. from ``set_global_mailer`` in tests).
    3. Default ``RecordingFakeMailer`` if ``NCE_MAILER_FAKE=1`` is set in environment.
    4. Default ``SMTPMailer``.
    """
    global _global_mailer

    if engine is not None:
        # Check engine.__dict__ to avoid auto-mock synthesis on MagicMock per Kaizen K-11
        has_explicit = False
        if hasattr(engine, "__dict__"):
            has_explicit = "mailer" in engine.__dict__
        elif hasattr(engine, "mailer"):
            has_explicit = True
        if has_explicit:
            cand = getattr(engine, "mailer", None)
            if cand is not None and isinstance(cand, Mailer):
                return cand

    if _global_mailer is not None:
        return _global_mailer

    if secret_env("NCE_MAILER_FAKE", "").strip().lower() in ("1", "true", "yes"):
        _global_mailer = RecordingFakeMailer()
        return _global_mailer

    return SMTPMailer()


def set_global_mailer(mailer: Mailer | None) -> None:
    """Set the process-level Mailer override (for test fixtures)."""
    global _global_mailer
    _global_mailer = mailer


def reset_mailer() -> None:
    """Reset the process-level Mailer override."""
    global _global_mailer
    _global_mailer = None
