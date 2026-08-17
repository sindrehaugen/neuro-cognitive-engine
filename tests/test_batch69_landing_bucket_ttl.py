"""
Batch 69 — landing-bucket-ttl
Unit tests for nce.storage.ensure_landing_bucket.

All tests are pure-unit: no Docker, no network, no real MinIO.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from minio import Minio
from minio.commonconfig import ENABLED
from minio.lifecycleconfig import (
    AbortIncompleteMultipartUpload,
    Expiration,
    LifecycleConfig,
    Rule,
)

from nce.storage import ensure_landing_bucket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(*, exists: bool) -> MagicMock:
    """Return a spec'd Minio mock with bucket_exists pre-configured."""
    client = MagicMock(spec=Minio)
    client.bucket_exists.return_value = exists
    return client


def _expected_lifecycle(ttl_days: int) -> LifecycleConfig:
    return LifecycleConfig(
        [
            Rule(
                ENABLED,
                rule_id="diag-landing-expire",
                expiration=Expiration(days=ttl_days),
                abort_incomplete_multipart_upload=AbortIncompleteMultipartUpload(
                    days_after_initiation=ttl_days,
                ),
            )
        ]
    )


# ---------------------------------------------------------------------------
# Core contract tests
# ---------------------------------------------------------------------------


def test_make_bucket_called_when_bucket_does_not_exist() -> None:
    """make_bucket is invoked when bucket_exists returns False."""
    client = _make_client(exists=False)
    ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)

    client.bucket_exists.assert_called_once_with("test-landing")
    client.make_bucket.assert_called_once_with("test-landing")


def test_make_bucket_skipped_when_bucket_already_exists() -> None:
    """make_bucket must NOT be called when bucket_exists returns True."""
    client = _make_client(exists=True)
    ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)

    client.bucket_exists.assert_called_once_with("test-landing")
    client.make_bucket.assert_not_called()


def test_set_bucket_lifecycle_always_called() -> None:
    """set_bucket_lifecycle is invoked regardless of whether the bucket existed."""
    for exists in (True, False):
        client = _make_client(exists=exists)
        ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)
        client.set_bucket_lifecycle.assert_called_once()


def test_lifecycle_uses_configured_ttl_days() -> None:
    """The lifecycle policy encodes the configured TTL (7 days by default)."""
    client = _make_client(exists=True)
    ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)

    client.set_bucket_lifecycle.assert_called_once()
    _, lifecycle_arg = client.set_bucket_lifecycle.call_args[0]

    assert len(lifecycle_arg.rules) == 1
    rule = lifecycle_arg.rules[0]
    assert rule.expiration.days == 7
    assert rule.abort_incomplete_multipart_upload.days_after_initiation == 7


def test_lifecycle_respects_custom_ttl_days() -> None:
    """A non-default TTL (e.g. 14 days) is passed through correctly."""
    client = _make_client(exists=True)
    ensure_landing_bucket(client, bucket="test-landing", ttl_days=14)

    _, lifecycle_arg = client.set_bucket_lifecycle.call_args[0]
    rule = lifecycle_arg.rules[0]
    assert rule.expiration.days == 14
    assert rule.abort_incomplete_multipart_upload.days_after_initiation == 14


def test_lifecycle_rule_id_and_status() -> None:
    """The lifecycle rule carries the expected rule_id and ENABLED status."""
    client = _make_client(exists=True)
    ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)

    _, lifecycle_arg = client.set_bucket_lifecycle.call_args[0]
    rule = lifecycle_arg.rules[0]
    assert rule.status == ENABLED
    assert rule.rule_id == "diag-landing-expire"


def test_bucket_name_passed_to_set_lifecycle() -> None:
    """set_bucket_lifecycle receives the correct bucket name as first positional arg."""
    client = _make_client(exists=True)
    ensure_landing_bucket(client, bucket="my-custom-bucket", ttl_days=7)

    bucket_name_arg, _ = client.set_bucket_lifecycle.call_args[0]
    assert bucket_name_arg == "my-custom-bucket"


# ---------------------------------------------------------------------------
# Default-from-cfg tests
# ---------------------------------------------------------------------------


def test_defaults_read_from_cfg() -> None:
    """When bucket/ttl_days are omitted, values come from cfg."""
    client = _make_client(exists=True)

    with patch("nce.storage.cfg") as mock_cfg:
        mock_cfg.NCE_DIAG_LANDING_BUCKET = "cfg-bucket"
        mock_cfg.NCE_DIAG_LANDING_TTL_DAYS = 3

        ensure_landing_bucket(client)

    client.bucket_exists.assert_called_once_with("cfg-bucket")
    bucket_name_arg, lifecycle_arg = client.set_bucket_lifecycle.call_args[0]
    assert bucket_name_arg == "cfg-bucket"
    assert lifecycle_arg.rules[0].expiration.days == 3


# ---------------------------------------------------------------------------
# Error-swallowing tests
# ---------------------------------------------------------------------------


def test_already_exists_error_swallowed() -> None:
    """An 'already exists' exception from make_bucket is swallowed gracefully."""
    client = _make_client(exists=False)
    client.make_bucket.side_effect = Exception("BucketAlreadyOwnedByYou")

    # Should not raise — lifecycle still applied
    ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)
    client.set_bucket_lifecycle.assert_called_once()


def test_unexpected_make_bucket_error_propagated() -> None:
    """An unexpected exception from make_bucket is re-raised."""
    client = _make_client(exists=False)
    client.make_bucket.side_effect = Exception("network timeout")

    with pytest.raises(Exception, match="network timeout"):
        ensure_landing_bucket(client, bucket="test-landing", ttl_days=7)
