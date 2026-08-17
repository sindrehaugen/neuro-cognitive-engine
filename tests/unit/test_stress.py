"""
tests/unit/test_stress.py
=========================
Unit tests for the Longitudinal Operator Stress Tracking system (StressTracker).
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from tests.fixtures.mock_db import MockConnection

from nce.analytics.stress import StressTracker
from nce.signing import SigningKeyDecryptionError, require_master_key


@pytest.mark.anyio
class TestStressTracker:
    @pytest.fixture(autouse=True)
    def setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure a valid 32-byte master key is present in environment
        monkeypatch.setenv("NCE_MASTER_KEY", "x" * 32)

    async def test_get_raw_empathic_data_parsing(self) -> None:
        ns = uuid.uuid4()
        now = datetime.datetime.now(datetime.timezone.utc)

        # Mock database results with varying formats of empathic_tensor (string and list)
        fetch_results = [
            {
                "empathic_tensor": "[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]",
                "created_at": now,
                "tlx_scores": '{"mental": 50}',
                "vad_scores": '{"valence": 0.5}',
            },
            {
                "empathic_tensor": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
                "created_at": now,
                "tlx_scores": {"mental": 60},
                "vad_scores": {"valence": 0.6},
            },
        ]
        conn = MockConnection(fetch_results)
        tracker = StressTracker(conn)

        records = await tracker.get_raw_empathic_data(ns)
        assert len(records) == 2
        assert records[0]["empathic_tensor"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        assert records[0]["tlx_scores"] == {"mental": 50}
        assert records[1]["empathic_tensor"] == [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
        assert records[1]["tlx_scores"] == {"mental": 60}

    async def test_burnout_alert_threshold_trigger(self) -> None:
        ns = uuid.uuid4()
        now = datetime.datetime.now(datetime.timezone.utc)

        # 1. Test case: 4 consecutive shifts > 7.0 frustration (No Alert)
        fetch_results_no_alert = [
            {
                "empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 8.0],
                "created_at": now,
                "tlx_scores": None,
                "vad_scores": None,
            }
            for _ in range(4)
        ]
        # plus one normal shift
        fetch_results_no_alert.append(
            {
                "empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 2.0],
                "created_at": now,
                "tlx_scores": None,
                "vad_scores": None,
            }
        )

        conn = MockConnection(fetch_results_no_alert)
        tracker = StressTracker(conn)
        with require_master_key() as mk:
            enc_report = await tracker.analyze_and_encrypt_stress(ns, mk)
            report = StressTracker.decrypt_report(enc_report, mk)
            assert report["burnout_alert"] is False
            assert len(report["frustration_trend"]) == 5

        # 2. Test case: 5 consecutive shifts > 7.0 frustration (Immediate Alert)
        fetch_results_alert = [
            {
                "empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 7.5],
                "created_at": now,
                "tlx_scores": None,
                "vad_scores": None,
            }
            for _ in range(5)
        ]
        conn = MockConnection(fetch_results_alert)
        tracker = StressTracker(conn)
        with require_master_key() as mk:
            enc_report = await tracker.analyze_and_encrypt_stress(ns, mk)
            report = StressTracker.decrypt_report(enc_report, mk)
            assert report["burnout_alert"] is True

    async def test_predictive_fatigue_exponential_smoothing(self) -> None:
        ns = uuid.uuid4()
        now = datetime.datetime.now(datetime.timezone.utc)

        fetch_results = [
            {
                "empathic_tensor": [1.0, 2.0, 3.0, 0.0, 0.0, 0.0],
                "created_at": now,
                "tlx_scores": None,
                "vad_scores": None,
            },
            {
                "empathic_tensor": [2.0, 4.0, 6.0, 0.0, 0.0, 0.0],
                "created_at": now,
                "tlx_scores": None,
                "vad_scores": None,
            },
        ]
        conn = MockConnection(fetch_results)
        tracker = StressTracker(conn)

        # Exponential smoothing with beta = 0.3
        # t=0: smoothed = [1.0, 2.0, 3.0]
        # t=1: smoothed = 0.3 * [2.0, 4.0, 6.0] + 0.7 * [1.0, 2.0, 3.0]
        #               = [1.3, 2.6, 3.9]
        with require_master_key() as mk:
            enc_report = await tracker.analyze_and_encrypt_stress(ns, mk, beta=0.3)
            report = StressTracker.decrypt_report(enc_report, mk)

            smoothed = report["smoothed_vad_trend"]
            assert len(smoothed) == 2
            assert smoothed[0] == [1.0, 2.0, 3.0]
            assert pytest.approx(smoothed[1]) == [1.3, 2.6, 3.9]

    async def test_cryptographic_field_level_encryption_integrity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = uuid.uuid4()
        now = datetime.datetime.now(datetime.timezone.utc)

        fetch_results = [
            {
                "empathic_tensor": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                "created_at": now,
                "tlx_scores": None,
                "vad_scores": None,
            }
        ]
        conn = MockConnection(fetch_results)
        tracker = StressTracker(conn)

        with require_master_key() as mk:
            enc_report = await tracker.analyze_and_encrypt_stress(ns, mk)

        # Confirm that the returned object is raw encrypted bytes
        assert isinstance(enc_report, bytes)
        assert b"valence" not in enc_report
        assert b"frustration" not in enc_report

        # Confirm decryption works with the correct master key
        with require_master_key() as mk:
            dec_report = StressTracker.decrypt_report(enc_report, mk)
            assert dec_report["frustration_trend"] == [6.0]

        # Confirm decryption fails for non-privileged modules lacking the correct key
        monkeypatch.setenv("NCE_MASTER_KEY", "y" * 32)
        with require_master_key() as wrong_mk:
            with pytest.raises(SigningKeyDecryptionError):
                StressTracker.decrypt_report(enc_report, wrong_mk)
