"""Every catalogued event with a producer must have a consumer registered somewhere.

Wave B-R2 (re-baseline R2):
1. publish() consults the consumer registry (OUTBOX_HANDLERS). A selector with zero
   registered consumers is dropped with a debug log and a Prometheus counter
   (nce_outbox_unconsumed_total{selector}) — no row in outbox_events, no dead-letter,
   no alert.
2. Read-model refresher consumers are registered for the 5 <TYPE>.upserted families:
   PRODUCT_SKU.upserted, QUOTE.upserted, INVOICE.upserted, ASSET.upserted, BOM_LINE.upserted.
3. Every active event in EVENT_CATALOGUE has a registered consumer in runtime.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.events.bus import publish
from nce.events.catalogue import EVENT_CATALOGUE
from nce.observability import OUTBOX_UNCONSUMED_TOTAL
from nce.outbox_relay import OUTBOX_HANDLERS


def _runtime_selectors() -> set[str]:
    """Selectors with a handler after every standard registrar has run.

    Imports and calls the same registrars ``nce/mcp_stdio_main.py`` and
    ``nce/cron.py`` call at startup. If a new registrar is added to those
    processes it must be added here too, or this ratchet will report its
    selectors as orphans.
    """
    from nce.read_model_subscribers import register_read_model_subscribers
    from nce.vertical_modules.field_tech.work_orders import register_field_tech_subscribers
    from nce.vertical_modules.hr.compliance import register_hr_compliance_subscribers
    from nce.vertical_modules.project import automation as project_automation
    from nce.vertical_modules.project import tasks as project_tasks
    from nce.vertical_modules.resources.watcher import register_resources_event_subscribers
    from nce.vertical_modules.system_design.subscribers import (
        register_system_design_subscribers,
    )

    for registrar in (
        register_system_design_subscribers,
        register_read_model_subscribers,
        register_field_tech_subscribers,
        register_resources_event_subscribers,
        register_hr_compliance_subscribers,
        project_tasks.register_bom_task_subscriber,
        project_automation.register_automation_subscribers,
    ):
        registrar()

    return {key for key, handlers in OUTBOX_HANDLERS.items() if handlers}


def test_the_measurement_is_live() -> None:
    """Positive control: the checks below are worthless if nothing registers.

    Without this, a renamed registrar would leave ``_runtime_selectors()``
    returning an empty set -- and every check here would still pass while
    measuring nothing.
    """
    runtime = _runtime_selectors()
    assert len(runtime) >= 15, f"only {len(runtime)} runtime selectors; a registrar is broken"
    # Selectors wired by different registrars, so one broken import shows.
    assert "DEVICE.upserted" in runtime  # system_design
    assert "TICKET.dispatched" in runtime  # field_tech
    assert "CERTIFICATION.EXPIRED" in runtime  # resources
    assert "PRODUCT_SKU.upserted" in runtime  # read_model_subscribers
    assert "QUOTE.upserted" in runtime  # read_model_subscribers
    assert "INVOICE.upserted" in runtime  # read_model_subscribers
    assert "ASSET.upserted" in runtime  # read_model_subscribers
    assert "BOM_LINE.upserted" in runtime  # read_model_subscribers
    assert len(EVENT_CATALOGUE) >= 50, "the catalogue itself failed to import"


def test_no_event_claims_a_consumer_that_is_not_registered() -> None:
    """Documentation must not assert wiring that does not exist.

    This is the dangerous direction: a catalogue entry naming a consumer makes a
    reader believe the seam is live. Currently zero, and it stays zero.
    """
    runtime = _runtime_selectors()
    lying = sorted(
        f"{selector} claims {contract.declared_consumers}"
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.declared_consumers and selector not in runtime
    )
    assert not lying, (
        "these catalogue entries name a consumer that no registrar actually "
        "registers, so the event dead-letters despite the documentation:\n  " + "\n  ".join(lying)
    )


def test_every_active_event_has_registered_consumer() -> None:
    """Positive ratchet: every selector marked ACTIVE in EVENT_CATALOGUE must have a runtime handler."""
    runtime = _runtime_selectors()
    active_missing = sorted(
        selector
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.status == "ACTIVE" and selector not in runtime
    )
    assert not active_missing, (
        "ACTIVE selectors in EVENT_CATALOGUE must have a registered runtime consumer; "
        f"missing: {active_missing}"
    )


@pytest.mark.asyncio
async def test_unregistered_selector_is_dropped_and_counted_at_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave B-R2: publish() drops unconsumed selectors with debug log and Prometheus counter.

    Asserts:
    1. conn.execute is NOT called (no row inserted into outbox_events).
    2. OUTBOX_UNCONSUMED_TOTAL is incremented for the dropped selector.
    """
    selector = "AGREEMENT.upserted"
    contract = EVENT_CATALOGUE.get(selector)
    assert contract is not None
    assert contract.status == "UNCONSUMED"

    # Ensure no handler registered for this selector
    monkeypatch.setitem(OUTBOX_HANDLERS, selector, [])

    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    mock_counter = MagicMock()
    monkeypatch.setattr(OUTBOX_UNCONSUMED_TOTAL, "labels", mock_counter)

    ns_id = uuid4()
    await publish(
        mock_conn,
        namespace_id=ns_id,
        node_type="AGREEMENT",
        op="upserted",
        aggregate_id="agr-test-1",
        payload={"foo": "bar"},
    )

    # 1. Must NOT insert into outbox_events
    mock_conn.execute.assert_not_called()

    # 2. Must increment the unconsumed metric
    mock_counter.assert_called_once_with(selector=selector)
    mock_counter.return_value.inc.assert_called_once()


