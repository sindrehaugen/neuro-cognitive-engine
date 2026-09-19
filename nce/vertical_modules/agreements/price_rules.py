"""
nce/vertical_modules/agreements/price_rules.py
==============================================
Config-as-IP reference library for Agreement Pricing Rules (prisregel)
for the Agreements vertical module (Wave B-10 / Revenue Lane).

Responsibilities
----------------
* Curated pricing rules for service agreements (Config-as-IP):
  - CPI index regulation rules (standard 100% KPI, capped 80% KPI).
  - SLA room category base pricing rules (meeting rooms, boardrooms, auditoriums).
  - Volume tier discount rules on managed AV locations / rooms.
  - Multi-year commitment discount and ceiling caps.
  - Minimum agreement fee floors.
* Rule evaluation engine:
  - Evaluates rule logic deterministically against agreement/contract contexts.
  - Integrates with index series for automated index-linked regulation.
* Read-only domain query and evaluation functions:
  - ``do_get_price_rules(engine, params)``
  - ``do_evaluate_price_rule(engine, params)``

Design invariants (uncle-bob-craft)
------------------------------------
* Config-as-IP lives in this module directory, NEVER under ``nce/config_data/`` (Q-5).
* Read-only domain functions: no database mutations, pure deterministic math.
* Zero identifying host portal literals.
"""

from __future__ import annotations

import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from nce.vertical_modules.agreements.index_series import calculate_index_uplift

log = logging.getLogger("nce.vertical_modules.agreements.price_rules")

_RULES_FILE = Path(__file__).resolve().parent / "price-rules.json"

_SCALE_4DP = Decimal("0.0001")
_SCALE_2DP = Decimal("0.01")


def _quantise_money(val: Decimal) -> Decimal:
    return val.quantize(_SCALE_2DP, rounding=ROUND_HALF_UP)


def _quantise_pct(val: Decimal) -> Decimal:
    return val.quantize(_SCALE_4DP, rounding=ROUND_HALF_UP)


def load_price_rules() -> list[dict[str, Any]]:
    """Load Config-as-IP agreement pricing rules from module JSON storage."""
    try:
        if _RULES_FILE.exists():
            data = json.loads(_RULES_FILE.read_text(encoding="utf-8"))
            return data.get("rules", [])
        log.warning("Price rules file %s not found", _RULES_FILE)
        return []
    except Exception as exc:
        log.exception("Failed to load price rules from %s: %s", _RULES_FILE, exc)
        return []


