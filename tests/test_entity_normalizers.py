"""Unit tests for entity normalizers (pure functions, no DB).

Tests the normalization logic: casefolding, stripping, and alias mapping.
"""

from nce.entity_resolution import normalize


class TestNormalize:
    """Test the normalize() pure function."""

    def test_normalize_identical_case_variants(self) -> None:
        """Cisco, 'Cisco Systems', CISCO all normalize to the same value."""
        result_1 = normalize("Cisco Systems", "manufacturer")
        result_2 = normalize("CISCO", "manufacturer")
        result_3 = normalize("cisco", "manufacturer")

        # All should resolve to "cisco" (the alias target)
        assert result_1 == "cisco"
        assert result_2 == "cisco"
        assert result_3 == "cisco"

    def test_normalize_aliased_manufacturer(self) -> None:
        """'Cisco Systems' is aliased to 'cisco' in the config."""
        result = normalize("Cisco Systems", "manufacturer")
        assert result == "cisco"

    def test_normalize_hewlett_packard_variants(self) -> None:
        """HP variants normalize to 'hp'."""
        assert normalize("Hewlett Packard", "manufacturer") == "hp"
        assert normalize("HEWLETT PACKARD", "manufacturer") == "hp"
        assert normalize("hewlett-packard", "manufacturer") == "hp"

    def test_normalize_unknown_manufacturer_passes_through(self) -> None:
        """Unknown manufacturer values pass through (casefolded, stripped)."""
        result = normalize("Acme Corp", "manufacturer")
        assert result == "acme corp"

    def test_normalize_strips_whitespace(self) -> None:
        """Whitespace is stripped before lookup."""
        result = normalize("  Cisco Systems  ", "manufacturer")
        assert result == "cisco"

    def test_normalize_empty_string(self) -> None:
        """Empty string normalizes to empty string."""
        result = normalize("", "manufacturer")
        assert result == ""

    def test_normalize_whitespace_only(self) -> None:
        """Whitespace-only string normalizes to empty string (after strip)."""
        result = normalize("   ", "manufacturer")
        assert result == ""

    def test_normalize_with_mixed_case_unknown(self) -> None:
        """Mixed-case unknown values are casefolded."""
        result = normalize("My Brand Inc", "manufacturer")
        assert result == "my brand inc"


class TestNormalizeIdempotence:
    """Test that normalization is idempotent (pure function property)."""

    def test_double_normalize_is_idempotent(self) -> None:
        """Normalizing twice returns the same result as once."""
        value = "Cisco Systems"
        first = normalize(value, "manufacturer")
        second = normalize(first, "manufacturer")
        assert first == second

    def test_normalize_idempotent_on_unknown(self) -> None:
        """Unknown values are idempotent too."""
        value = "Some New Brand"
        first = normalize(value, "manufacturer")
        second = normalize(first, "manufacturer")
        assert first == second
