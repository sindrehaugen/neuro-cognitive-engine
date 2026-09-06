"""Shared signing service (C7) — one ``SignTransport`` interface, multiple impls.

Consumers import from this package, not from the concrete transport modules
directly, so the dependency rule is maintained.

Currently shipped implementations
----------------------------------
``manual``
    Credential-free local transport (ships without vendor credentials).
``email_code``
    Native e-signature via emailed one-time code (Wave Q-2).

Planned (not yet built — blocked on external credentials):
    ``oneflow``, ``criipto``, ``signicat``
"""

from nce.signing_service.email_code import (
    EmailCodeTransport,
    clear_dispatched_otp_codes,
    get_dispatched_otp_codes,
)
from nce.signing_service.manual import ManualTransport, get_audit_trail, sha256_fingerprint
from nce.signing_service.transport import (
    REQUIRED_SESSION_KEYS,
    SignTransport,
    TransportMethod,
    UnimplementedTransportError,
    get_signing_transport,
    reset_signing_transports,
)

__all__ = [
    "EmailCodeTransport",
    "ManualTransport",
    "REQUIRED_SESSION_KEYS",
    "SignTransport",
    "TransportMethod",
    "UnimplementedTransportError",
    "clear_dispatched_otp_codes",
    "get_audit_trail",
    "get_dispatched_otp_codes",
    "get_signing_transport",
    "reset_signing_transports",
    "sha256_fingerprint",
]