@pytest.mark.asyncio
async def test_registered_consumer_proceeds_to_outbox_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave B-R2: publish() writes to outbox_events when a consumer is registered."""
    _runtime_selectors()  # ensures PRODUCT_SKU.upserted is registered
    selector = "PRODUCT_SKU.upserted"
    assert OUTBOX_HANDLERS.get(selector)

    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    mock_counter = MagicMock()
    monkeypatch.setattr(OUTBOX_UNCONSUMED_TOTAL, "labels", mock_counter)

    ns_id = uuid4()
    await publish(
        mock_conn,
        namespace_id=ns_id,
        node_type="PRODUCT_SKU",
        op="upserted",
        aggregate_id="prod-test-1",
        payload={"sku": "TEST-SKU"},
    )

    # 1. Must insert into outbox_events
    mock_conn.execute.assert_called_once()
    sql = mock_conn.execute.call_args[0][0]
    assert "INSERT INTO outbox_events" in sql

    # 2. Must NOT increment the unconsumed metric
    mock_counter.assert_not_called()


def test_catalogue_status_and_consumer_declarations_consistent() -> None:
    """Ratchet: Enforce lifecycle status invariants against empty consumer declarations.

    1. No ACTIVE or UNPRODUCED selector may have empty declared_consumers.
       (Guards against fail-open predicate defects where active events drain silently).
    2. Every UNCONSUMED selector must have empty declared_consumers.
    3. Any DEPRECATED selector with empty declared_consumers is explicitly accounted for.
    """
    active_or_unproduced_with_empty_consumers = [
        selector
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.status in ("ACTIVE", "UNPRODUCED") and not contract.declared_consumers
    ]
    assert not active_or_unproduced_with_empty_consumers, (
        "ACTIVE or UNPRODUCED selectors must declare consumers; empty consumers would "
        f"cause fail-open relay bugs: {active_or_unproduced_with_empty_consumers}"
    )

    unconsumed_with_nonempty_consumers = [
        selector
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.status == "UNCONSUMED" and contract.declared_consumers
    ]
    assert not unconsumed_with_nonempty_consumers, (
        "UNCONSUMED selectors must have empty declared_consumers; otherwise they are not "
        f"unconsumed: {unconsumed_with_nonempty_consumers}"
    )

    deprecated_selectors = [
        selector
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.status == "DEPRECATED"
    ]
    assert set(deprecated_selectors) == {"cert.expiry"}, (
        f"Unexpected set of DEPRECATED selectors: {deprecated_selectors}"
    )
