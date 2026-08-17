"""Unit tests for the C8 allow-list field redactor.

All tests are pure (no DB, no HTTP, no fixtures).
Covers:
  - Only allow-listed fields pass through for each surface.
  - Unlisted fields are dropped (omission-safety).
  - ``margin``/``cost``/``internal-status`` never leak on any surface,
    even when present on the node.
  - An unknown surface raises ``UnknownSurfaceError`` (never a passthrough).
  - An empty node returns an empty dict.
  - Fields present on the allow-list but absent from the node are not invented.
"""

import pytest

from nce.redaction import UnknownSurfaceError, project

# ---------------------------------------------------------------------------
# Shared test node — contains sensitive fields that must never leak
# ---------------------------------------------------------------------------

_NODE_WITH_SENSITIVE: dict = {
    "id": "node-abc-123",
    "node_type": "device",
    "label": "Core Switch A",
    "description": "Primary core switch",
    "category": "network",
    "manufacturer": "cisco",
    "model": "Catalyst 9300",
    "part_number": "C9300-48P",
    "serial_number": "FCW2234G0AB",
    "status": "active",
    "location": "Oslo DC",
    "site": "oslo",
    "rack": "A01",
    "unit": "42",
    "interface": "GigabitEthernet1/0/1",
    "ip_address": "10.0.1.1",
    "mac_address": "aa:bb:cc:dd:ee:ff",
    "firmware_version": "17.6.1",
    "hardware_version": "V02",
    "warranty_expiry": "2027-12-31",
    "install_date": "2022-01-15",
    "tags": ["core", "production"],
    "namespace_id": "ns-001",
    # Sensitive fields that must NEVER appear on any external surface:
    "margin": 0.35,
    "cost": 12500.00,
    "internal-status": "flagged-for-review",
    "dg_percent": 0.30,
    "supplier_cost": 8000.00,
}


# ---------------------------------------------------------------------------
# Partner surface
# ---------------------------------------------------------------------------


class TestPartnerSurface:
    """project(..., 'partner') tests."""

    def test_only_allow_listed_fields_returned(self) -> None:
        """Only fields from partner allow-list appear in output."""
        result = project(_NODE_WITH_SENSITIVE, "partner")
        # All returned keys must be from the allow-list
        from nce.redaction.redactor import _load_allow_list

        allowed = _load_allow_list("partner")
        for key in result:
            assert key in allowed, f"Non-allow-listed field leaked: {key!r}"

    def test_known_safe_fields_pass_through(self) -> None:
        """Fields that are on the partner allow-list and on the node are present."""
        result = project(_NODE_WITH_SENSITIVE, "partner")
        assert result["id"] == "node-abc-123"
        assert result["label"] == "Core Switch A"
        assert result["manufacturer"] == "cisco"
        assert result["status"] == "active"

    def test_margin_never_leaks(self) -> None:
        """``margin`` is not on the partner allow-list and must not appear."""
        result = project(_NODE_WITH_SENSITIVE, "partner")
        assert "margin" not in result

    def test_cost_never_leaks(self) -> None:
        """``cost`` is not on the partner allow-list and must not appear."""
        result = project(_NODE_WITH_SENSITIVE, "partner")
        assert "cost" not in result

    def test_internal_status_never_leaks(self) -> None:
        """``internal-status`` is not on the partner allow-list and must not appear."""
        result = project(_NODE_WITH_SENSITIVE, "partner")
        assert "internal-status" not in result

    def test_unlisted_field_dropped(self) -> None:
        """A novel field not on the allow-list is dropped (omission-safety)."""
        node = {**_NODE_WITH_SENSITIVE, "newly_added_secret_field": "sensitive"}
        result = project(node, "partner")
        assert "newly_added_secret_field" not in result

    def test_extra_sensitive_fields_dropped(self) -> None:
        """``dg_percent`` and ``supplier_cost`` are not on partner allow-list."""
        result = project(_NODE_WITH_SENSITIVE, "partner")
        assert "dg_percent" not in result
        assert "supplier_cost" not in result


# ---------------------------------------------------------------------------
# Public-quote surface
# ---------------------------------------------------------------------------


