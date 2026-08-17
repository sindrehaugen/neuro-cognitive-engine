"""
nce/vertical_modules/procurement/transports.py
===============================================
PoTransport interface + concrete transport implementations.

``PoTransport`` is the stable contract behind which order-placement adapters
live.  ``do_submit_po`` (Wave 11) selects the right adapter; ``do_generate_po``
(this wave) does NOT call any transport — it only creates the draft PO node.

Two implementations ship here:

``NettailerPoTransport``
    Live Nettailer order adapter.  In tests, do NOT make a real external call
    (mock ``place_order`` at the call site).

``NetsetPoTransport`` 🔴
    Stub for the unbuilt Netset Order API (tracked as a Wave blocker).
    ``place_order`` always raises ``NotImplementedError`` with a clear message
    so callers can detect the missing backend at import time — not at runtime
    silence.

Dependency rule (uncle-bob inward): this module imports only stdlib and the
``nce.http_resilience`` utility.  No web / admin / MCP / DB modules.
"""

from __future__ import annotations

import abc
import logging
from typing import Any

log = logging.getLogger("nce.vertical_modules.procurement.transports")

# ---------------------------------------------------------------------------
# Abstract base — stable contract for all PO transport adapters
# ---------------------------------------------------------------------------


class PoTransport(abc.ABC):
    """Abstract PO transport adapter.

    ``do_submit_po`` (Wave 11) selects the concrete implementation; callers
    should only depend on this interface.
    """

    @abc.abstractmethod
    async def place_order(
        self,
        po_number: str,
        supplier_id: str,
        line_items: list[dict[str, Any]],
        *,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Place a purchase order with the external supplier system.

        Parameters
        ----------
        po_number:
            Internal PO number (matches the draft PO node label ``PO:<PO_NUMBER>``).
        supplier_id:
            Supplier identifier.
        line_items:
            List of order line dicts, each with at minimum ``artnr`` and
            ``quantity``.
        namespace_id:
            Tenant namespace UUID string — passed through for audit correlation.
        idempotency_key:
            Stable key from the governed call; transport adapters SHOULD
            forward this to the external API when supported.

        Returns
        -------
        dict
            Adapter-specific confirmation payload.

        Raises
        ------
        NotImplementedError
            When the backend is not yet available (stub implementations).
        """


# ---------------------------------------------------------------------------
# Live Nettailer adapter
# ---------------------------------------------------------------------------


class NettailerPoTransport(PoTransport):
    """Live Nettailer order-placement adapter.

    In tests, mock ``place_order`` at the call site — do NOT make a real
    external HTTP call from the test suite.

    Configuration is read from environment variables (via ``nce.config``) by
    the caller; this class intentionally receives no secrets at construction
    time (twelve-factor / Secrets rule).
    """

    async def place_order(
        self,
        po_number: str,
        supplier_id: str,
        line_items: list[dict[str, Any]],
        *,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Forward a PO to the Nettailer Order API.

        The real implementation would call ``nce.http_resilience.request_with_retry``
        with the Nettailer order endpoint.  This body is the integration seam —
        Wave 11 (``do_submit_po``) instantiates this class and calls ``place_order``.
        """
        log.info(
            "[nettailer-transport] place_order po=%s supplier=%s lines=%d ns=%s ikey=%s",
            po_number,
            supplier_id,
            len(line_items),
            namespace_id[:8] if len(namespace_id) >= 8 else namespace_id,
            idempotency_key[:8],
        )
        # Real HTTP call lives here (Wave 11 wires it up with credentials).
        raise NotImplementedError(
            "NettailerPoTransport.place_order: real HTTP call not yet wired — "
            "this is called by do_submit_po (Wave 11), not do_generate_po."
        )


# ---------------------------------------------------------------------------
# Netset stub — 🔴 unbuilt Order API blocker
# ---------------------------------------------------------------------------


class NetsetPoTransport(PoTransport):
    """Stub transport for the Netset Order API (🔴 unbuilt — Wave blocker).

    The Netset Order API is not yet available.  This stub raises a clear
    ``NotImplementedError`` so callers can detect the missing backend
    immediately rather than silently sending orders nowhere.

    Replace this class body when the Netset Order API ships.
    """

    async def place_order(
        self,
        po_number: str,
        supplier_id: str,
        line_items: list[dict[str, Any]],
        *,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "Netset Order API not yet available — "
            "NetsetPoTransport.place_order is a stub until the Netset Order API ships. "
            f"po_number={po_number!r} supplier_id={supplier_id!r} "
            "Track blocker: docs/vertical_engines/01-procurement-engine.md §External 🔴"
        )
