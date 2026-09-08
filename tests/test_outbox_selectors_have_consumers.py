"""Every catalogued event with a producer must have a consumer registered somewhere.

The relay does not ignore an event it has no handler for -- ``run_outbox_relay_once``
raises ``OutboxDeliveryError``, dead-letters the event immediately and dispatches
an alert (``nce/outbox_relay.py:423``). That is the right behaviour for a
misconfiguration, and it means a produced event with no registered subscriber
turns ordinary traffic into DLQ rows and alerts.

Measured on ``main`` when this ratchet landed: **52 catalogued selectors have a
declared producer and no runtime handler in any process.** They include the
``<TYPE>.upserted`` events that every graph projection emits -- ``PRODUCT.upserted``,
``QUOTE.upserted``, ``INVOICE.upserted``, ``ASSET.upserted``, ``BOM_LINE.upserted`` --
so on a live system with real traffic the dead-letter queue and the alert channel
are filled by normal operation. A DLQ that fills up during ordinary use trains
everyone to ignore it, which is the same argument the repository already made for
gating the permanently-red Pages job.

🔴 **No existing test can see this.** A test registers only the subscribers it
needs, so the relay always has a handler for the event under test. The gap is
visible solely by importing the real registrars and comparing what they register
against what the catalogue says is produced.

This ratchet does three things:

1. Pins the orphan set SHRINK-ONLY. A new produced-but-unconsumed selector fails.
2. Asserts that **no** event claims a consumer that is not actually registered.
   That is the dangerous direction -- documentation asserting wiring that does not
   exist -- and it is currently at zero. It stays at zero.
3. Proves the measurement is live, so the two checks above cannot pass by
   measuring nothing.
"""

from __future__ import annotations

import pytest

from nce.events.catalogue import EVENT_CATALOGUE
from nce.outbox_relay import OUTBOX_HANDLERS

# ---------------------------------------------------------------------------
# Shrink-only. 🔴 This list may only get SHORTER.
# ---------------------------------------------------------------------------
# Each entry is a catalogued selector whose producer publishes into
# ``outbox_events`` while no process registers a handler, so every emission
# dead-letters with an alert. Removing an entry means either a consumer was
# registered, or the producer was retired. Adding one means shipping that
# behaviour deliberately -- do not.
KNOWN_UNCONSUMED_PRODUCED_SELECTORS: frozenset[str] = frozenset(
    {
        "ACCOUNT.upserted",
        "AGREEMENT.upserted",
        "AGREEMENT_SIGNATURE.upserted",
        "AGREEMENT_TERM.upserted",
        "ASSET.upserted",
        "BOM_LINE.upserted",
        "CABLE.deleted",
        "CABLE.retired",
        "CASE_STUDY.upserted",
        "CERT.upserted",
        "CONTRACTOR.upserted",
        "CUSTOMER.upserted",
        "D365_Account.upserted",
        "D365_KnowledgeArticle.upserted",
        "DEAL.upserted",
        "DESIGN.retired",
        "DESIGN_LINE.retired",
        "DEVICE.deleted",
        "DEVICE.retired",
        "DYNAMICS_ENTITY.upserted",
        "FUNCTIONAL_LOCATION.retired",
        "GATE.upserted",
        "GOODS_RECEIPT.upserted",
        "INVENTORY_ITEM.upserted",
        "INVENTORY_RMA.upserted",
        "INVOICE.upserted",
        "LEAD.upserted",
        "MARGIN.upserted",
        "OPPORTUNITY.upserted",
        "PERIOD.upserted",
        "PO.upserted",
        "PORT.deleted",
        "PORT.retired",
        "POSTING.upserted",
        "PO_LINE.upserted",
        "PROCUREMENT_MATCH.upserted",
        "PRODUCT.upserted",
        "PRODUCT_SKU.upserted",
        "PROJECT.upserted",
        "PROJECT_CASE_STUDY.upserted",
        "PROJECT_GATE.upserted",
        "PROJECT_PROJECT.upserted",
        "PROJECT_TASK.upserted",
        "PURCHASE_ORDER.upserted",
        "QUOTE.edge_realized_as",
        "QUOTE.upserted",
        "RACK.deleted",
        "RACK.retired",
        "STOCK_LOCATION.upserted",
        "TASK.upserted",
        "TICKET.sla_breached",
        "VENDOR.upserted",
    }
)


