"""
Tests for LLM provider ``__repr__`` and ``_redact_api_key()`` — ensures
that API keys never appear in raw form in string representations.

Verification strategy:
  - Each provider's ``repr()`` is checked for the explicit absence of
    the full raw API key string.
  - ``_redact_api_key()`` edge cases are tested directly.
"""

from __future__ import annotations

import asyncio

import pytest

import nce.providers._http_utils
import nce.providers.base
from nce.providers.anthropic_provider import AnthropicProvider
from nce.providers.base import _redact_api_key
from nce.providers.google_gemini import GoogleGeminiProvider
from nce.providers.local_cognitive import LocalCognitiveProvider
from nce.providers.openai_compat import OpenAICompatProvider

# ---------------------------------------------------------------------------
# _redact_api_key edge cases
# ---------------------------------------------------------------------------


class TestRedactApiKey:
    """Unit tests for the ``_redact_api_key()`` utility."""

    def test_empty_key(self):
        """Empty string returns <empty>."""
        assert _redact_api_key("") == "<empty>"

    def test_short_key(self):
        """Keys ≤7 chars return <redacted>."""
        assert _redact_api_key("abc") == "<redacted>"
        assert _redact_api_key("1234567") == "<redacted>"

    def test_normal_key_preserves_first3_last4(self):
        """Standard key preserves first 3 and last 4 chars, middle ellipsized."""
        result = _redact_api_key("sk-ant-abcdefghijklmnop1234")
        assert result == "sk-...1234"
        assert "abcdefghijklmnop" not in result

    def test_exactly_8_chars(self):
        """8-char key preserves first 3 and last 4 (1 char middle)."""
        result = _redact_api_key("12345678")
        assert result == "123...5678"

    def test_key_value_not_in_output(self):
        """The full key must never appear in the redacted output."""
        key = "sk-proj-ABCDEF1234567890abcdef"
        result = _redact_api_key(key)
        assert key not in result
        assert "ABCDEF1234567890abcdef" not in result


# ---------------------------------------------------------------------------
# Provider __repr__ tests
# ---------------------------------------------------------------------------

_RAW_KEY = "sk-raw-test-key-do-not-leak-1234567890abcdef"


class TestAnthropicProviderRepr:
    """``AnthropicProvider.__repr__`` must not leak the raw API key."""

    def test_repr_does_not_contain_raw_key(self):
        provider = AnthropicProvider(
            api_key=_RAW_KEY,
            model="claude-sonnet-4-20250514",
        )
        rep = repr(provider)
        assert _RAW_KEY not in rep, f"Raw API key leaked in repr: {rep}"
        assert "...." not in rep  # No fully-ellipsized pattern
        # Should contain the redacted form (first 3 + ... + last 4)
        assert "sk-...cdef" in rep or _redact_api_key(_RAW_KEY) in rep


