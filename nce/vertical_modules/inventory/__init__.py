"""Inventory Engine vertical module (Module 11).

Warehouse & Inventory Engine — see
``docs/vertical_engines/11-inventory-engine.md``. This wave (Batch 129,
Module 11.Wave 1, ``locations-stock-tables``) ships only the table schema —
``stock_locations`` (hierarchical warehouse→zone→bin; a van is a flat
top-level location) and ``inventory_items`` — plus :mod:`schema_seed`'s
idempotent one-warehouse-plus-N-vans seed helper.

No ``do_*`` domain functions, MCP tools, or REST routes yet — those land in
later waves per the docs file's Build-phases list (B2 goods-receipt onward).
"""

from __future__ import annotations
