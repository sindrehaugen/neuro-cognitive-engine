"""Batch 72 — NetBox context enrichment for the Diagnostics Engine.

Resolves a single device's physical/organisational NetBox context (slug, site,
location, room, tenant) from either a device *slug* or a hardware *serial*.

The enrichment seam is the bridge between the diagnostics pipeline core
(Batch 70 log profiles, Batch 71 streaming) and NetBox's inventory: once a log
stream has been attributed to a device, callers use
``resolve_device_context`` to attach the "where does this live / who owns it"
metadata before anomalies are surfaced.

Network access is delegated entirely to the existing
``NetBoxGraphQLClient.execute_query`` (see
``nce/vertical_modules/netbox/graphql_activation.py``).  This module opens no
HTTP sessions of its own and adds no dependencies.

A missing device is **non-fatal**: the requested ``device_slug`` is echoed back,
every other field is ``None``, and ``resolved`` is ``False``.  This lets the
diagnostics pipeline keep streaming for devices that NetBox has not yet been
told about, rather than aborting the whole run.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("nce.vertical_modules.diagnostics.enrichment")

# GraphQL lookup for a device's organisational/physical context.
#
# NetBox's ``device_list`` accepts a ``filters`` argument; we resolve by slug
# *or* serial in a single round-trip and select the first match.  ``location``
# carries NetBox's rack-group / room hierarchy, so we expose it both as the
# raw location name (``location``) and, when present, as ``room`` — keeping the
# diagnostics vocabulary ("which room is this AV device in?") aligned with the
# field-deployment mental model.
DEVICE_CONTEXT_QUERY = """
query ResolveDeviceContext($slug: [String!], $serial: [String!]) {
  device_list(filters: {slug: $slug, serial: $serial}) {
    id
    name
    serial
    site {
      slug
      name
    }
    location {
      slug
      name
    }
    tenant {
      slug
      name
    }
  }
}
"""


class _NetBoxClientLike(Protocol):
    """Structural type for the slice of ``NetBoxGraphQLClient`` we depend on.

    Only ``execute_query`` is required; using a ``Protocol`` keeps this module
    decoupled from the concrete client (and makes the unit tests' mock a
    first-class, type-checked stand-in rather than an ``Any`` escape hatch).
    """

    async def execute_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def _empty_context(device_slug: str | None) -> dict[str, Any]:
    """Build the non-fatal "not resolved" result.

    Echoes ``device_slug`` (which may itself be ``None`` when only a serial was
    supplied) and leaves the remaining fields ``None``.
    """
    return {
        "device_slug": device_slug,
        "site": None,
        "location": None,
        "room": None,
        "tenant": None,
        "resolved": False,
    }


def _name_of(node: Any) -> str | None:
    """Return the ``name`` of a nested NetBox object, defensively.

    NetBox returns ``null`` for unset relations (a device may have no location
    or tenant), so the related object is frequently ``None``.
    """
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str):
            return name
    return None


async def resolve_device_context(
    netbox_client: _NetBoxClientLike,
    *,
    slug: str | None = None,
    serial: str | None = None,
) -> dict[str, Any]:
    """Resolve a device's NetBox context from a *slug* or *serial*.

    Args:
        netbox_client: An existing NetBox GraphQL client exposing
            ``execute_query`` (e.g. ``NetBoxGraphQLClient``).  No new HTTP
            session is opened — the passed-in client is reused verbatim.
        slug: The device slug to resolve by, if known.
        serial: The hardware serial to resolve by, if known.

    Returns:
        A dict with keys ``device_slug``, ``site``, ``location``, ``room``,
        ``tenant`` and ``resolved``.  On a hit, ``device_slug`` is NetBox's
        canonical slug and ``resolved`` is ``True``.  On a miss (or when
        neither identifier is supplied), the requested ``slug`` is echoed back,
        the other fields are ``None`` and ``resolved`` is ``False``.

    A missing device is non-fatal: this function never raises for an unmatched
    lookup — it returns the not-resolved shape so the diagnostics pipeline can
    proceed for un-inventoried devices.
    """
    if slug is None and serial is None:
        log.debug("resolve_device_context called with neither slug nor serial")
        return _empty_context(None)

    # NetBox list filters take arrays; only send the identifiers we actually
    # have so a present-but-empty filter never widens the match.
    variables: dict[str, Any] = {}
    if slug is not None:
        variables["slug"] = [slug]
    if serial is not None:
        variables["serial"] = [serial]

    response = await netbox_client.execute_query(DEVICE_CONTEXT_QUERY, variables)

    data_payload = response.get("data") or {}
    devices = data_payload.get("device_list") or []
    if not devices:
        # Non-fatal: echo the requested slug, everything else None.
        return _empty_context(slug)

    device = devices[0]
    if not isinstance(device, dict):
        return _empty_context(slug)

    site_node = device.get("site")
    location_node = device.get("location")
    tenant_node = device.get("tenant")

    # NetBox's canonical slug is the device name; fall back to the requested
    # slug so we never hand back ``None`` for a resolved device.
    resolved_slug = device.get("name") or slug
    location_name = _name_of(location_node)

    return {
        "device_slug": resolved_slug,
        "site": _name_of(site_node),
        "location": location_name,
        # "room" mirrors the NetBox location for the diagnostics vocabulary;
        # NetBox has no distinct "room" primitive, so location *is* the room.
        "room": location_name,
        "tenant": _name_of(tenant_node),
        "resolved": True,
    }
