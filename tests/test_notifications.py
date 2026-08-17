import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from nce.net_safety import BridgeURLValidationError
from nce.notifications import (
    _MAX_MESSAGE_LEN,
    _MAX_SEND_RETRIES,
    _MAX_TITLE_LEN,
    NotificationDispatcher,
    _post_with_retry,
)


@pytest_asyncio.fixture
async def dispatcher():
    d = NotificationDispatcher()
    d.slack_webhook = "https://hooks.slack.com/services/test"
    d.teams_webhook = "https://example.com/teams/webhook"
    d.smtp_host = "smtp.example.com"
    await d.start_worker()
    yield d
    if d._worker_task is not None:
        await d.stop_worker()


@pytest.mark.asyncio
async def test_slack_dispatch(dispatcher):
    """Verify Slack webhook payloads are correctly formatted."""
    mock_post = AsyncMock()
    dispatcher._http_client.post = mock_post
    with patch("nce.net_safety.validate_extractor_url", return_value=dispatcher.slack_webhook):
        await dispatcher._send_slack("Test Alert", "System is down")
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://hooks.slack.com/services/test"
    assert kwargs["json"] == {"text": "*Test Alert*\nSystem is down"}


@pytest.mark.asyncio
async def test_teams_dispatch(dispatcher):
    """Verify Teams webhook payloads are correctly formatted."""
    mock_post = AsyncMock()
    dispatcher._http_client.post = mock_post
    with patch("nce.net_safety.validate_extractor_url", return_value=dispatcher.teams_webhook):
        await dispatcher._send_teams("Test Alert", "System is down")
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://example.com/teams/webhook"
    assert kwargs["json"] == {"title": "Test Alert", "text": "System is down"}


@pytest.mark.asyncio
async def test_email_dispatch(dispatcher, monkeypatch):
    """Verify SMTP emails are constructed and sent correctly."""
    monkeypatch.setenv("NCE_SMTP_FROM", "nce-alerts@example.com")
    monkeypatch.setenv("NCE_SMTP_TO", "admin@example.com")
    fake = MagicMock()
    fake.send = AsyncMock()
    with patch.dict(sys.modules, {"aiosmtplib": fake}):
        with patch("nce.net_safety.validate_extractor_url"):
            await dispatcher._send_email("Test Alert", "System is down")
    fake.send.assert_called_once()
    msg = fake.send.call_args[0][0]
    assert msg["Subject"] == "Test Alert"
    assert msg["To"] == "admin@example.com"
    assert msg["From"] == "nce-alerts@example.com"
    assert fake.send.call_args[1]["hostname"] == "smtp.example.com"


@pytest.mark.asyncio
async def test_snmp_dispatch(dispatcher):
    """Verify SNMP dispatch logs debug and does not raise."""
    with patch("nce.notifications.log") as mock_log:
        await dispatcher._send_snmp("Test Alert", "System is down")
    mock_log.debug.assert_called_once_with(
        "SNMP notification not implemented; alert '%s' not delivered via SNMP.",
        "Test Alert",
    )


@pytest.mark.asyncio
async def test_worker_dispatches_to_all_channels(dispatcher):
    """Verify the worker pulls from the queue and calls all dispatch methods."""
    with (
        patch.object(dispatcher, "_send_slack", new_callable=AsyncMock) as mock_slack,
        patch.object(dispatcher, "_send_teams", new_callable=AsyncMock) as mock_teams,
        patch.object(dispatcher, "_send_email", new_callable=AsyncMock) as mock_email,
        patch.object(dispatcher, "_send_snmp", new_callable=AsyncMock) as mock_snmp,
    ):
        await dispatcher.start_worker()
        await dispatcher.dispatch_alert("Test Alert", "System is down")

        # Wait for the queue to process the alert
        await dispatcher._queue.join()
        await dispatcher.stop_worker()

        mock_slack.assert_called_once_with("Test Alert", "System is down")
        mock_teams.assert_called_once_with("Test Alert", "System is down")
        mock_email.assert_called_once_with("Test Alert", "System is down")
        mock_snmp.assert_called_once_with("Test Alert", "System is down")


# ---------------------------------------------------------------------------
# Batch 1 — security: truncation, log sanitization, SSRF validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_alert_truncates_title(dispatcher):
    long_title = "T" * (_MAX_TITLE_LEN + 50)
    long_message = "short message"
    with patch.object(dispatcher._queue, "put_nowait") as mock_put:
        with patch("nce.notifications.log") as mock_log:
            await dispatcher.dispatch_alert(long_title, long_message)
    mock_put.assert_called_once_with((long_title[:_MAX_TITLE_LEN], long_message))
    mock_log.warning.assert_called_once_with("Dispatching Alert: %s", long_title[:_MAX_TITLE_LEN])


