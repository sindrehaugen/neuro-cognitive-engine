"""tests/unit/test_principal_tier_hardening.py

Principal-tier-hardening wave. Positive controls for the fix in three
places, per INSTRUMENT_STANDARD.md (a check must ship with a committed
positive control proving it fires):

  1. nce.resource_surface.rest.resolve_principal_tier -- reads only
     request.state.namespace_ctx.principal_kind (the real attribute
     admin_app.py's HMACAuthMiddleware populates -- see nce/auth.py's
     _resolve_namespace_context), never a caller-supplied header;
     unresolved principals get the least-privileged fallback, not
     "employee".
  2. nce.resource_surface.rest.redact_item -- a tier absent from a spec's
     tier_allowlists is treated as an explicit empty allowlist (deny), not
     as unfiltered.
  3. nce.principal_bindings.service.extract_principal_identity -- tier is
     read only from request.state.namespace_ctx's verified principal_kind,
     never from a header or query param.

Each mechanism gets: a forged-header-does-not-elevate case, an
undeclared/unknown-tier-does-not-leak case, and a still-works case for the
legitimate, declared path -- the third is the one G's finding taught this
estate to always include, so an unconditional deny can't quietly pass the
suite.
"""

from __future__ import annotations

from uuid import uuid4

from starlette.requests import Request

from nce.auth import NamespaceContext
from nce.principal_bindings.service import extract_principal_identity
from nce.resource_surface.rest import redact_item, resolve_principal_tier
from nce.resource_surface.spec import ResourceSpec


def _make_request(
    *,
    state_principal_kind: str | None = None,
    header_tier: str | None = None,
    query_tier: str | None = None,
) -> Request:
    headers = []
    if header_tier is not None:
        headers.append((b"x-nce-principal-tier", header_tier.encode("latin-1")))
    query_string = f"tier={query_tier}".encode("latin-1") if query_tier is not None else b""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/probe",
        "headers": headers,
        "query_string": query_string,
    }
    req = Request(scope)
    if state_principal_kind is not None:
        req.state.namespace_ctx = NamespaceContext(principal_kind=state_principal_kind)
    return req


# ===========================================================================
# 1. resolve_principal_tier -- header trust removed, least-privilege fallback
# ===========================================================================


def test_resolve_principal_tier_forged_header_does_not_elevate_when_unverified():
    """No verified state at all: a caller-supplied header claiming 'employee'
    must not grant it -- the whole point of removing header trust."""
    req = _make_request(state_principal_kind=None, header_tier="employee")
    assert resolve_principal_tier(req) == "unverified"


def test_resolve_principal_tier_forged_header_does_not_override_verified_state():
    """A verified contractor whose request also carries a forged
    'employee' header must resolve to the verified tier, not the header."""
    req = _make_request(state_principal_kind="contractor", header_tier="employee")
    assert resolve_principal_tier(req) == "contractor"


def test_resolve_principal_tier_unrecognised_namespace_ctx_value_falls_to_least_privilege():
    """Defense in depth: NamespaceContext's own validator already normalises
    an unrecognised principal_kind to 'employee', so this exercises
    resolve_principal_tier's own guard directly against a stand-in object
    that skips that normalisation -- it must not trust an unrecognised
    value just because *something* is set on namespace_ctx."""

    class _FakeNamespaceCtx:
        principal_kind = "super-admin"

    req = _make_request()
    req.state.namespace_ctx = _FakeNamespaceCtx()
    assert resolve_principal_tier(req) == "unverified"


def test_resolve_principal_tier_no_state_no_header_is_least_privilege_not_employee():
    req = _make_request()
    assert resolve_principal_tier(req) == "unverified"


def test_resolve_principal_tier_valid_verified_employee_still_works():
    """The legitimate path must still work: this is not an unconditional deny."""
    req = _make_request(state_principal_kind="employee")
    assert resolve_principal_tier(req) == "employee"


def test_resolve_principal_tier_valid_verified_external_customer_still_works():
    req = _make_request(state_principal_kind="external-customer")
    assert resolve_principal_tier(req) == "external-customer"


# ===========================================================================
# 2. redact_item -- an undeclared tier denies, it does not unfilter
# ===========================================================================

