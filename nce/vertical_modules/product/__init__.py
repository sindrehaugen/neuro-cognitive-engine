"""
nce/vertical_modules/product/__init__.py
======================================
Product Engine vertical module (Module 2).

Exports public core functions for catalog matching, enrichment, golden records,
spec ingestion, pricing, related products, and EOL tracking.
"""

from __future__ import annotations

from nce.vertical_modules.product._guard import (
    ProductDisabledError,
    require_product_enabled,
)
from nce.vertical_modules.product.enrich import do_enrich_product
from nce.vertical_modules.product.golden_record import do_golden_record
from nce.vertical_modules.product.ingestion import do_ingest_spec
from nce.vertical_modules.product.matching import (
    do_match_bom_line,
    do_record_match_decision,
)
from nce.vertical_modules.product.pricing import do_price_product
from nce.vertical_modules.product.related import do_related_products
from nce.vertical_modules.product.watchers import do_check_eol

__all__ = [
    "ProductDisabledError",
    "do_check_eol",
    "do_enrich_product",
    "do_golden_record",
    "do_ingest_spec",
    "do_match_bom_line",
    "do_price_product",
    "do_record_match_decision",
    "do_related_products",
    "require_product_enabled",
]
