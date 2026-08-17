"""Shared signing service (C7) — one ``SignTransport`` interface, multiple impls.

Consumers import from this package, not from the concrete transport modules
directly, so the dependency rule is maintained.

Currently shipped implementations
----------------------------------
``manual``
    Credential-free local transport (ships without vendor credentials).

Planned (not yet built — blocked on external credentials):
    ``oneflow``, ``criipto``, ``signicat``
"""

from nce.signing_service.manual import ManualTransport, get_audit_trail, sha256_fingerprint
from nce.signing_service.transport import REQUIRED_SESSION_KEYS, SignTransport, TransportMethod

__all__ = [
    "ManualTransport",
    "REQUIRED_SESSION_KEYS",
    "SignTransport",
    "TransportMethod",
    "get_audit_trail",
    "sha256_fingerprint",
]
