"""Tests for dead_letter_queue payload sanitisation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.dead_letter_queue import _sanitize_dlq_kwargs, store_dead_letter


class TestSanitizeDlqKwargs:
    def test_passes_through_simple_values(self):
        assert _sanitize_dlq_kwargs("hello") == "hello"
        assert _sanitize_dlq_kwargs(42) == 42
        assert _sanitize_dlq_kwargs(None) is None

    def test_redacts_sensitive_keys(self):
        payload = {
            "provider": "sharepoint",
            "access_token": "super_secret_token",
            "refresh_token": "another_secret",
            "password": "hunter2",
            "api_key": "sk-12345",
        }
        out = _sanitize_dlq_kwargs(payload)
        assert out["provider"] == "sharepoint"
        assert out["access_token"] == "[REDACTED]"
        assert out["refresh_token"] == "[REDACTED]"
        assert out["password"] == "[REDACTED]"
        assert out["api_key"] == "[REDACTED]"

    def test_truncates_long_strings(self):
        long_str = "x" * 5000
        out = _sanitize_dlq_kwargs({"data": long_str})
        assert len(out["data"]) == 4110  # 4096 + len('...[truncated]')
        assert out["data"].endswith("...[truncated]")

    def test_limits_nested_dict_keys(self):
        big = {f"key_{i}": i for i in range(100)}
        out = _sanitize_dlq_kwargs(big)
        assert len(out) == 50

    def test_limits_list_length(self):
        big_list = [{"id": i} for i in range(100)]
        out = _sanitize_dlq_kwargs({"items": big_list})
        assert len(out["items"]) == 50

    def test_recursive_dict_redaction(self):
        nested = {
            "outer": {
                "access_token": "nested_secret",
                " innocent": "keep_me",
            }
        }
        out = _sanitize_dlq_kwargs(nested)
        assert out["outer"]["access_token"] == "[REDACTED]"
        assert out["outer"][" innocent"] == "keep_me"

    def test_string_in_list_truncated(self):
        long_str = "y" * 5000
        out = _sanitize_dlq_kwargs({"items": [long_str]})
        assert out["items"][0].endswith("...[truncated]")


class TestDeadLetterQueueAlerts:
    @pytest.mark.asyncio
    async def test_store_dead_letter_triggers_alert(self):
        # Arrange
        mock_conn = AsyncMock()
        # First return value is the DLQ ID, second is the count for _refresh_backlog_gauge
        mock_conn.fetchval.side_effect = ["mock-dlq-id", 5]

        mock_pool = MagicMock()
        mock_acquire_cm = MagicMock()
        mock_acquire_cm.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_acquire_cm

        task_name = "test_indexing_task"
        job_id = "job-123"
        kwargs = {"namespace_id": "ns-456", "foo": "bar"}
        error_message = "Test error occurred"
        attempt_count = 5

        # Mock NotificationDispatcher.dispatch_alert
        with patch(
            "nce.notifications.dispatcher.dispatch_alert", new_callable=AsyncMock
        ) as mock_dispatch_alert:
            # Act
            dlq_id = await store_dead_letter(
                pg_pool=mock_pool,
                task_name=task_name,
                job_id=job_id,
                kwargs=kwargs,
                error_message=error_message,
                attempt_count=attempt_count,
            )

            # Assert
            assert dlq_id == "mock-dlq-id"
            mock_dispatch_alert.assert_called_once()
            args, _ = mock_dispatch_alert.call_args
            title, message = args
            assert task_name in title
            assert task_name in message
            assert job_id in message
            assert error_message in message

    @pytest.mark.asyncio
    async def test_store_dead_letter_alert_failure_does_not_raise(self):
        # Arrange
        mock_conn = AsyncMock()
        mock_conn.fetchval.side_effect = ["mock-dlq-id", 5]

        mock_pool = MagicMock()
        mock_acquire_cm = MagicMock()
        mock_acquire_cm.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_acquire_cm

        task_name = "test_indexing_task"
        job_id = "job-123"
        kwargs = {"foo": "bar"}
        error_message = "Test error occurred"
        attempt_count = 5

        with patch(
            "nce.notifications.dispatcher.dispatch_alert", side_effect=RuntimeError("alert failed")
        ):
            # Act & Assert (should NOT raise RuntimeError)
            dlq_id = await store_dead_letter(
                pg_pool=mock_pool,
                task_name=task_name,
                job_id=job_id,
                kwargs=kwargs,
                error_message=error_message,
                attempt_count=attempt_count,
            )
            assert dlq_id == "mock-dlq-id"
