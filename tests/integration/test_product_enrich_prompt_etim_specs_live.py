"""
tests/integration/test_product_enrich_prompt_etim_specs_live.py
==================================================================
Live-Postgres sibling for `_fetch_product_row`/`_call_product_enrichment`'s
handling of `product_catalog.etim_specs`
(`nce/vertical_modules/product/enrich.py`). Dispatched from E's fourteen-site
jsonb-decode sweep, assigned to Lane F -- the second, distinct symptom on
this same column (see `test_capability_sync_etim_merge_live.py` for the
first, a silent-discard-before-merge bug in a different module).

THE BUG, EXACTLY
------------------
`product_catalog.etim_specs` is jsonb; no jsonb codec is registered on this
project's pool (confirmed: nce/semantic_search.py's own comment states the
same fact), so asyncpg always hands the column back as a raw JSON STRING,
never a dict. `_fetch_product_row` returned `dict(row)` unchanged, so
`product_row["etim_specs"]` was a string. The LLM prompt builder then did
`json.dumps(product_row.get("etim_specs") or {})` -- since the value is a
non-empty STRING (truthy), `json.dumps()` re-serializes the STRING ITSELF:
wrapping it in an extra pair of quotes and escaping every internal `"`,
producing a garbled, double-encoded blob in the prompt
(`"{\\"manufacturer\\": \\"Extron\\", ...}"`) instead of the intended
pretty-printed JSON object (`{"manufacturer": "Extron", ...}`). This is a
silent-wrong-answer defect, not a crash: the LLM receives a confusing,
harder-to-parse representation of the product's existing specs on every
enrichment call, degrading (not failing) the enrichment it produces.

THE FIX
---------
`_fetch_product_row` now decodes `etim_specs` once, at the source, so every
consumer of the returned dict gets a real dict -- `json.dumps()` on it then
produces the intended plain object, matching what a caller reading this
function's own type hint (`dict[str, Any]`) would already assume it does.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest

from nce.vertical_modules.product.enrich import _call_product_enrichment, _fetch_product_row

pytestmark = pytest.mark.integration

_REAL_ETIM_SPECS = {
    "manufacturer": "Extron",
    "device_category": "switcher",
    "power_draw_watts": 45,
    "poe_class": 4,
}


class _CapturingProvider:
    """Stub LLMProvider: captures the exact prompt text sent, returns a
    canned response -- the only thing this test mocks is the truly external
    LLM call, not the database layer under test."""

    def __init__(self) -> None:
        self.last_messages: list[Any] | None = None

    async def complete(self, messages: list[Any], response_model: type) -> Any:
        self.last_messages = messages
        return response_model(proposals=[])


async def _create_product(pg_pool: asyncpg.Pool, mfr_part_no: str) -> uuid.UUID:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO product_catalog (manufacturer, mfr_part_no, product_source_id, etim_specs) "
            "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
            "Extron",
            mfr_part_no,
            f"SRC-{mfr_part_no}",
            json.dumps(_REAL_ETIM_SPECS),
        )


@pytest.mark.asyncio
async def test_fetch_product_row_decodes_etim_specs_to_a_real_dict(pg_pool: asyncpg.Pool) -> None:
    """The narrowest possible regression: the function's own return-type
    hint (`dict[str, Any]`) promises `etim_specs` is usable as a dict --
    against a real row, it was actually a string."""
    mfr_part_no = f"DTP-{uuid.uuid4().hex[:8]}"
    product_id = await _create_product(pg_pool, mfr_part_no)

    async with pg_pool.acquire() as conn:
        row = await _fetch_product_row(conn, str(product_id))

    assert row is not None
    assert isinstance(row["etim_specs"], dict), (
        f"etim_specs was returned as {type(row['etim_specs'])}, not a dict -- "
        f"the exact defect this test exists to catch"
    )
    assert row["etim_specs"] == _REAL_ETIM_SPECS


@pytest.mark.asyncio
async def test_enrichment_prompt_contains_a_real_json_object_not_a_double_encoded_string(
    pg_pool: asyncpg.Pool,
) -> None:
    """The user-visible consequence: the prompt actually SENT to the LLM
    must contain the specs as a real JSON object, round-trippable by
    json.loads, not a JSON string literal wrapping the whole thing a
    second time.
    """
    mfr_part_no = f"DTP-{uuid.uuid4().hex[:8]}"
    product_id = await _create_product(pg_pool, mfr_part_no)

    async with pg_pool.acquire() as conn:
        product_row = await _fetch_product_row(conn, str(product_id))
    assert product_row is not None

    provider = _CapturingProvider()
    await _call_product_enrichment(provider, product_row, ["device_category"], {"source": "test"})

    assert provider.last_messages is not None
    prompt_text = provider.last_messages[-1].content
    assert "Existing Specifications:" in prompt_text

    specs_line = next(
        line for line in prompt_text.splitlines() if line.startswith("- Existing Specifications:")
    )
    embedded_json = specs_line.split("- Existing Specifications:", 1)[1].strip()

    # A double-encoded value would itself be a JSON STRING (quoted), whose
    # parsed result is another string that still needs a second json.loads
    # to reach the real object. A correctly-encoded value parses straight
    # to a dict on the first pass.
    parsed = json.loads(embedded_json)
    assert isinstance(parsed, dict), (
        f"the embedded specs parsed to a {type(parsed)}, not a dict -- this is exactly what a "
        f"double-encoded string produces (json.loads of a JSON-string-of-a-JSON-string yields "
        f"another string): {embedded_json!r}"
    )
    assert parsed == _REAL_ETIM_SPECS
