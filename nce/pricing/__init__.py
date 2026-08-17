"""
nce.pricing — Shared pricing service.

Consolidates DG-based pricing once (kills inline *0.7 and copies).
Exports dg_price, load_dg, and resolve_price.
"""

from nce.pricing.dg import dg_price, load_dg
from nce.pricing.resolver import PriceTier, resolve_price

__all__ = ["PriceTier", "dg_price", "load_dg", "resolve_price"]
