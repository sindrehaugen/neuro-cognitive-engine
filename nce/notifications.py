from __future__ import annotations

import asyncio
import logging
import os
from email.message import EmailMessage

import httpx

log = logging.getLogger("nce-notifications")

_MAX_TITLE_LEN: int = 256
_MAX_MESSAGE_LEN: int = 4_000
_MAX_SEND_RETRIES: int = 3


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    json: dict,
    *,
    channel: str,
) -> None:
    for attempt in range(_MAX_SEND_RETRIES):
        try:
            resp = await client.post(url, json=json)
            resp.raise_for_status()
            return
        except httpx.TimeoutException:
            log.warning("%s timeout attempt %d/%d", channel, attempt + 1, _MAX_SEND_RETRIES)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                log.error(
                    "%s rejected (status=%s); not retrying",
                    channel,
                    e.response.status_code,
                )
                return
            log.warning(
                "%s server error attempt %d/%d",
                channel,
                attempt + 1,
                _MAX_SEND_RETRIES,
            )
        except httpx.RequestError as e:
            log.warning(
                "%s request error attempt %d/%d: %s",
                channel,
                attempt + 1,
                _MAX_SEND_RETRIES,
                type(e).__name__,
            )
        if attempt < _MAX_SEND_RETRIES - 1:
            await asyncio.sleep(0.5 * (attempt + 1))
    log.error("%s send failed after %d attempts", channel, _MAX_SEND_RETRIES)


def _build_smtp_config() -> dict:
    """Read and validate SMTP configuration from environment variables.

    Separates configuration reading (outer-layer concern) from message sending
    (use-case concern). Enforces that hardcoded defaults are not permitted.
    """
    smtp_from = os.environ.get("NCE_SMTP_FROM", "").strip()
    smtp_to = os.environ.get("NCE_SMTP_TO", "").strip()

    if not smtp_from or not smtp_to:
        raise ValueError(
            "NCE_SMTP_FROM and NCE_SMTP_TO must be set. No example.com defaults permitted."
        )

    return {
        "from": smtp_from,
        "to": smtp_to,
        "username": os.environ.get("NCE_SMTP_USER", "").strip() or None,
        "password": os.environ.get("NCE_SMTP_PASS", "").strip() or None,
    }


class NotificationDispatcher:
    def __init__(self):
        self.slack_webhook = None  # To be loaded from config/env
        self.teams_webhook = None  # To be loaded from config/env
        self.smtp_host = None  # To be loaded from config/env
        self._queue = asyncio.Queue(maxsize=1000)
        self._worker_task = None
        self._http_client: httpx.AsyncClient | None = None

    async def start_worker(self):
        if self._worker_task is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
            self._worker_task = asyncio.create_task(self._worker())

    async def stop_worker(self):
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            while not self._queue.empty():
                try:
                    title, message = self._queue.get_nowait()
                    await asyncio.gather(
                        self._send_slack(title, message),
                        self._send_teams(title, message),
                        self._send_email(title, message),
                        return_exceptions=True,
                    )
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._worker_task = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def _worker(self):
        while True:
            try:
                title, message = await self._queue.get()
                tasks = [
                    self._send_slack(title, message),
                    self._send_teams(title, message),
                    self._send_email(title, message),
                    self._send_snmp(title, message),
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Notification worker error: %s", e)

    async def _send_slack(self, title: str, message: str):
        if not self.slack_webhook:
            return
        from nce.net_safety import validate_extractor_url

        await validate_extractor_url(self.slack_webhook, what="slack_webhook")
        if not self._http_client:
            return
        payload = {"text": f"*{title}*\n{message}"}
        await _post_with_retry(
            self._http_client,
            self.slack_webhook,
            payload,
            channel="slack",
        )

    async def _send_teams(self, title: str, message: str):
        if not self.teams_webhook:
            return
        from nce.net_safety import validate_extractor_url

        await validate_extractor_url(self.teams_webhook, what="teams_webhook")
        if not self._http_client:
            return
        payload = {"title": title, "text": message}
        await _post_with_retry(
            self._http_client,
            self.teams_webhook,
            payload,
            channel="teams",
        )

    async def _send_email(self, title: str, message: str):
        if not self.smtp_host:
            return
        try:
            import aiosmtplib
        except ImportError:
            log.warning(
                "Email alert skipped: aiosmtplib is not installed "
                "(install optional dep or configure SMTP off)"
            )
            return
        from nce.net_safety import validate_extractor_url

        await validate_extractor_url(f"smtp://{self.smtp_host}", what="smtp_host")
        try:
            smtp_config = _build_smtp_config()
            msg = EmailMessage()
            msg.set_content(message)
            msg["Subject"] = title
            msg["From"] = smtp_config["from"]
            msg["To"] = smtp_config["to"]
            # FIX-052: Port 587 with STARTTLS for secure SMTP.
            # Never use port 25 unencrypted in production.
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=587,
                use_tls=False,
                start_tls=True,
                timeout=5,
                username=smtp_config.get("username"),
                password=smtp_config.get("password"),
            )
        except Exception as e:
            log.error("Failed to send Email notification: %s", e)

    async def _send_snmp(self, title: str, message: str):
        log.debug(
            "SNMP notification not implemented; alert '%s' not delivered via SNMP.",
            title,
        )

    async def dispatch_alert(self, title: str, message: str):
        """
        Dispatches alert across all configured channels non-blockingly via a supervised queue.
        """
        title = title[:_MAX_TITLE_LEN]
        message = message[:_MAX_MESSAGE_LEN]
        log.warning("Dispatching Alert: %s", title)
        if self._worker_task is None:
            await self.start_worker()

        try:
            self._queue.put_nowait((title, message))
        except asyncio.QueueFull:
            log.error("Notification queue is full, dropping alert!")


dispatcher = NotificationDispatcher()
