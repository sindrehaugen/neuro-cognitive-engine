"""nce.events — thin subscribe/publish interface over the in-core transactional outbox.

Generalises ``nce.outbox_relay`` (C4 §9.6) into a typed subscribe/publish API
without forking or duplicating any relay logic.

Public surface
--------------
- ``publish``   — insert one row into ``outbox_events`` (post-commit delivery via relay).
- ``subscribe`` — register a handler keyed by ``(node_type, op)`` in the relay registry.
"""

from __future__ import annotations

from nce.events.bus import publish, subscribe

__all__ = ["publish", "subscribe"]