@pytest.mark.asyncio
async def test_dispatch_alert_truncates_message(dispatcher):
    title = "Alert"
    long_message = "M" * (_MAX_MESSAGE_LEN + 100)
    with patch.object(dispatcher._queue, "put_nowait") as mock_put:
        with patch("nce.notifications.log"):
            await dispatcher.dispatch_alert(title, long_message)
    mock_put.assert_called_once_with((title, long_message[:_MAX_MESSAGE_LEN]))


@pytest.mark.asyncio
async def test_dispatch_alert_log_excludes_message_content(dispatcher):
    secret_message = "password=super-secret-token"
    with patch.object(dispatcher._queue, "put_nowait"):
        with patch("nce.notifications.log") as mock_log:
            await dispatcher.dispatch_alert("Alert Title", secret_message)
    mock_log.warning.assert_called_once_with("Dispatching Alert: %s", "Alert Title")
    for call in mock_log.warning.call_args_list:
        assert secret_message not in str(call)


@pytest.mark.asyncio
async def test_send_slack_raises_on_internal_ip_webhook(dispatcher):
    with patch(
        "nce.net_safety.validate_extractor_url",
        side_effect=BridgeURLValidationError("blocked"),
    ):
        with pytest.raises(BridgeURLValidationError):
            await dispatcher._send_slack("Alert", "msg")


