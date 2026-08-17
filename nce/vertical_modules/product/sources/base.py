"""
nce/vertical_modules/product/sources/base.py
============================================
Pluggable ``SourceAdapter`` abstract base for Product Engine feed adapters.

Design invariants
-----------------
* **Dependency-inward:** this module imports *nothing* from web, admin, DB, or
  any concrete adapter.  Concrete adapters depend on this base — never the
  reverse.
* **Single responsibility:** declares the contract only; zero business logic.
* **Canonical row shape:** every adapter's ``stream`` must yield dicts whose
  keys are the canonical public field names used by the Product Engine:

  Required identity fields::

      mfr_part_no  — manufacturer part number (string)
      manufacturer — manufacturer / brand name  (string)

  Optional descriptive fields (string, absent key == "")::

      product_name, short_description, long_description,
      category, sub_category, lifecycle_status, stock_qty,
      currency, gtin, product_url, image_url, weight_kg

  **Cost / margin fields are never included** in the public shape
  (ADR-0017 — internal-only).

Source adapters that carry cost/margin internally must strip those fields
before yielding from ``stream``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class SourceAdapter(ABC):
    """Abstract base for all Product Engine source adapters.

    Subclasses must implement :meth:`stream`, which asynchronously yields
    batches of canonical product row dicts.  The batch size is an
    implementation detail — callers must not assume a fixed size.

    The adapter is responsible for:

    * Reading its own configuration (env vars / secret store) at call time.
    * Normalising its native payload format to the canonical row shape.
    * Being a **no-op** (yielding nothing) when its required configuration
      key is absent.
    * **Never logging** secrets, API keys, or URLs that carry auth tokens.
    """

    @abstractmethod
    def stream(self, **kwargs: Any) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield batches of canonical (public) product row dicts.

        Implementations must:

        * Return an :class:`~collections.abc.AsyncIterator` — callers use
          ``async for batch in adapter.stream()``.
        * Yield lists; each list item is a canonical row dict.
        * Yield nothing (return immediately) when required config is absent.
        * Never include cost/margin fields (``unit_cost``, ``bid_price``,
          ``supplier_price``) in yielded rows.

        Parameters
        ----------
        **kwargs:
            Adapter-specific overrides (e.g. ``batch_size``, ``url``).
            May be ignored if the subclass has no use for them.
        """
        ...  # pragma: no cover
