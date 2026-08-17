"""Tests for nce.http_resilience — tenacity-backed outbound HTTP retries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import nce.http_resilience as hr


async def _run_operation_without_retry(op, **_kw):
    return await op()


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://example.com/")


def _response(
    status: int,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or {},
        content=content,
        request=_request(),
    )


class TestRetryConfig:
    @pytest.mark.asyncio
    async def test_negative_max_retries_raises(self):
        async def op():
            return "ok"

        with pytest.raises(ValueError, match="max_retries"):
            await hr.execute_http_with_retry(op, max_retries=-1)

    @pytest.mark.asyncio
    async def test_zero_base_delay_raises(self):
        async def op():
            return "ok"

        with pytest.raises(ValueError, match="base_delay_ms"):
            await hr.execute_http_with_retry(op, base_delay_ms=0)

    @pytest.mark.asyncio
    async def test_max_delay_less_than_base_raises(self):
        async def op():
            return "ok"

        with pytest.raises(ValueError, match="max_delay_ms"):
            await hr.execute_http_with_retry(op, base_delay_ms=500, max_delay_ms=100)

    @pytest.mark.asyncio
    async def test_backoff_factor_below_one_raises(self):
        async def op():
            return "ok"

        with pytest.raises(ValueError, match="backoff_factor"):
            await hr.execute_http_with_retry(op, backoff_factor=0.5)

    @pytest.mark.asyncio
    async def test_operation_name_too_long_raises(self):
        async def op():
            return "ok"

        with pytest.raises(ValueError, match="operation_name"):
            await hr.execute_http_with_retry(op, operation_name="x" * 129)


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_transient_error_retries_then_succeeds(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise hr.ExternalAPITransientError("temporary", operation="t")
            return "ok"

        out = await hr.execute_http_with_retry(
            op,
            operation_name="t",
            max_retries=3,
            base_delay_ms=1,
            max_delay_ms=50,
            max_total_ms=5000,
        )
        assert out == "ok"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_client_error_not_retried(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            raise hr.ExternalAPIClientError("bad request", operation="t", status_code=400)

        with pytest.raises(hr.ExternalAPIClientError):
            await hr.execute_http_with_retry(
                op,
                operation_name="t",
                max_retries=3,
                base_delay_ms=1,
            )
        assert calls == 1

    @pytest.mark.asyncio
    async def test_max_retries_3_means_4_total_attempts(self):
        calls = 0

        async def op():
            nonlocal calls
            calls += 1
            raise hr.ExternalAPITransientError("always", operation="t")

        with pytest.raises(hr.ExternalAPIRetriesExhaustedError):
            await hr.execute_http_with_retry(
                op,
                operation_name="t",
                max_retries=3,
                base_delay_ms=1,
                max_delay_ms=20,
                max_total_ms=5000,
            )
        assert calls == 4

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises_ExternalAPIRetriesExhaustedError(self):
        async def op():
            raise hr.ExternalAPITransientError("always", operation="t")

        with pytest.raises(hr.ExternalAPIRetriesExhaustedError) as ei:
            await hr.execute_http_with_retry(
                op,
                operation_name="t",
                max_retries=3,
                base_delay_ms=1,
                max_delay_ms=20,
                max_total_ms=5000,
            )
        assert isinstance(ei.value, hr.ExternalAPIRetriesExhaustedError)
        assert ei.value.attempts == 4


class TestRetryAfterParsing:
    def test_integer_seconds_parsed(self):
        assert hr._parse_retry_after("30") == 30

    def test_zero_clamped_to_zero(self):
        assert hr._parse_retry_after("0") == 0

    def test_negative_clamped_to_zero(self):
        assert hr._parse_retry_after("-5") == 0

    def test_http_date_parsed(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        rfc2822 = format_datetime(future, usegmt=True)
        parsed = hr._parse_retry_after(rfc2822)
        assert parsed is not None
        assert 55 <= parsed <= 65

    def test_invalid_string_returns_none(self):
        assert hr._parse_retry_after("not-a-date") is None

    def test_none_input_returns_none(self):
        assert hr._parse_retry_after(None) is None


class TestClassifyHttpxResponse:
    def test_429_raises_transient_with_retry_after(self):
        resp = _response(429, headers={"retry-after": "45"})
        with pytest.raises(hr.ExternalAPITransientError) as ei:
            hr.classify_httpx_response(resp, operation="op")
        assert ei.value.status_code == 429
        assert ei.value.retry_after_s == 45

    def test_500_raises_transient(self):
        resp = _response(500)
        with pytest.raises(hr.ExternalAPITransientError) as ei:
            hr.classify_httpx_response(resp, operation="op")
        assert ei.value.status_code == 500

    def test_404_raises_client_error(self):
        resp = _response(404)
        with pytest.raises(hr.ExternalAPIClientError) as ei:
            hr.classify_httpx_response(resp, operation="op")
        assert ei.value.status_code == 404

    def test_200_does_not_raise(self):
        resp = _response(200)
        assert hr.classify_httpx_response(resp, operation="op") is None

    def test_204_does_not_raise(self):
        resp = _response(204)
        assert hr.classify_httpx_response(resp, operation="op") is None


class TestOauthTokenPostForm:
    @pytest.mark.asyncio
    async def test_successful_response_returns_json(self):
        mock_resp = _response(200, content=b'{"access_token": "tok"}')
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            out = await hr.oauth_token_post_form(
                "https://auth.example/token",
                {"grant_type": "client_credentials"},
                operation="oauth:test",
            )
        assert out == {"access_token": "tok"}

    @pytest.mark.asyncio
    async def test_content_type_header_set(self):
        mock_resp = _response(200, content=b'{"access_token": "tok"}')
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            await hr.oauth_token_post_form(
                "https://auth.example/token",
                {"grant_type": "client_credentials"},
                operation="oauth:test",
            )
        hdrs = mock_client.post.call_args.kwargs["headers"]
        assert hdrs["Content-Type"] == "application/x-www-form-urlencoded"

    @pytest.mark.asyncio
    async def test_caller_content_type_not_overridden(self):
        mock_resp = _response(200, content=b'{"access_token": "tok"}')
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            await hr.oauth_token_post_form(
                "https://auth.example/token",
                {"grant_type": "client_credentials"},
                operation="oauth:test",
                headers={"Content-Type": "application/json"},
            )
        hdrs = mock_client.post.call_args.kwargs["headers"]
        assert hdrs["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_non_json_response_raises_client_error(self):
        mock_resp = _response(200, content=b"not json")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(hr.ExternalAPIClientError):
                await hr.oauth_token_post_form(
                    "https://auth.example/token",
                    {"grant_type": "client_credentials"},
                    operation="oauth:test",
                )

    @pytest.mark.asyncio
    async def test_client_reused_across_retries(self):
        post_calls = 0
        ok_resp = _response(200, content=b'{"access_token": "tok"}')

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        async def post(*_args, **_kwargs):
            nonlocal post_calls
            post_calls += 1
            if post_calls < 3:
                raise httpx.TimeoutException("timed out")
            return ok_resp

        mock_client.post = post

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            out = await hr.oauth_token_post_form(
                "https://auth.example/token",
                {"grant_type": "client_credentials"},
                operation="oauth:test",
            )

        assert out == {"access_token": "tok"}
        assert mock_client.__aenter__.call_count == 1
        assert post_calls == 3


class TestSecretRedaction:
    def test_response_body_access_token_not_in_error_message(self):
        """Documents redaction on error tails; DSN-style creds are scrubbed."""
        resp = _response(
            401,
            content=b"failed: postgresql://user:supersecret@host/db",
        )
        with pytest.raises(hr.ExternalAPIClientError) as ei:
            hr.classify_httpx_response(resp, operation="op")
        assert "supersecret" not in str(ei.value)

    @pytest.mark.asyncio
    async def test_dsn_in_transport_error_is_redacted_in_oauth_form(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.RequestError(
                "transport: postgresql://user:pass@host/db",
                request=_request(),
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client),
            patch(
                "nce.http_resilience.redact_secrets_in_text",
                return_value="REDACTED",
            ),
            patch.object(
                hr,
                "execute_http_with_retry",
                new=_run_operation_without_retry,
            ),
        ):
            with pytest.raises(hr.ExternalAPITransientError) as ei:
                await hr.oauth_token_post_form(
                    "https://auth.example/token",
                    {"grant_type": "client_credentials"},
                    operation="oauth:test",
                )
        assert "REDACTED" in str(ei.value)
        assert "postgresql://user:pass@host/db" not in str(ei.value)


class TestPostJsonWithRetry:
    @pytest.mark.asyncio
    async def test_successful_response_returns_json(self):
        mock_resp = _response(200, content=b'{"id": "sub123"}')
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            out = await hr.post_json_with_retry(
                "https://api.example/subscriptions",
                {"changeType": "updated"},
                operation="setup_webhook:test",
            )
        assert out == {"id": "sub123"}

    @pytest.mark.asyncio
    async def test_content_type_header_set(self):
        mock_resp = _response(200, content=b'{"id": "sub123"}')
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nce.http_resilience.httpx.AsyncClient", return_value=mock_client):
            await hr.post_json_with_retry(
                "https://api.example/subscriptions",
                {"changeType": "updated"},
                operation="setup_webhook:test",
            )
        hdrs = mock_client.post.call_args.kwargs["headers"]
        assert hdrs["Content-Type"] == "application/json"


class TestRequestWithRetry:
    @pytest.mark.asyncio
    async def test_transient_503_retries_and_succeeds(self):
        calls = 0

        async def mock_request(method, url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                return _response(503)
            return _response(200, content=b"success")

        client = AsyncMock()
        client.request = mock_request

        resp = await hr.request_with_retry(
            client,
            "GET",
            "https://example.com/api",
            operation_name="test_transient",
            base_delay_ms=1,
            max_delay_ms=5,
            max_total_ms=1000,
        )
        assert resp.status_code == 200
        assert resp.content == b"success"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_sustained_outage_fails(self):
        async def mock_request(method, url, **kwargs):
            return _response(503)

        client = AsyncMock()
        client.request = mock_request

        with pytest.raises(hr.ExternalAPIRetriesExhaustedError):
            await hr.request_with_retry(
                client,
                "GET",
                "https://example.com/api",
                operation_name="test_sustained",
                max_retries=2,
                base_delay_ms=1,
                max_delay_ms=5,
                max_total_ms=1000,
            )


class TestRequestWithRetrySync:
    def test_transient_503_retries_and_succeeds_sync(self):
        calls = 0
        from unittest.mock import MagicMock

        def mock_request(method, url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                return _response(503)
            return _response(200, content=b"success")

        client = MagicMock()
        client.request = mock_request

        resp = hr.request_with_retry_sync(
            client,
            "GET",
            "https://example.com/api",
            operation_name="test_transient_sync",
            base_delay_ms=1,
            max_delay_ms=5,
            max_total_ms=1000,
        )
        assert resp.status_code == 200
        assert resp.content == b"success"
        assert calls == 3

    def test_sustained_outage_fails_sync(self):
        from unittest.mock import MagicMock

        def mock_request(method, url, **kwargs):
            return _response(503)

        client = MagicMock()
        client.request = mock_request

        with pytest.raises(hr.ExternalAPIRetriesExhaustedError):
            hr.request_with_retry_sync(
                client,
                "GET",
                "https://example.com/api",
                operation_name="test_sustained_sync",
                max_retries=2,
                base_delay_ms=1,
                max_delay_ms=5,
                max_total_ms=1000,
            )