class TestOpenAICompatProviderRepr:
    """``OpenAICompatProvider.__repr__`` must not leak the raw API key."""

    def test_repr_does_not_contain_raw_key(self):
        provider = OpenAICompatProvider(
            api_key=_RAW_KEY,
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
        )
        rep = repr(provider)
        assert _RAW_KEY not in rep, f"Raw API key leaked in repr: {rep}"

    def test_repr_azure_with_endpoint(self, monkeypatch: pytest.MonkeyPatch):
        """Azure deployment repr includes endpoint but not raw key."""
        import socket

        def _mock_getaddrinfo(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        provider = OpenAICompatProvider(
            api_key=_RAW_KEY,
            model="gpt-4o",
            base_url="https://my-resource.openai.azure.com",
            is_azure=True,
        )
        rep = repr(provider)
        assert _RAW_KEY not in rep
        assert "azure=True" in rep


class TestGoogleGeminiProviderRepr:
    """``GoogleGeminiProvider.__repr__`` must not leak the raw API key."""

    def test_repr_does_not_contain_raw_key(self):
        provider = GoogleGeminiProvider(
            api_key=_RAW_KEY,
            model="gemini-2.0-flash",
        )
        rep = repr(provider)
        assert _RAW_KEY not in rep, f"Raw API key leaked in repr: {rep}"


class TestLocalCognitiveProviderRepr:
    """``LocalCognitiveProvider.__repr__`` has no API key (model + base_url only)."""

    def test_repr_contains_model_and_url(self, monkeypatch: pytest.MonkeyPatch):
        import socket

        def _mock_getaddrinfo(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr("socket.getaddrinfo", _mock_getaddrinfo)

        provider = LocalCognitiveProvider(
            base_url="http://localhost:11435",
            model="local-model",
        )
        rep = repr(provider)
        assert "local-model" in rep
        assert "localhost" in rep
        # No API key to leak — base_url is safe
        assert "api_key" not in rep.lower()


# ---------------------------------------------------------------------------
# RetryPolicy — jitter behaviour
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    """``RetryPolicy.delay_for_attempt`` must include full jitter."""

    def test_delays_are_non_deterministic(self):
        """Multiple calls for the same attempt produce different values."""
        rp = nce.providers.base.RetryPolicy(
            base_delay_ms=10_000,
            backoff_factor=2.0,
            max_delay_ms=60_000,
        )
        delays = {rp.delay_for_attempt(2) for _ in range(50)}
        assert len(delays) > 10, (
            f"Expected jitter to produce varied delays, got only {len(delays)} unique"
        )

    def test_delay_never_exceeds_cap(self):
        rp = nce.providers.base.RetryPolicy(
            base_delay_ms=10_000,
            backoff_factor=2.0,
            max_delay_ms=5_000,
        )
        for attempt in range(1, 7):
            assert rp.delay_for_attempt(attempt) <= rp.max_delay_ms

    def test_delay_never_zero(self):
        rp = nce.providers.base.RetryPolicy(
            base_delay_ms=1_000,
            backoff_factor=2.0,
        )
        for attempt in range(1, 6):
            assert rp.delay_for_attempt(attempt) >= 1

    def test_delays_increase_with_attempt(self):
        """Median of many samples should increase (jitter can cross over)."""
        rp = nce.providers.base.RetryPolicy(
            base_delay_ms=100,
            backoff_factor=4.0,
            max_delay_ms=100_000,
        )
        samples = 200
        medians = []
        for attempt in range(1, 5):
            vals = sorted(rp.delay_for_attempt(attempt) for _ in range(samples))
            medians.append(vals[samples // 2])
        # Each successive median should be larger
        for i in range(1, len(medians)):
            assert medians[i] > medians[i - 1], (
                f"Median delay decreased: attempt={i + 1} median={medians[i]} "
                f"vs attempt={i} median={medians[i - 1]}"
            )


# ---------------------------------------------------------------------------
# CircuitBreaker — state-machine transitions
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """``CircuitBreaker`` state machine behaviour."""

    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self):
        cb = nce.providers.base.CircuitBreaker()
        assert cb.state == nce.providers.base.CircuitBreakerState.CLOSED
        assert await cb.check() is True

    @pytest.mark.asyncio
    async def test_consecutive_failures_open_circuit(self):
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=60.0,
        )
        for _ in range(3):
            await cb.record_failure()
        assert cb.state == nce.providers.base.CircuitBreakerState.OPEN
        assert await cb.check() is False

    @pytest.mark.asyncio
    async def test_open_circuit_rejects_all_requests(self):
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=3600.0,
        )
        await cb.record_failure()
        for _ in range(10):
            assert await cb.check() is False

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        cb = nce.providers.base.CircuitBreaker(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        await cb.record_failure()
        # 2 failures then reset, then 1 failure → still below threshold
        assert cb.state == nce.providers.base.CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_transitions_after_recovery_timeout(self):
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
        )
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == nce.providers.base.CircuitBreakerState.OPEN
        await asyncio.sleep(0.02)
        assert await cb.check() is True
        assert cb.state == nce.providers.base.CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_closes_circuit(self):
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
        )
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.02)
        assert await cb.check() is True  # → HALF_OPEN
        await cb.record_success()
        assert cb.state == nce.providers.base.CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self):
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
        )
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.02)
        assert await cb.check() is True  # → HALF_OPEN
        await cb.record_failure()
        assert cb.state == nce.providers.base.CircuitBreakerState.OPEN
        # Still at threshold (2) so next failure keeps it open
        assert await cb.check() is False

    @pytest.mark.asyncio
    async def test_half_open_limits_probe_requests(self):
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.01,
            half_open_max_requests=1,
        )
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.02)
        assert await cb.check() is True  # 1st probe allowed
        assert await cb.check() is False  # 2nd probe blocked
        await cb.record_success()
        assert cb.state == nce.providers.base.CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_repr_contains_state_and_failure_count(self):
        cb = nce.providers.base.CircuitBreaker(failure_threshold=3)
        rep = repr(cb)
        assert "closed" in rep
        assert "0/3" in rep