_SPEC_WITH_ALLOWLIST = ResourceSpec(
    engine="probe",
    entity="widgets",
    node_type="PROBE_WIDGET",
    table_name="stock_locations",
    writable_fields=("name", "cost_price"),
    tier_allowlists={"contractor": ("id", "name")},
)

_SPEC_WITHOUT_ALLOWLIST = ResourceSpec(
    engine="probe",
    entity="gadgets",
    node_type="PROBE_GADGET",
    table_name="stock_locations",
    writable_fields=("name", "cost_price"),
)

_ITEM = {"id": "1", "name": "Widget", "cost_price": 42.0}


def test_redact_item_unknown_tier_sees_nothing_even_with_a_declared_allowlist():
    """A tier the spec never declared (here: 'unverified', the new fallback)
    must not fall through to 'unfiltered' just because the spec declares
    *some* other tier's allowlist."""
    out = redact_item(_ITEM, _SPEC_WITH_ALLOWLIST, "unverified")
    assert out == {}


def test_redact_item_spec_with_no_allowlist_at_all_leaks_nothing_to_a_real_tier():
    """The actual fixed vulnerability: a spec that never declared
    tier_allowlists used to mean 'unfiltered' for every non-employee tier.
    It must now mean 'nothing', for a real tier, not just the fallback."""
    out = redact_item(_ITEM, _SPEC_WITHOUT_ALLOWLIST, "contractor")
    assert out == {}


def test_redact_item_declared_tier_still_sees_its_allowed_fields():
    """The valid case must still pass, or an unconditional deny would
    satisfy this suite without actually implementing tier redaction."""
    out = redact_item(_ITEM, _SPEC_WITH_ALLOWLIST, "contractor")
    assert out == {"id": "1", "name": "Widget"}
    assert "cost_price" not in out


def test_redact_item_employee_always_sees_everything():
    out = redact_item(_ITEM, _SPEC_WITHOUT_ALLOWLIST, "employee")
    assert out == _ITEM


def test_redact_item_sensitive_substrings_still_stripped_for_declared_tier():
    """A field allowlisted by name but matching a sensitive substring (cost/
    margin/bid) stays stripped -- the allowlist fix must not regress this
    older, independent guard."""
    spec = ResourceSpec(
        engine="probe",
        entity="parts",
        node_type="PROBE_PART",
        table_name="stock_locations",
        writable_fields=("name", "cost_price"),
        tier_allowlists={"contractor": ("id", "name", "cost_price")},
    )
    out = redact_item(_ITEM, spec, "contractor")
    assert out == {"id": "1", "name": "Widget"}


# ===========================================================================
# 3. extract_principal_identity -- header/query tier can never override state
# ===========================================================================


def _make_binding_request(
    *,
    ns_ctx_principal_kind: str | None,
    header_tier: str | None = None,
    query_tier: str | None = None,
) -> Request:
    headers = []
    if header_tier is not None:
        headers.append((b"x-nce-principal-tier", header_tier.encode("latin-1")))
    query_string = f"tier={query_tier}".encode("latin-1") if query_tier is not None else b""
    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/api/probe",
        "headers": headers,
        "query_string": query_string,
    }
    req = Request(scope)
    if ns_ctx_principal_kind is not None:
        req.state.namespace_ctx = NamespaceContext(
            namespace_id=uuid4(),
            agent_id="probe-agent",
            principal_kind=ns_ctx_principal_kind,
        )
    return req


def test_extract_principal_identity_forged_header_does_not_override_verified_tier():
    req = _make_binding_request(ns_ctx_principal_kind="contractor", header_tier="employee")
    _, _, tier = extract_principal_identity(req)
    assert tier == "contractor"


def test_extract_principal_identity_forged_query_param_does_not_override_verified_tier():
    req = _make_binding_request(ns_ctx_principal_kind="external-customer", query_tier="employee")
    _, _, tier = extract_principal_identity(req)
    assert tier == "external-customer"


def test_extract_principal_identity_verified_employee_still_works():
    req = _make_binding_request(ns_ctx_principal_kind="employee")
    _, _, tier = extract_principal_identity(req)
    assert tier == "employee"