def get_price_rules(
    rule_id: str | None = None,
    rule_type: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Filter and retrieve price rules definitions."""
    all_rules = load_price_rules()
    results: list[dict[str, Any]] = []

    for item in all_rules:
        if rule_id and item.get("rule_id", "").upper() != rule_id.strip().upper():
            continue
        if rule_type and item.get("rule_type", "").lower() != rule_type.strip().lower():
            continue
        if search:
            needle = search.strip().lower()
            text_corpus = (
                f"{item.get('rule_id', '')} {item.get('rule_type', '')} "
                f"{item.get('name', '')} {item.get('description', '')}"
            ).lower()
            if needle not in text_corpus:
                continue
        results.append(item)

    return results


def evaluate_price_rule(rule_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a pricing rule against a given contract or calculation context."""
    rules = get_price_rules(rule_id=rule_id)
    if not rules:
        raise ValueError(f"Unknown price rule: {rule_id!r}")
    rule = rules[0]
    rule_type = rule.get("rule_type")
    params = rule.get("parameters", {})

    if rule_type == "cpi_index_regulation":
        index_series_id = context.get("index_series_id") or params.get("index_series_id", "SSB_KPI")
        reg_ratio = context.get("regulation_ratio", params.get("regulation_ratio", 1.0))
        rule_cap = params.get("cap_pct")
        context_cap = context.get("cap_pct") or context.get("cpi_cap")

        # Context cap (e.g. contract's legal cpi_cap) takes precedence or bounds rule cap
        effective_cap: float | None = None
        if rule_cap is not None and context_cap is not None:
            effective_cap = min(float(rule_cap), float(context_cap))
        elif context_cap is not None:
            effective_cap = float(context_cap)
        elif rule_cap is not None:
            effective_cap = float(rule_cap)

        floor_pct = context.get("floor_pct", params.get("floor_pct", 0.0))

        uplift_res = calculate_index_uplift(
            series_id=str(index_series_id),
            base_period=context.get("base_period"),
            target_period=context.get("target_period"),
            base_index=context.get("base_index"),
            target_index=context.get("target_index"),
            regulation_ratio=reg_ratio,
            cap_pct=effective_cap,
            floor_pct=floor_pct,
        )

        evaluation: dict[str, Any] = {
            "rule_id": rule["rule_id"],
            "rule_name": rule["name"],
            "rule_type": rule_type,
            "index_calculation": uplift_res,
            "effective_uplift_pct": uplift_res["effective_uplift_pct"],
        }

        # If current amount is passed, compute new amounts
        current_amount = (
            context.get("current_annual_amount")
            or context.get("annual_amount")
            or context.get("current_amount")
        )
        if current_amount is not None:
            c_d = Decimal(str(current_amount))
            uplift_d = Decimal(str(uplift_res["effective_uplift_pct"]))
            adjustment_d = c_d * uplift_d
            new_d = c_d + adjustment_d

            evaluation["current_amount"] = float(_quantise_money(c_d))
            evaluation["adjustment_amount"] = float(_quantise_money(adjustment_d))
            evaluation["renewal_amount"] = float(_quantise_money(new_d))

        return evaluation

    elif rule_type == "sla_room_pricing":
        rates = params.get("rates_per_month", {})
        currency = params.get("currency", "NOK")
        multiplier = Decimal(str(params.get("on_site_sla_multiplier", 1.5)))
        is_on_site = bool(context.get("on_site", False))

        room_counts: dict[str, int] = context.get("room_counts", {})
        lines: list[dict[str, Any]] = []
        total_monthly = Decimal("0.00")

        for room_cat, count in room_counts.items():
            if count <= 0:
                continue
            base_rate = Decimal(str(rates.get(room_cat, 0.0)))
            unit_rate = base_rate * (multiplier if is_on_site else Decimal("1.0"))
            line_total = unit_rate * Decimal(count)
            total_monthly += line_total
            lines.append(
                {
                    "room_category": room_cat,
                    "count": count,
                    "unit_monthly_rate": float(_quantise_money(unit_rate)),
                    "total_monthly": float(_quantise_money(line_total)),
                }
            )

        total_annual = total_monthly * Decimal("12")
        return {
            "rule_id": rule["rule_id"],
            "rule_name": rule["name"],
            "rule_type": rule_type,
            "currency": currency,
            "is_on_site": is_on_site,
            "lines": lines,
            "total_monthly_amount": float(_quantise_money(total_monthly)),
            "total_annual_amount": float(_quantise_money(total_annual)),
        }

    elif rule_type == "volume_tier_discount":
        tiers = params.get("tiers", [])
        total_rooms = int(context.get("total_rooms", 0))
        discount_pct = Decimal("0.0")

        for tier in tiers:
            min_r = tier.get("min_rooms", 0)
            max_r = tier.get("max_rooms")
            if total_rooms >= min_r:
                if max_r is None or total_rooms <= max_r:
                    discount_pct = Decimal(str(tier.get("discount_pct", 0.0)))
                    break

        base_amount = context.get("base_amount")
        discount_amount = Decimal("0.00")
        final_amount: Decimal | None = None
        if base_amount is not None:
            b_d = Decimal(str(base_amount))
            discount_amount = b_d * discount_pct
            final_amount = b_d - discount_amount

        return {
            "rule_id": rule["rule_id"],
            "rule_name": rule["name"],
            "rule_type": rule_type,
            "total_rooms": total_rooms,
            "discount_pct": float(_quantise_pct(discount_pct)),
            "base_amount": float(_quantise_money(Decimal(str(base_amount))))
            if base_amount is not None
            else None,
            "discount_amount": float(_quantise_money(discount_amount))
            if base_amount is not None
            else None,
            "final_amount": float(_quantise_money(final_amount))
            if final_amount is not None
            else None,
        }

    elif rule_type == "multi_year_commitment":
        months = int(context.get("commitment_months", params.get("commitment_months", 36)))
        rule_months = int(params.get("commitment_months", 36))
        discount_pct = (
            Decimal(str(params.get("discount_pct", 0.08)))
            if months >= rule_months
            else Decimal("0.0")
        )
        cpi_cap = Decimal(str(params.get("cpi_cap_ceiling", 0.03)))

        return {
            "rule_id": rule["rule_id"],
            "rule_name": rule["name"],
            "rule_type": rule_type,
            "commitment_months": months,
            "qualifies_for_discount": months >= rule_months,
            "discount_pct": float(_quantise_pct(discount_pct)),
            "cpi_cap_ceiling": float(_quantise_pct(cpi_cap)),
        }

    elif rule_type == "minimum_fee_floor":
        min_fee = Decimal(str(params.get("minimum_monthly_fee", 1500.0)))
        current_fee = Decimal(str(context.get("monthly_fee", 0.0)))
        fee_adjusted = current_fee < min_fee
        final_fee = min_fee if fee_adjusted else current_fee

        return {
            "rule_id": rule["rule_id"],
            "rule_name": rule["name"],
            "rule_type": rule_type,
            "minimum_monthly_fee": float(_quantise_money(min_fee)),
            "original_monthly_fee": float(_quantise_money(current_fee)),
            "final_monthly_fee": float(_quantise_money(final_fee)),
            "is_floor_applied": fee_adjusted,
        }

    raise ValueError(f"Unsupported price rule type: {rule_type!r}")


def do_get_price_rules(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Domain query: list or search agreement pricing rules."""
    rule_id = params.get("rule_id")
    rule_type = params.get("rule_type")
    search = params.get("search")

    matches = get_price_rules(rule_id=rule_id, rule_type=rule_type, search=search)
    return {
        "status": "ok",
        "total": len(matches),
        "rules": matches,
    }


def do_evaluate_price_rule(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Domain query: evaluate a pricing rule against given context."""
    rule_id = params.get("rule_id")
    if not rule_id:
        raise ValueError("do_evaluate_price_rule: 'rule_id' is required")

    context = params.get("context", {})
    # Also support top-level params as context fallback
    merged_context = dict(params)
    merged_context.update(context)

    result = evaluate_price_rule(rule_id=str(rule_id), context=merged_context)
    result["ok"] = True
    return result