class TestPublicQuoteSurface:
    """project(..., 'public-quote') tests."""

    _QUOTE_NODE: dict = {
        "id": "quote-xyz-456",
        "node_type": "quote_line",
        "label": "Catalyst 9300 48P",
        "description": "PoE+ managed switch",
        "category": "network",
        "manufacturer": "cisco",
        "model": "Catalyst 9300",
        "part_number": "C9300-48P",
        "quantity": 2,
        "unit_price": 14500.00,
        "currency": "NOK",
        "lead_time_days": 10,
        "availability": "in-stock",
        "tags": ["quote-2024"],
        "namespace_id": "ns-001",
        # Sensitive:
        "margin": 0.35,
        "cost": 12500.00,
        "internal-status": "draft-review",
        "serial_number": "FCW2234G0AB",
    }

    def test_only_allow_listed_fields_returned(self) -> None:
        """Only fields from public-quote allow-list appear in output."""
        result = project(self._QUOTE_NODE, "public-quote")
        from nce.redaction.redactor import _load_allow_list

        allowed = _load_allow_list("public-quote")
        for key in result:
            assert key in allowed, f"Non-allow-listed field leaked: {key!r}"

    def test_known_safe_fields_pass_through(self) -> None:
        """Fields on the public-quote allow-list pass through."""
        result = project(self._QUOTE_NODE, "public-quote")
        assert result["id"] == "quote-xyz-456"
        assert result["unit_price"] == 14500.00
        assert result["quantity"] == 2
        assert result["currency"] == "NOK"

    def test_margin_never_leaks(self) -> None:
        """``margin`` must not appear on the public-quote surface."""
        result = project(self._QUOTE_NODE, "public-quote")
        assert "margin" not in result

    def test_cost_never_leaks(self) -> None:
        """``cost`` must not appear on the public-quote surface."""
        result = project(self._QUOTE_NODE, "public-quote")
        assert "cost" not in result

    def test_internal_status_never_leaks(self) -> None:
        """``internal-status`` must not appear on the public-quote surface."""
        result = project(self._QUOTE_NODE, "public-quote")
        assert "internal-status" not in result

    def test_serial_number_not_on_public_quote(self) -> None:
        """``serial_number`` is on partner list but not public-quote — must be dropped."""
        result = project(self._QUOTE_NODE, "public-quote")
        assert "serial_number" not in result

    def test_unlisted_field_dropped(self) -> None:
        """A field not on the allow-list is dropped (omission-safety)."""
        node = {**self._QUOTE_NODE, "internal_notes": "do not share"}
        result = project(node, "public-quote")
        assert "internal_notes" not in result


# ---------------------------------------------------------------------------
# Cross-surface: sensitive fields never leak regardless of surface
# ---------------------------------------------------------------------------


class TestSensitiveFieldsNeverLeak:
    """Security invariant: margin/cost/internal-status never on any surface."""

    _SENSITIVE_NODE: dict = {
        "id": "node-001",
        "label": "test",
        "margin": 0.4,
        "cost": 9999.99,
        "internal-status": "confidential",
    }

    @pytest.mark.parametrize("surface", ["partner", "public-quote"])
    def test_margin_never_leaks_on_any_surface(self, surface: str) -> None:
        result = project(self._SENSITIVE_NODE, surface)
        assert "margin" not in result

    @pytest.mark.parametrize("surface", ["partner", "public-quote"])
    def test_cost_never_leaks_on_any_surface(self, surface: str) -> None:
        result = project(self._SENSITIVE_NODE, surface)
        assert "cost" not in result

    @pytest.mark.parametrize("surface", ["partner", "public-quote"])
    def test_internal_status_never_leaks_on_any_surface(self, surface: str) -> None:
        result = project(self._SENSITIVE_NODE, surface)
        assert "internal-status" not in result


# ---------------------------------------------------------------------------
# Omission-safety
# ---------------------------------------------------------------------------


class TestOmissionSafety:
    """New / unknown fields are hidden by default."""

    @pytest.mark.parametrize("surface", ["partner", "public-quote"])
    def test_novel_field_is_dropped(self, surface: str) -> None:
        """A field added to a node but not to any allow-list is dropped."""
        node = {"id": "n1", "label": "x", "future_secret_field": "secret"}
        result = project(node, surface)
        assert "future_secret_field" not in result

    @pytest.mark.parametrize("surface", ["partner", "public-quote"])
    def test_empty_node_returns_empty_dict(self, surface: str) -> None:
        """Empty input node → empty output dict."""
        result = project({}, surface)
        assert result == {}

    @pytest.mark.parametrize("surface", ["partner", "public-quote"])
    def test_allow_listed_field_absent_from_node_not_invented(self, surface: str) -> None:
        """A field on the allow-list that is absent from the node is not fabricated."""
        node = {"id": "n1"}
        result = project(node, surface)
        # Only "id" should be present; no extra keys invented
        assert list(result.keys()) == ["id"]


# ---------------------------------------------------------------------------
# Unknown surface
# ---------------------------------------------------------------------------


class TestUnknownSurface:
    """Unknown surface must raise, never pass through."""

    def test_unknown_surface_raises(self) -> None:
        """An unrecognised surface raises ``UnknownSurfaceError``."""
        with pytest.raises(UnknownSurfaceError):
            project({"id": "n1", "label": "x"}, "nonexistent-surface")

    def test_unknown_surface_does_not_return_full_node(self) -> None:
        """Confirm an unrecognised surface never silently passes all fields through."""
        with pytest.raises(UnknownSurfaceError):
            project({"margin": 1.0, "cost": 999.0}, "admin-internal")
