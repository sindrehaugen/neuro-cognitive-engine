"""A tool's ``required`` list must match what its handler actually demands.

Why this is not just a restatement of the schema
------------------------------------------------
The obvious version of this test — a table of expected ``required`` lists checked
against the schemas — is **self-confirming**: both sides would be copied from the
same place, so it would pass even if every schema were wrong. That is the exact
defect shape found in ``tests/test_actor_trust.py`` on 2026-09-02, where two tests
derived their expectation from the same read they were validating.

So the expectation here comes from an **independent source**: the handler's own
docstring, which marks each argument required or optional in prose the schema
does not participate in. Drift in *either* direction fails — a schema that adds a
required field the docs call optional, and a schema that drops one the docs call
required.

Scope, stated honestly
----------------------
This covers tools whose handler or core documents its arguments with the two
conventions used in this repo:

    ``bom_line``   (str, required)          -- numpydoc-ish param blocks
    queue_id (str):   Required. ...         -- "Arguments:" blocks

It is **not** a general contract verifier. Tools whose contract is recorded
nowhere cannot be checked by anything, which is the open half of OQ-3 and needs
one sentence per tool from its module owner — not a cleverer regex here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

#: tool -> (file, function that reads the arguments)
#: The MCP handlers are thin pass-throughs, so for several tools the contract
#: lives in the ``do_*`` core one level down; that is the function named here.
_CONTRACT_SOURCE: dict[str, tuple[str, str]] = {
    "product_match_bom_line": ("nce/vertical_modules/product/matching.py", "do_match_bom_line"),
    "product_price": ("nce/vertical_modules/product/pricing.py", "do_price_product"),
    "product_related": ("nce/vertical_modules/product/related.py", "do_related_products"),
    "product_enrich": (
        "nce/vertical_modules/product/mcp_handlers.py",
        "handle_product_enrich",
    ),
    "resolve": ("nce/entity_resolution/mcp_handlers.py", "handle_resolve"),
    "merge_queue_list": ("nce/entity_resolution/mcp_handlers.py", "handle_merge_queue_list"),
    "merge_queue_confirm": (
        "nce/entity_resolution/mcp_handlers.py",
        "handle_merge_queue_confirm",
    ),
    "merge_queue_reject": (
        "nce/entity_resolution/mcp_handlers.py",
        "handle_merge_queue_reject",
    ),
    "procurement_calculate_tco": (
        "nce/vertical_modules/procurement/mcp_handlers.py",
        "handle_procurement_calculate_tco",
    ),
    "procurement_evaluate_match": (
        "nce/vertical_modules/procurement/mcp_handlers.py",
        "handle_procurement_evaluate_match",
    ),
    "procurement_forecast_rebate": (
        "nce/vertical_modules/procurement/mcp_handlers.py",
        "handle_procurement_forecast_rebate",
    ),
    "procurement_rank_suppliers": (
        "nce/vertical_modules/procurement/mcp_handlers.py",
        "handle_procurement_rank_suppliers",
    ),
    "procurement_recommend_move_spend": (
        "nce/vertical_modules/procurement/mcp_handlers.py",
        "handle_procurement_recommend_move_spend",
    ),
    "procurement_whatif_spend": (
        "nce/vertical_modules/procurement/mcp_handlers.py",
        "handle_procurement_whatif_spend",
    ),
    "vendors_calibrate_weights": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_calibrate_weights",
    ),
    "vendors_check_tier_at_risk": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_check_tier_at_risk",
    ),
    "vendors_compute_performance": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_compute_performance",
    ),
    "vendors_compute_scorecard": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_compute_scorecard",
    ),
    "vendors_detect_reliability_degradation": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_detect_reliability_degradation",
    ),
    "vendors_get_tier_status": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_get_tier_status",
    ),
    "vendors_match_contractor": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_match_contractor",
    ),
    "vendors_recall_similar_jobs": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_recall_similar_jobs",
    ),
    "vendors_reliability_radar": (
        "nce/vertical_modules/vendors/mcp_handlers.py",
        "handle_vendors_reliability_radar",
    ),
    "pricing_resolve": (
        "nce/pricing/mcp_handlers.py",
        "handle_pricing_resolve",
    ),
    "project_can_enter_phase": (
        "nce/vertical_modules/project/mcp_handlers.py",
        "handle_project_can_enter_phase",
    ),
    "project_suggest_pl": (
        "nce/vertical_modules/project/mcp_handlers.py",
        "handle_project_suggest_pl",
    ),
    "agreements_lookup_terms": (
        "nce/vertical_modules/agreements/mcp_handlers.py",
        "handle_agreements_lookup_terms",
    ),
    "sales_get_quote_lines": (
        "nce/vertical_modules/sales/mcp_handlers.py",
        "handle_sales_get_quote_lines",
    ),
    "sales_get_signed_baseline": (
        "nce/vertical_modules/sales/mcp_handlers.py",
        "handle_sales_get_signed_baseline",
    ),
    "sales_ping": (
        "nce/vertical_modules/sales/mcp_handlers.py",
        "handle_sales_ping",
    ),
    "economy_match_invoice": (
        "nce/vertical_modules/economy/mcp_handlers.py",
        "handle_economy_match_invoice",
    ),
    "economy_compute_periodisering": (
        "nce/vertical_modules/economy/mcp_handlers.py",
        "handle_economy_compute_periodisering",
    ),
    "economy_emit_event": (
        "nce/vertical_modules/economy/mcp_handlers.py",
        "handle_economy_emit_event",
    ),
    "detect_causal_cycles": (
        "nce/replay_mcp_handlers.py",
        "handle_detect_causal_cycles",
    ),
    "system_design_from_quote": (
        "nce/vertical_modules/system_design/mcp_handlers.py",
        "handle_system_design_from_quote",
    ),
    "system_design_to_quote": (
        "nce/vertical_modules/system_design/mcp_handlers.py",
        "handle_system_design_to_quote",
    ),
    "system_design_generate_sow": (
        "nce/vertical_modules/system_design/mcp_handlers.py",
        "handle_system_design_generate_sow",
    ),
    "system_design_enrich_design_lines": (
        "nce/vertical_modules/system_design/mcp_handlers.py",
        "handle_system_design_enrich_design_lines",
    ),
    "system_design_propose_design": (
        "nce/vertical_modules/system_design/mcp_handlers.py",
        "handle_system_design_propose_design",
    ),
    "sales_add_quote_line": (
        "nce/vertical_modules/sales/mcp_handlers.py",
        "handle_sales_add_quote_line",
    ),
}

# ``name`` (type, required)  |  ``name`` (type, optional, ...)
_PARAM_BLOCK = re.compile(r"``(?P<name>[a-z_][a-z0-9_]*)``\s*\((?P<meta>[^)]*)\)", re.IGNORECASE)
# name (type):   Required. ...     /  name (type): Optional. ...
_ARGS_BLOCK = re.compile(
    r"^\s*(?P<name>[a-z_][a-z0-9_]*)\s*\([^)]*\)\s*:\s*(?P<meta>.*)$", re.MULTILINE
)
# "Required arguments:" / "Optional arguments:" section headers
_SECTION = re.compile(r"^\s*(Required|Optional) arguments?:\s*$", re.MULTILINE)
# Inside such a section, the argument name starts the LINE -- bare
# (`namespace_id (str, UUID)`) or backticked (```product_id``  (str, UUID)`).
# Must be line-anchored: a name in continuation prose ("must contain
# ``unit_price`` (float)") is a NESTED field, not a top-level argument, and
# harvesting those made three procurement cases fail on first run.
# `{0,2} not ``? -- the latter REQUIRES one backtick, so bare names never
# matched and every procurement case came back empty on the first attempt.
# The parenthesised part must also look like a TYPE. Line-anchoring alone was
# not enough: a WRAPPED CONTINUATION line begins with prose whose first word can
# still be followed by "(" -- economy's "see ``x.do_y`` for the exact / shape
# (``buckets``/``project_id``)" made `shape` read as an argument, failing two
# cases on first run.
_TYPE_TOKEN = r"(?:str|int|float|bool|dict|list|tuple|set|number|uuid|UUID|Any)"
_ARG_AT_LINE_START = re.compile(
    r"^\s*`{0,2}([a-z_][a-z0-9_]*)`{0,2}\s*\(\s*" + _TYPE_TOKEN, re.MULTILINE
)
# vendors'/project's one-liner: Requires ``a``, ``b`` (dict with ``c``) ... in *arguments*.
# Capture the whole span; parenthesised sub-spans are stripped before harvesting
# because names inside them are FIELDS of an argument, not arguments.
_REQUIRES_INLINE = re.compile(r"Requires\s+(.*?)\bin\s+\*arguments\*", re.DOTALL)
_PARENTHESISED = re.compile(r"\([^()]*\)")
# numpydoc-style block:  namespace_id : str  (required)
_NUMPYDOC_ARG = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s*:\s*[^\n(]*\(\s*required\s*\)", re.MULTILINE | re.IGNORECASE
)


def _docstring(rel: str, func: str) -> str:
    with open(_REPO / rel, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{func} not found in {rel}")


def _documented_required(doc: str) -> set[str]:
    """Argument names the docstring marks as required, by either convention."""
    required: set[str] = set()

    for m in _PARAM_BLOCK.finditer(doc):
        meta = m.group("meta").lower()
        if "required" in meta and "optional" not in meta:
            required.add(m.group("name"))

    for m in _ARGS_BLOCK.finditer(doc):
        meta = m.group("meta").lower()
        if meta.startswith("required") or " required." in meta:
            required.add(m.group("name"))

    # A "Required arguments:" section: every ``name`` until the next header.
    for section in _SECTION.finditer(doc):
        if section.group(1) != "Required":
            continue
        rest = doc[section.end() :]
        stop = _SECTION.search(rest)
        body = rest[: stop.start()] if stop else rest
        required.update(m.group(1) for m in _ARG_AT_LINE_START.finditer(body))

    # vendors'/project's one-line form. Strip parenthesised sub-spans first:
    # "``project`` (dict with ``current_phase`` and ``criteria_met``)" documents
    # ONE argument whose fields happen to be named.
    for m in _REQUIRES_INLINE.finditer(doc):
        span = m.group(1)
        while True:
            stripped = _PARENTHESISED.sub(" ", span)
            if stripped == span:
                break
            span = stripped
        required.update(re.findall(r"``([a-z_][a-z0-9_]*)``", span))

    # numpydoc "name : type (required)"
    required.update(m.group(1) for m in _NUMPYDOC_ARG.finditer(doc))

    return required


def _schema_required(name: str) -> list[str]:
    from nce import mcp_stdio_tools as tools_mod

    for tool in tools_mod.TOOLS:
        if tool.name == name:
            return list(tool.inputSchema.get("required", []))
    raise AssertionError(f"{name} is not advertised in mcp_stdio_tools.TOOLS")


@pytest.mark.parametrize("tool", sorted(_CONTRACT_SOURCE))
def test_schema_required_matches_the_documented_contract(tool: str) -> None:
    rel, func = _CONTRACT_SOURCE[tool]
    documented = _documented_required(_docstring(rel, func))

    assert documented, (
        f"{rel}::{func} documents no required arguments, so this test would pass "
        "vacuously. Either the docstring lost its argument block or the parser no "
        "longer recognises its convention -- fix one of those, do not delete the case."
    )

    schema = set(_schema_required(tool))
    assert schema == documented, (
        f"{tool}: inputSchema['required'] is {sorted(schema)} but "
        f"{rel}::{func} documents {sorted(documented)} as required.\n"
        "  missing from schema: " + str(sorted(documented - schema)) + "\n"
        "  claimed but not documented required: " + str(sorted(schema - documented))
    )


def test_every_case_resolves_to_a_real_function() -> None:
    """The table cannot rot into silently-skipped entries."""
    for tool, (rel, func) in sorted(_CONTRACT_SOURCE.items()):
        assert (_REPO / rel).is_file(), f"{tool}: {rel} does not exist"
        assert _docstring(rel, func), f"{tool}: {rel}::{func} has no docstring"


def test_the_parser_can_actually_fail() -> None:
    """Guard the guard: the required-marker parser must discriminate.

    A parser that returned everything, or nothing, would make every case above
    either vacuous or permanently red. Prove it separates the two conventions'
    required fields from their optional ones.
    """
    doc = """
        params:
            ``namespace_id``   (str, required)
            ``bom_line``       (str, required)    -- raw line.
            ``manufacturer``   (str, optional)    -- hint.
    """
    assert _documented_required(doc) == {"namespace_id", "bom_line"}

    doc2 = """
        Arguments:
            namespace_id (str):     Required. UUID of the namespace.
            queue_id (str):         Required. UUID of the row.
            note (str):             Optional. Free text.
    """
    assert _documented_required(doc2) == {"namespace_id", "queue_id"}

    assert _documented_required("no argument documentation at all") == set()

    # tranche 5, convention 3: bare names inside a "Required arguments:" section
    doc3 = """
        Required arguments:
            namespace_id (str, UUID)
            supplier     (dict) -- must contain unit_price.
        Optional arguments:
            supplier_id  (str) -- filter to one supplier.
    """
    assert _documented_required(doc3) == {"namespace_id", "supplier"}

    # tranche 5, convention 4: the vendors one-liner
    # a NESTED field name in continuation prose must NOT be read as an argument
    doc3b = """
        Required arguments:
            namespace_id (str, UUID)
            supplier     (dict) -- must contain ``unit_price`` (float).
    """
    assert _documented_required(doc3b) == {"namespace_id", "supplier"}

    doc4 = "Requires ``namespace_id`` and ``vendor_id`` in *arguments*."
    assert _documented_required(doc4) == {"namespace_id", "vendor_id"}

    # ...and it must not swallow a sentence that merely mentions arguments
    assert _documented_required("This tool reads ``namespace_id`` from the caller.") == set()

    # a wrapped continuation line must not be read as an argument: its first
    # word is prose, and the parens hold field names rather than a type.
    doc3c = """
        Required arguments:
            namespace_id (str, UUID)
            params       (dict) -- see ``x.do_y`` for the exact
                         shape (``buckets``/``project_id``).
    """
    assert _documented_required(doc3c) == {"namespace_id", "params"}

    # tranche 6, convention 5: nested field names inside the inline form are
    # FIELDS of an argument, not arguments.
    doc5 = (
        "Requires ``namespace_id``, ``project`` (dict with ``current_phase`` and "
        "``criteria_met``), and ``target_phase`` (str) in *arguments*."
    )
    assert _documented_required(doc5) == {"namespace_id", "project", "target_phase"}

    # tranche 6, convention 6: numpydoc "name : type (required)"
    doc6 = """
        Arguments
        ---------
        namespace_id : str  (required)
        quote_id     : str  (required) -- the Sales QUOTE identifier.
        note         : str  (optional)
    """
    assert _documented_required(doc6) == {"namespace_id", "quote_id"}
