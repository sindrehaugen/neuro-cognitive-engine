"""
tests/test_settings_registry.py
===============================
Unit tests for Settings Registry (Batch 33).
Verifies registry completeness, correct types, reload classes, validation,
and production-locking guardrails.
"""

from __future__ import annotations

import pytest

from nce.settings_registry import REGISTRY


def test_registry_completeness():
    """Verify that the registry contains settings across ~22 sections and is populated."""
    assert len(REGISTRY) > 0
    sections = {meta.section for meta in REGISTRY.values()}
    # Verify we have seeded settings across ~22 sections
    assert len(sections) >= 20


def test_registry_metadata_contracts():
    """Verify that every entry follows key contracts and reload class boundaries."""
    for key, meta in REGISTRY.items():
        assert meta.key == key
        assert meta.section
        assert meta.type in {"str", "int", "float", "bool", "secret", "list"}
        assert meta.reload_class in {"HOT", "WARM", "COLD"}
        assert meta.validator is not None
        assert callable(meta.validator)
        # Secrets must be marked is_secret
        if meta.type == "secret":
            assert meta.is_secret is True
        # If marked is_secret, type should be secret
        if meta.is_secret:
            assert meta.type == "secret" or meta.key == "NCE_MASTER_KEY"


def test_prod_locked_keys_flagged():
    """Verify that guardrail and security settings are flagged as prod_locked."""
    expected_prod_locked = {
        "NCE_BYPASS_WORM",
        "NCE_BYPASS_RLS",
        "NCE_ADMIN_OVERRIDE",
        "NCE_LOAD_DOTENV",
        "NCE_ALLOW_ADMIN_DOTENV_PERSIST",
        "NCE_MASTER_KEY",
        "WEBHOOK_DEDUP_FAIL_OPEN",
    }
    for key in expected_prod_locked:
        assert key in REGISTRY, f"Expected guardrail key {key} was not found in REGISTRY."
        assert REGISTRY[key].prod_locked is True, f"Key {key} must be flagged as prod_locked."


@pytest.mark.parametrize(
    "key,valid_val,invalid_val",
    [
        ("MONGO_URI", "mongodb://localhost", 123),
        ("PG_MIN_POOL", 5, "five"),
        ("PG_MIN_POOL", 2, True),  # True is an int in Python but should fail custom validator
        ("MINIO_SECURE", True, "yes"),
        ("CONSOLIDATION_HALF_LIFE_DAYS", 15.5, "fifteen"),
        ("NCE_TOOLS_DISABLED", ["tool1", "tool2"], "tool1"),
        ("NCE_TOOLS_DISABLED", ["tool1", "tool2"], [123, "tool2"]),
    ],
)
def test_validators(key, valid_val, invalid_val):
    """Test that setting validators accept valid values and reject invalid inputs."""
    meta = REGISTRY[key]
    assert meta.validator(valid_val) is True, (
        f"Validator for {key} rejected valid value: {valid_val}"
    )
    assert meta.validator(invalid_val) is False, (
        f"Validator for {key} accepted invalid value: {invalid_val}"
    )


def test_validate_env():
    """Verify validate_env checks and reports malformed environment settings correctly."""
    from nce.settings_registry import validate_env

    # Valid scenario
    valid_env = {
        "PG_MIN_POOL": "5",
        "MINIO_SECURE": "true",
        "MONGO_URI": "mongodb://test",
    }
    errors = validate_env(valid_env)
    assert not errors

    # Invalid scenario
    invalid_env = {
        "PG_MIN_POOL": "not_an_int",
        "MINIO_SECURE": "invalid_bool",
        "MONGO_URI": "",  # Empty is forbidden by allow_empty=False
    }
    errors = validate_env(invalid_env)
    assert "PG_MIN_POOL" in errors
    assert "MINIO_SECURE" in errors
    assert "MONGO_URI" in errors


def test_auto_load_defaults():
    """Verify auto_load_defaults populates missing keys in target dictionary."""
    from nce.settings_registry import auto_load_defaults

    # Empty env dict should receive defaults
    mock_env = {}
    auto_load_defaults(mock_env)
    assert mock_env["PG_MIN_POOL"] == "1"
    assert mock_env["PG_MAX_POOL"] == "10"

    # Pre-existing values should NOT be overwritten by default
    mock_env = {"PG_MIN_POOL": "42"}
    auto_load_defaults(mock_env)
    assert mock_env["PG_MIN_POOL"] == "42"
    assert mock_env["PG_MAX_POOL"] == "10"

    # Pre-existing values SHOULD be overwritten if overwrite=True
    mock_env = {"PG_MIN_POOL": "42"}
    auto_load_defaults(mock_env, overwrite=True)
    assert mock_env["PG_MIN_POOL"] == "1"