# ---------------------------------------------------------------------------
# execute_with_retry — integration with circuit breaker
# ---------------------------------------------------------------------------


class _FakeProvider(nce.providers.base.LLMProvider):
    """Minimal LLMProvider subclass for testing execute_with_retry."""

    def __init__(self, identifier: str = "test/fake"):
        super().__init__()
        self._id = identifier

    async def complete(self, messages, response_model):
        raise NotImplementedError("Not used in these tests")

    def model_identifier(self) -> str:
        return self._id


class TestExecuteWithRetry:
    """``LLMProvider.execute_with_retry`` — retry loop behaviour."""

    @pytest.mark.asyncio
    async def test_successful_call_passthrough(self):
        provider = _FakeProvider()

        async def ok_op():
            return "ok"

        result = await provider.execute_with_retry(ok_op)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retryable_error_triggers_retry_then_succeeds(self):
        provider = _FakeProvider()
        call_count = 0

        async def flaky_operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise nce.providers.base.LLMRateLimitError(
                    "too fast",
                    provider="test/fake",
                    retry_after=1,
                )
            return "recovered"

        result = await provider.execute_with_retry(
            flaky_operation,
            retry_policy=nce.providers.base.RetryPolicy(
                max_retries=3,
                base_delay_ms=1,
            ),
        )
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises(self):
        provider = _FakeProvider()

        async def always_fails():
            raise nce.providers.base.LLMTimeoutError(
                "timeout",
                provider="test/fake",
            )

        with pytest.raises(nce.providers.base.LLMRetriesExhaustedError) as excinfo:
            await provider.execute_with_retry(
                always_fails,
                retry_policy=nce.providers.base.RetryPolicy(
                    max_retries=2,
                    base_delay_ms=1,
                ),
            )
        assert isinstance(excinfo.value.last_error, nce.providers.base.LLMTimeoutError)

    @pytest.mark.asyncio
    async def test_non_retryable_error_not_retried(self):
        provider = _FakeProvider()
        call_count = 0

        async def auth_error():
            nonlocal call_count
            call_count += 1
            raise nce.providers.base.LLMAuthenticationError(
                "bad key",
                provider="test/fake",
            )

        with pytest.raises(nce.providers.base.LLMAuthenticationError):
            await provider.execute_with_retry(
                auth_error,
                retry_policy=nce.providers.base.RetryPolicy(
                    max_retries=3,
                    base_delay_ms=1,
                ),
            )
        assert call_count == 1, "Should not retry auth errors"

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_and_blocks(self):
        provider = _FakeProvider()
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=3600.0,
        )

        async def failing_op():
            raise nce.providers.base.LLMUpstreamError(
                "server error",
                provider="test/fake",
            )

        # Exhaust retries — circuit breaker records failures
        with pytest.raises(nce.providers.base.LLMRetriesExhaustedError) as excinfo:
            await provider.execute_with_retry(
                failing_op,
                retry_policy=nce.providers.base.RetryPolicy(
                    max_retries=1,
                    base_delay_ms=1,
                ),
                circuit_breaker=cb,
            )
        assert isinstance(excinfo.value.last_error, nce.providers.base.LLMUpstreamError)

        # Circuit should now be open
        assert cb.state == nce.providers.base.CircuitBreakerState.OPEN

        # Next call should fail fast with "circuit breaker open"
        with pytest.raises(nce.providers.base.LLMCircuitOpenError) as excinfo:
            await provider.execute_with_retry(
                failing_op,
                retry_policy=nce.providers.base.RetryPolicy(
                    max_retries=1,
                    base_delay_ms=1,
                ),
                circuit_breaker=cb,
            )
        assert "Circuit breaker OPEN" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_success_closes_circuit_and_resets(self):
        provider = _FakeProvider()
        cb = nce.providers.base.CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.01,
        )
        await cb.record_failure()

        # Wait for recovery
        await asyncio.sleep(0.02)

        # This should succeed (half-open probe)
        async def ok_op():
            return "recovered"

        result = await provider.execute_with_retry(
            ok_op,
            retry_policy=nce.providers.base.RetryPolicy(max_retries=0, base_delay_ms=1),
            circuit_breaker=cb,
        )
        assert result == "recovered"
        assert cb.state == nce.providers.base.CircuitBreakerState.CLOSED