@pytest.mark.asyncio
async def test_send_email_raises_on_internal_ip_smtp_host(dispatcher, monkeypatch):
    dispatcher.smtp_host = "127.0.0.1"
    monkeypatch.setenv("NCE_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("NCE_SMTP_TO", "admin@example.com")
    fake = MagicMock()
    fake.send = AsyncMock()
    with patch.dict(sys.modules, {"aiosmtplib": fake}):
        with patch(
            "nce.net_safety.validate_extractor_url",
            side_effect=BridgeURLValidationError("blocked smtp"),
        ):
            with pytest.raises(BridgeURLValidationError):
                await dispatcher._send_email("Alert", "msg")
    fake.send.assert_not_called()


# ---------------------------------------------------------------------------
# Batch 2 — shared httpx client and SMTP authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_slack_reuses_shared_http_client(dispatcher):
    mock_post = AsyncMock()
    dispatcher._http_client.post = mock_post
    with patch("nce.net_safety.validate_extractor_url", return_value=dispatcher.slack_webhook):
        await dispatcher._send_slack("A1", "msg1")
        await dispatcher._send_slack("A2", "msg2")
    assert mock_post.call_count == 2
    assert dispatcher._http_client is not None


@pytest.mark.asyncio
async def test_stop_worker_closes_http_client():
    d = NotificationDispatcher()
    await d.start_worker()
    client = d._http_client
    assert client is not None
    with patch.object(client, "aclose", new_callable=AsyncMock) as mock_aclose:
        await d.stop_worker()
        mock_aclose.assert_awaited_once()
    assert d._http_client is None


@pytest.mark.asyncio
async def test_send_email_passes_smtp_credentials(dispatcher, monkeypatch):
    monkeypatch.setenv("NCE_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("NCE_SMTP_TO", "admin@example.com")
    monkeypatch.setenv("NCE_SMTP_USER", "smtp-user")
    monkeypatch.setenv("NCE_SMTP_PASS", "smtp-secret")
    fake = MagicMock()
    fake.send = AsyncMock()
    with patch.dict(sys.modules, {"aiosmtplib": fake}):
        with patch("nce.net_safety.validate_extractor_url"):
            await dispatcher._send_email("Alert", "body")
    assert fake.send.call_args[1]["username"] == "smtp-user"
    assert fake.send.call_args[1]["password"] == "smtp-secret"


@pytest.mark.asyncio
async def test_send_email_no_auth_when_credentials_unset(dispatcher, monkeypatch):
    monkeypatch.setenv("NCE_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("NCE_SMTP_TO", "admin@example.com")
    monkeypatch.delenv("NCE_SMTP_USER", raising=False)
    monkeypatch.delenv("NCE_SMTP_PASS", raising=False)
    fake = MagicMock()
    fake.send = AsyncMock()
    with patch.dict(sys.modules, {"aiosmtplib": fake}):
        with patch("nce.net_safety.validate_extractor_url"):
            await dispatcher._send_email("Alert", "body")
    assert fake.send.call_args[1]["username"] is None
    assert fake.send.call_args[1]["password"] is None


# ---------------------------------------------------------------------------
# Batch 3 — queue drain, exception narrowing, SNMP stub log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_worker_drains_remaining_queue_items():
    d = NotificationDispatcher()
    d.slack_webhook = "https://hooks.slack.com/services/test"
    d.teams_webhook = "https://example.com/teams/webhook"
    d.smtp_host = "smtp.example.com"
    await d.start_worker()

    send_slack = AsyncMock()
    send_teams = AsyncMock()
    send_email = AsyncMock()
    d._send_slack = send_slack
    d._send_teams = send_teams
    d._send_email = send_email

    for i in range(3):
        d._queue.put_nowait((f"title-{i}", f"msg-{i}"))

    await d.stop_worker()

    assert send_slack.await_count == 3
    assert send_teams.await_count == 3
    assert send_email.await_count == 3
    assert d._queue.empty()


@pytest.mark.asyncio
async def test_send_slack_timeout_logged_as_warning(dispatcher):
    mock_post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    dispatcher._http_client.post = mock_post
    with patch("nce.net_safety.validate_extractor_url", return_value=dispatcher.slack_webhook):
        with patch("nce.notifications.log") as mock_log:
            with patch("nce.notifications.asyncio.sleep", new_callable=AsyncMock):
                await dispatcher._send_slack("Alert", "msg")
    assert mock_log.warning.call_count == _MAX_SEND_RETRIES
    mock_log.error.assert_called_once_with(
        "%s send failed after %d attempts", "slack", _MAX_SEND_RETRIES
    )


@pytest.mark.asyncio
async def test_send_teams_http_status_error_logs_status_code(dispatcher):
    request = httpx.Request("POST", dispatcher.teams_webhook)
    response = httpx.Response(503, request=request)

    async def raise_503(*_args, **_kwargs):
        raise httpx.HTTPStatusError("error", request=request, response=response)

    mock_post = AsyncMock(side_effect=raise_503)
    dispatcher._http_client.post = mock_post
    with patch("nce.net_safety.validate_extractor_url", return_value=dispatcher.teams_webhook):
        with patch("nce.notifications.log") as mock_log:
            with patch("nce.notifications.asyncio.sleep", new_callable=AsyncMock):
                await dispatcher._send_teams("Alert", "msg")
    assert mock_post.await_count == _MAX_SEND_RETRIES
    mock_log.error.assert_called_once_with(
        "%s send failed after %d attempts", "teams", _MAX_SEND_RETRIES
    )


# ---------------------------------------------------------------------------
# Batch 4 — retry with backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_with_retry_timeout_exhausts_attempts():
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    with patch("nce.notifications.log") as mock_log:
        with patch("nce.notifications.asyncio.sleep", new_callable=AsyncMock):
            await _post_with_retry(client, "https://example.com/hook", {"x": 1}, channel="slack")
    assert client.post.await_count == _MAX_SEND_RETRIES
    mock_log.error.assert_called_once_with(
        "%s send failed after %d attempts", "slack", _MAX_SEND_RETRIES
    )


@pytest.mark.asyncio
async def test_post_with_retry_4xx_does_not_retry():
    client = AsyncMock()
    request = httpx.Request("POST", "https://example.com/hook")
    response = httpx.Response(400, request=request)

    async def raise_400(*_args, **_kwargs):
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    client.post = AsyncMock(side_effect=raise_400)
    with patch("nce.notifications.log") as mock_log:
        with patch("nce.notifications.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await _post_with_retry(client, "https://example.com/hook", {"x": 1}, channel="teams")
    assert client.post.await_count == 1
    mock_sleep.assert_not_awaited()
    mock_log.error.assert_called_once_with(
        "%s rejected (status=%s); not retrying",
        "teams",
        400,
    )


@pytest.mark.asyncio
async def test_post_with_retry_5xx_retries_to_limit():
    client = AsyncMock()
    request = httpx.Request("POST", "https://example.com/hook")
    response = httpx.Response(502, request=request)

    async def raise_502(*_args, **_kwargs):
        raise httpx.HTTPStatusError("bad gateway", request=request, response=response)

    client.post = AsyncMock(side_effect=raise_502)
    with patch("nce.notifications.log") as mock_log:
        with patch("nce.notifications.asyncio.sleep", new_callable=AsyncMock):
            await _post_with_retry(client, "https://example.com/hook", {"x": 1}, channel="slack")
    assert client.post.await_count == _MAX_SEND_RETRIES
    mock_log.error.assert_called_once_with(
        "%s send failed after %d attempts", "slack", _MAX_SEND_RETRIES
    )


@pytest.mark.asyncio
async def test_post_with_retry_succeeds_on_second_attempt():
    client = AsyncMock()
    ok_response = httpx.Response(200, request=httpx.Request("POST", "https://example.com/hook"))
    ok_response.raise_for_status = MagicMock()
    client.post = AsyncMock(
        side_effect=[httpx.TimeoutException("timed out"), ok_response],
    )
    with patch("nce.notifications.log") as mock_log:
        with patch("nce.notifications.asyncio.sleep", new_callable=AsyncMock):
            await _post_with_retry(client, "https://example.com/hook", {"x": 1}, channel="slack")
    assert client.post.await_count == 2
    mock_log.error.assert_not_called()
