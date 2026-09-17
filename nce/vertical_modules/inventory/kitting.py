"""
nce/vertical_modules/inventory/kitting.py
=========================================
Kitting and package reservation expansion under the settled PACKAGE decision
(Module 11, Batch 136b / Wave IN-2 — settled 2026-08-31 per
``ML_HANDOFF_2026-08-31.md:109-140``).

The Architectural Taxonomy (Settled 2026-08-31)
-----------------------------------------------
1. **Package:**
   A supplier/manufacturer grouping (one product ID, one SKU).
   - In Inventory: **Stocked as a UNIT, NEVER decompose.**
   - In Design: Decomposes to N devices (design-side only, Wave 132h).
   - Invariant: Decomposing a package in inventory reserves items never
     bought and double-counts against stock on hand. A package must be
     reserved as a single unit under its supplier SKU.
2. **Kit:**
   A product + its accessories.
   - In Inventory: Components are stocked **per piece**.
   - In Design: N devices.
   - Invariant: Expands confirmed project content (never live ``accessory_of``
     edges) into per-line reservations using ``do_reserve_stock``.
3. **Bundle:**
   Several products; may contain kits or packages.
   - In Inventory: Its members are expanded per their individual nature.

The Two Invariant Gates
-----------------------
- **Gate 1 (PACKAGE Invariant):**
  If an item is marked as a package (``is_package=True`` or ``item_type="package"``),
  it is reserved as a single unit using its parent SKU. Any nested component
  decomposition is ignored in inventory, logging and reporting that packages
  are stocked as units.
- **Gate 2 (CONFIRMATION Invariant):**
  Kitting expands ONLY confirmed project content passed in the manifest. It
  NEVER traverses or queries live classifier ``accessory_of`` edges from
  ``kg_edges`` at reservation time. Classifier edges are unconfirmed suggestions
  (confidence 0.5-0.8); confirmation into project content is the required gate.

Atomicity and Rollback
----------------------
``do_reserve_kit`` supports ``all_or_nothing: bool = True`` (default). If any line
in the kit expansion raises ``InsufficientAvailableError``, all previously
reserved lines in the batch are cleanly rolled back (released via
``do_release_stock``), and ``InsufficientAvailableError`` is re-raised with the
complete failure diagnostics.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nce.vertical_modules.inventory.reservation import (
    InsufficientAvailableError,
    _as_location_uuid,
    _as_ns_uuid,
    _as_project_label,
    _as_quantity,
    _as_sku,
    do_release_stock,
    do_reserve_stock,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.inventory.kitting")


def _expand_kit_targets(
    items: list[dict[str, Any]],
    disallow_unconfirmed_accessories: bool = True,
) -> list[dict[str, Any]]:
    """Expand project content items into flat reservation targets.

    Enforces the PACKAGE and CONFIRMATION invariants:
    - Packages are kept as single units under their parent SKU (never decomposed).
    - Kits expand their confirmed components per piece.
    - Live accessory_of edges are never expanded at reservation time.
    """
    targets: list[dict[str, Any]] = []

    for idx, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise ValueError(f"items[{idx}]: expected object, got {type(raw_item).__name__}")

        if disallow_unconfirmed_accessories and (
            raw_item.get("expand_live_accessories") or raw_item.get("query_live_edges")
        ):
            raise ValueError(
                f"items[{idx}]: live accessory_of edges cannot be expanded at reservation time; "
                "only confirmed project content may be reserved (Gate 2 invariant)"
            )

        sku = _as_sku(raw_item.get("sku"), f"items[{idx}].sku")
        qty = _as_quantity(raw_item.get("qty", 1), f"items[{idx}].qty")

        is_package = bool(
            raw_item.get("is_package", False)
            or raw_item.get("type") == "package"
            or raw_item.get("item_type") == "package"
        )
        is_kit = bool(
            raw_item.get("is_kit", False)
            or raw_item.get("type") == "kit"
            or raw_item.get("item_type") == "kit"
            or "components" in raw_item
            or "accessories" in raw_item
        )

        if is_package:
            # PACKAGE DECISION: Stocked as a UNIT, never decompose in inventory!
            # Even if sub-components are provided in the manifest, we reserve
            # the package SKU as a single unit and note the non-decomposition.
            targets.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "item_type": "package",
                    "parent_sku": None,
                    "decomposed": False,
                    "note": "package_stocked_as_unit",
                }
            )
        elif is_kit:
            # KIT DECISION: Product + confirmed accessories, components stocked per piece.
            raw_components = raw_item.get("components") or raw_item.get("accessories") or []
            if not isinstance(raw_components, list):
                raise ValueError(
                    f"items[{idx}].components: expected list, got {type(raw_components).__name__}"
                )

            # If include_parent is True or not explicitly False, reserve the base product SKU
            include_parent = raw_item.get("include_parent", True)
            if include_parent:
                targets.append(
                    {
                        "sku": sku,
                        "qty": qty,
                        "item_type": "kit_base",
                        "parent_sku": sku,
                        "decomposed": True,
                        "note": "kit_base_product",
                    }
                )

            for c_idx, raw_comp in enumerate(raw_components):
                if not isinstance(raw_comp, dict):
                    raise ValueError(
                        f"items[{idx}].components[{c_idx}]: expected object, got {type(raw_comp).__name__}"
                    )
                comp_sku = _as_sku(
                    raw_comp.get("sku") or raw_comp.get("component_sku"),
                    f"items[{idx}].components[{c_idx}].sku",
                )
                comp_qty_ratio = _as_quantity(
                    raw_comp.get("qty", 1),
                    f"items[{idx}].components[{c_idx}].qty",
                )
                total_comp_qty = qty * comp_qty_ratio
                targets.append(
                    {
                        "sku": comp_sku,
                        "qty": total_comp_qty,
                        "item_type": "kit_component",
                        "parent_sku": sku,
                        "decomposed": True,
                        "note": "kit_confirmed_accessory",
                    }
                )
        else:
            # Standard single item
            targets.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "item_type": "standard",
                    "parent_sku": None,
                    "decomposed": False,
                    "note": "standard_item",
                }
            )

    return targets


async def do_reserve_kit(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Reserve stock for a confirmed project kit or package under the PACKAGE decision.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "project_id":   str,          # required, e.g. "PROJECT:QUOTE-001"
            "location":     str | UUID,   # required, stock_locations.id
            "items":        list[dict],   # required, confirmed project lines
            "all_or_nothing": bool,       # optional, default True
        }``

    Returns
    -------
    dict
        ``{
            "ok": True,
            "project_id": str,
            "location_id": str,
            "reserved_lines": list[dict],
            "summary": {
                "total_lines": int,
                "packages_reserved": int,
                "kit_components_reserved": int,
                "standard_items_reserved": int,
            }
        }``
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    location = _as_location_uuid(params.get("location"), "location")
    project_label = _as_project_label(params.get("project_id"), "do_reserve_kit")

    raw_items = params.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("do_reserve_kit: 'items' must be a non-empty list")

    all_or_nothing = bool(params.get("all_or_nothing", True))

    targets = _expand_kit_targets(raw_items)

    completed_reservations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    reserved_lines: list[dict[str, Any]] = []

    packages_count = 0
    kit_components_count = 0
    standard_count = 0

    try:
        for target in targets:
            res = await do_reserve_stock(
                engine,
                {
                    "namespace_id": str(ns_uuid),
                    "sku": target["sku"],
                    "qty": target["qty"],
                    "location": str(location),
                    "project_id": project_label,
                },
            )
            completed_reservations.append((target, res))
            line_report = {
                "sku": target["sku"],
                "qty": float(target["qty"]),
                "item_type": target["item_type"],
                "parent_sku": target["parent_sku"],
                "decomposed": target["decomposed"],
                "note": target["note"],
                "on_hand": float(res["on_hand"]),
                "reserved": float(res["reserved"]),
                "blocked": float(res["blocked"]),
                "available": float(res["available"]),
            }
            reserved_lines.append(line_report)

            if target["item_type"] == "package":
                packages_count += 1
            elif target["item_type"] in ("kit_component", "kit_base"):
                kit_components_count += 1
            else:
                standard_count += 1

    except InsufficientAvailableError as exc:
        if all_or_nothing:
            log.warning(
                "do_reserve_kit: Insufficient stock for %s on project %s; rolling back %d reservations",
                exc.sku,
                project_label,
                len(completed_reservations),
            )
            # Rollback previously reserved lines in reverse order
            for prev_target, _ in reversed(completed_reservations):
                try:
                    await do_release_stock(
                        engine,
                        {
                            "namespace_id": str(ns_uuid),
                            "sku": prev_target["sku"],
                            "qty": prev_target["qty"],
                            "location": str(location),
                            "project_id": project_label,
                        },
                    )
                except Exception as rb_exc:
                    log.error(
                        "do_reserve_kit rollback failure for sku=%s: %s",
                        prev_target["sku"],
                        rb_exc,
                    )
            raise exc
        else:
            raise exc

    return {
        "ok": True,
        "project_id": project_label,
        "location_id": str(location),
        "reserved_lines": reserved_lines,
        "summary": {
            "total_lines": len(reserved_lines),
            "packages_reserved": packages_count,
            "kit_components_reserved": kit_components_count,
            "standard_items_reserved": standard_count,
        },
    }


async def do_release_kit(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Release stock reservations for a kit or package under the PACKAGE decision.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "project_id":   str,          # required, e.g. "PROJECT:QUOTE-001"
            "location":     str | UUID,   # required, stock_locations.id
            "items":        list[dict],   # required, confirmed project lines
        }``

    Returns
    -------
    dict
        ``{
            "ok": True,
            "project_id": str,
            "location_id": str,
            "released_lines": list[dict],
            "summary": {
                "total_lines": int,
                "packages_released": int,
                "kit_components_released": int,
                "standard_items_released": int,
            }
        }``
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")
    location = _as_location_uuid(params.get("location"), "location")
    project_label = _as_project_label(params.get("project_id"), "do_release_kit")

    raw_items = params.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("do_release_kit: 'items' must be a non-empty list")

    targets = _expand_kit_targets(raw_items)

    released_lines: list[dict[str, Any]] = []
    packages_count = 0
    kit_components_count = 0
    standard_count = 0

    for target in targets:
        res = await do_release_stock(
            engine,
            {
                "namespace_id": str(ns_uuid),
                "sku": target["sku"],
                "qty": target["qty"],
                "location": str(location),
                "project_id": project_label,
            },
        )
        line_report = {
            "sku": target["sku"],
            "qty": float(target["qty"]),
            "item_type": target["item_type"],
            "parent_sku": target["parent_sku"],
            "decomposed": target["decomposed"],
            "note": target["note"],
            "on_hand": float(res["on_hand"]),
            "reserved": float(res["reserved"]),
            "blocked": float(res["blocked"]),
            "available": float(res["available"]),
        }
        released_lines.append(line_report)

        if target["item_type"] == "package":
            packages_count += 1
        elif target["item_type"] in ("kit_component", "kit_base"):
            kit_components_count += 1
        else:
            standard_count += 1

    return {
        "ok": True,
        "project_id": project_label,
        "location_id": str(location),
        "released_lines": released_lines,
        "summary": {
            "total_lines": len(released_lines),
            "packages_released": packages_count,
            "kit_components_released": kit_components_count,
            "standard_items_released": standard_count,
        },
    }
