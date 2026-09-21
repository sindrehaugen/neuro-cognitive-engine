"""
tests/integration/test_support_tickets_jsonb_decode_live.py
==============================================================
Live-Postgres proof that `support/tickets.py`'s shared `_row_to_dict`
helper actually decodes the JSONB columns it returns.

No asyncpg jsonb codec is registered anywhere in this codebase
(`nce/semantic_search.py`'s own comment documents this estate-wide fact),
so `service_tickets.ai_diagnosis`/`.events`/`sla_clocks.paused_intervals`/
`customer_health.trend`/`.drivers` all arrive from a real connection as raw
JSON strings, never already-parsed dicts/lists. `_row_to_dict` only ever
converted `UUID`/`datetime` fields and passed everything else through raw
-- every response from `do_open_ticket`/`do_query_ticket`/`do_update_
ticket`/`support/ecosystem.py`/`support/health.py` shipped these columns
as undecoded strings. Found while sweeping every hand-written JSONB
writer/reader in the estate for the convention `documents.py` (#406)
missed.

Every existing unit test for this module mocks the row with the column
already set to a Python dict/list (never a string), so this bug was
structurally invisible to all of them -- exactly the shape F's estate-wide
"51 mocked DB-dependent modules" finding describes. This file is the live
sibling that actually reaches Postgres.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from nce.vertical_modules.support.tickets import do_open_ticket

pytestmark = pytest.mark.integration


async def test_open_ticket_returns_ai_diagnosis_as_a_real_dict(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """The actual regression: create a ticket with a real, non-trivial
    `ai_diagnosis` payload. `do_open_ticket`'s own INSERT already writes
    it correctly (`json.dumps`, confirmed by reading the write path
    before this fix); the bug was entirely on the read side. Before this
    fix, the returned `ai_diagnosis` would be the raw JSON string
    `'{"root_cause": "..."}'`, not a dict -- `result["ticket"]
    ["ai_diagnosis"]["root_cause"]` would raise `TypeError: string
    indices must be integers` on a string, not a dict.
    """
    result = await do_open_ticket(
        pg_pool,
        {
            "namespace_id": str(namespace_id),
            "summary": "jsonb decode probe",
            "ai_diagnosis": {"root_cause": "flaky sensor", "confidence": 0.8},
        },
    )
    ticket = result["ticket"]
    assert isinstance(ticket["ai_diagnosis"], dict), (
        f"ai_diagnosis was not decoded to a dict, got {type(ticket['ai_diagnosis'])}: "
        f"{ticket['ai_diagnosis']!r}"
    )
    assert ticket["ai_diagnosis"]["root_cause"] == "flaky sensor"
    assert ticket["ai_diagnosis"]["confidence"] == 0.8


async def test_open_ticket_returns_events_as_a_real_list_not_the_string_default(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """`service_tickets.events` defaults to `'[]'::jsonb` when no caller
    ever sets it -- before this fix, a fresh ticket's `events` would come
    back as the literal two-character string `"[]"`, not an empty list.
    `"[]" == []` is `False` in Python, so this is a real, checkable
    difference, not just a type-annotation nicety.
    """
    result = await do_open_ticket(
        pg_pool,
        {
            "namespace_id": str(namespace_id),
            "summary": "jsonb decode probe - events default",
        },
    )
    ticket = result["ticket"]
    assert ticket["events"] == [], (
        f"events was not decoded to an empty list, got {type(ticket['events'])}: "
        f"{ticket['events']!r}"
    )


async def test_open_ticket_sla_clock_paused_intervals_is_a_real_list(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """`do_open_ticket` also creates the initial SLA clock row and returns
    it through the same `_row_to_dict` helper -- `sla_clocks.
    paused_intervals` defaults to `'[]'::jsonb` the same way `events`
    does, and is a separate table/column from the ticket's own JSONB
    fields, so this proves the fix covers more than one call site's worth
    of columns, not just `service_tickets`.
    """
    result = await do_open_ticket(
        pg_pool,
        {
            "namespace_id": str(namespace_id),
            "summary": "jsonb decode probe - sla clock",
            "create_sla_clock": True,
        },
    )
    sla_clock = result.get("sla_clock")
    assert sla_clock is not None, "no sla_clock was created/returned to check"
    assert sla_clock["paused_intervals"] == [], (
        f"paused_intervals was not decoded to an empty list, got "
        f"{type(sla_clock['paused_intervals'])}: {sla_clock['paused_intervals']!r}"
    )
