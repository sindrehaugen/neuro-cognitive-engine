"""
Tests for ``PIIEntity.__repr__`` — ensures raw PII values never leak in
string representations before or after ``clear_raw_value()``.

Verification strategy:
  - Fresh entity with raw PII → ``repr()`` shows ``<present>`` not the raw value.
  - After ``clear_raw_value()`` → ``repr()`` shows ``[REDACTED]``.
  - Raw value never appears in any string representation at any lifecycle stage.
"""

from __future__ import annotations

from nce.models import PIIEntity


class TestPIIEntityRepr:
    """PIIEntity string representation safety tests."""

    def test_fresh_entity_repr_shows_present_not_raw(self):
        """Raw PII value is never exposed in repr of a fresh entity."""
        entity = PIIEntity(
            start=10,
            end=25,
            entity_type="EMAIL",
            value="victim@example.com",
            score=0.95,
        )
        rep = repr(entity)
        # Must NOT contain the raw value
        assert "victim@example.com" not in rep, f"Raw PII leaked in repr: {rep}"
        # Must indicate value is present
        assert "<present>" in rep, f"Should indicate present value: {rep}"
        # Must NOT contain [REDACTED] (not cleared yet)
        assert "[REDACTED]" not in rep, f"Should not show redacted before clear: {rep}"

    def test_cleared_entity_repr_shows_redacted(self):
        """After clear_raw_value, repr shows [REDACTED]."""
        entity = PIIEntity(
            start=5,
            end=18,
            entity_type="PHONE",
            value="+1-800-555-0199",
            score=0.88,
        )
        entity.clear_raw_value()
        rep = repr(entity)
        # Must NOT contain the raw value
        assert "+1-800-555-0199" not in rep, f"Raw PII leaked after clear: {rep}"
        # Must show [REDACTED]
        assert "[REDACTED]" in rep, f"Should show redacted after clear: {rep}"
        # Must NOT contain <present>
        assert "<present>" not in rep, f"Should not show present after clear: {rep}"

    def test_entity_with_token_repr_includes_token(self):
        """repr includes token when set."""
        entity = PIIEntity(
            start=0,
            end=9,
            entity_type="CREDIT_CARD",
            value="4111-1111-1111-1111",
            score=0.99,
            token="<CREDIT_CARD_abc123>",
        )
        entity.clear_raw_value()
        rep = repr(entity)
        assert "4111-1111-1111-1111" not in rep, f"Raw CC leaked in repr: {rep}"
        assert "[REDACTED]" in rep
        assert "<CREDIT_CARD_abc123>" in rep, f"Token missing from repr: {rep}"
        assert "<present>" not in rep

    def test_repr_after_clear_is_idempotent(self):
        """Calling clear_raw_value multiple times is safe."""
        entity = PIIEntity(
            start=0,
            end=5,
            entity_type="EMAIL",
            value="spam@me.com",
            score=0.7,
        )
        entity.clear_raw_value()
        first_rep = repr(entity)
        entity.clear_raw_value()  # Second call
        second_rep = repr(entity)
        assert first_rep == second_rep, "Repr should be stable after repeated clear"
        assert "spam@me.com" not in first_rep

    def test_model_dump_of_cleared_entity_shows_redacted(self):
        """model_dump() after clear returns [REDACTED] for value."""
        entity = PIIEntity(
            start=0,
            end=5,
            entity_type="EMAIL",
            value="raw@example.com",
            score=0.5,
        )
        entity.clear_raw_value()
        dumped = entity.model_dump()
        assert dumped["value"] == "[REDACTED]", f"model_dump leaked: {dumped}"
        assert "raw@example.com" not in str(dumped)