# ---------------------------------------------------------------------------
# _http_utils — status code mapping
# ---------------------------------------------------------------------------


class TestHttpErrorClassification:
    """``post_with_error_handling`` maps HTTP status to typed exceptions."""

    @pytest.mark.asyncio
    async def test_429_raises_llm_rate_limit_error(self, monkeypatch):
        async def _mock_post(*args, **kwargs):
            class _FakeResp:
                is_success = False
                status_code = 429
                headers = {"retry-after": "5"}

                def json(self):
                    return {"error": "rate limited"}

                @property
                def text(self):
                    return "rate limited"

            return _FakeResp()

        monkeypatch.setattr("httpx.AsyncClient.post", _mock_post)

        with pytest.raises(nce.providers.base.LLMRateLimitError) as excinfo:
            await nce.providers._http_utils.post_with_error_handling(
                url="https://api.test/v1/chat",
                body={"model": "test"},
                timeout=10.0,
                model_id="test/model",
            )
        assert excinfo.value.retry_after == 5
        assert excinfo.value.status_code == 429

    @pytest.mark.asyncio
    async def test_500_raises_llm_upstream_error(self, monkeypatch):
        async def _mock_post(*args, **kwargs):
            class _FakeResp:
                is_success = False
                status_code = 502

                def json(self):
                    return {"error": "bad gateway"}

                @property
                def text(self):
                    return "bad gateway"

            return _FakeResp()

        monkeypatch.setattr("httpx.AsyncClient.post", _mock_post)

        with pytest.raises(nce.providers.base.LLMUpstreamError) as excinfo:
            await nce.providers._http_utils.post_with_error_handling(
                url="https://api.test/v1/chat",
                body={"model": "test"},
                timeout=10.0,
                model_id="test/model",
            )
        assert excinfo.value.status_code == 502

    @pytest.mark.asyncio
    async def test_401_raises_llm_authentication_error(self, monkeypatch):
        async def _mock_post(*args, **kwargs):
            class _FakeResp:
                is_success = False
                status_code = 401

                def json(self):
                    return {"error": "unauthorized"}

                @property
                def text(self):
                    return "unauthorized"

            return _FakeResp()

        monkeypatch.setattr("httpx.AsyncClient.post", _mock_post)

        with pytest.raises(nce.providers.base.LLMAuthenticationError) as excinfo:
            await nce.providers._http_utils.post_with_error_handling(
                url="https://api.test/v1/chat",
                body={"model": "test"},
                timeout=10.0,
                model_id="test/model",
            )
        assert excinfo.value.status_code == 401