def _runtime_selectors() -> set[str]:
    """Selectors with a handler after every standard registrar has run.

    Imports and calls the same registrars ``nce/mcp_stdio_main.py`` and
    ``nce/cron.py`` call at startup. If a new registrar is added to those
    processes it must be added here too, or this ratchet will report its
    selectors as orphans.
    """
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
        register_field_tech_subscribers,
        register_resources_event_subscribers,
        register_hr_compliance_subscribers,
        project_tasks.register_bom_task_subscriber,
        project_automation.register_automation_subscribers,
    ):
        registrar()

    return {key for key, handlers in OUTBOX_HANDLERS.items() if handlers}


def test_the_measurement_is_live() -> None:
    """Positive control: the two checks below are worthless if nothing registers.

    Without this, a renamed registrar would leave ``_runtime_selectors()``
    returning an empty set -- and every check here would still pass while
    measuring nothing.
    """
    runtime = _runtime_selectors()
    assert len(runtime) >= 10, f"only {len(runtime)} runtime selectors; a registrar is broken"
    # Three that are wired by three DIFFERENT registrars, so one broken import shows.
    assert "DEVICE.upserted" in runtime  # system_design
    assert "TICKET.dispatched" in runtime  # field_tech
    assert "CERTIFICATION.EXPIRED" in runtime  # resources
    assert len(EVENT_CATALOGUE) >= 50, "the catalogue itself failed to import"


def test_no_event_claims_a_consumer_that_is_not_registered() -> None:
    """Documentation must not assert wiring that does not exist.

    This is the dangerous direction: a catalogue entry naming a consumer makes a
    reader believe the seam is live. Currently zero, and it stays zero -- unlike
    the orphan list below, there is no allowlist for this.
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


def test_produced_but_unconsumed_selectors_do_not_grow() -> None:
    """A produced event with no handler dead-letters on every emission."""
    runtime = _runtime_selectors()
    orphans = {
        selector
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.declared_producers and selector not in runtime
    }

    new = sorted(orphans - KNOWN_UNCONSUMED_PRODUCED_SELECTORS)
    assert not new, (
        "these selectors are produced but no process registers a handler, so every "
        "emission raises OutboxDeliveryError, dead-letters and alerts:\n  "
        + "\n  ".join(new)
        + "\n\nRegister a consumer, or retire the producer. Do not add them to the "
        "allowlist."
    )


def test_orphan_allowlist_is_shrink_only() -> None:
    """A stale entry reserves permission for a defect that was already fixed."""
    runtime = _runtime_selectors()
    orphans = {
        selector
        for selector, contract in EVENT_CATALOGUE.items()
        if contract.declared_producers and selector not in runtime
    }
    stale = sorted(KNOWN_UNCONSUMED_PRODUCED_SELECTORS - orphans)
    assert not stale, (
        "these allowlist entries no longer match an orphan -- delete them so the "
        f"list keeps shrinking: {stale}"
    )


@pytest.mark.parametrize("selector", sorted(KNOWN_UNCONSUMED_PRODUCED_SELECTORS)[:5])
def test_allowlisted_selectors_are_real_catalogue_entries(selector: str) -> None:
    """Guards against a typo silently widening the allowlist.

    An entry that matches no catalogue key would be caught by the staleness test,
    but only in aggregate; this names the first few individually so a typo is
    obvious in the failure output.
    """
    assert selector in EVENT_CATALOGUE, f"{selector!r} is not a catalogue selector"
    assert EVENT_CATALOGUE[selector].declared_producers, (
        f"{selector!r} is allowlisted as produced-but-unconsumed yet declares no producer"
    )


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
