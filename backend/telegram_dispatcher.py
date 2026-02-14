"""
Telegram Dispatcher — Rate-Limited Message Delivery

Sends SMS messages to Telegram via the Bot API with:
  • Exponential backoff on 429 (rate limit) errors
  • Configurable retry limits
  • Zero-log policy (no OTP content in logs)
  • Connection pooling via httpx.AsyncClient
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from config import get_settings

logger = logging.getLogger("sms_gateway.telegram")

# Telegram Bot API rate limits:
#   - 30 messages per second to the same chat
#   - 20 messages per minute to the same group
TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramDispatcher:
    """
    Async Telegram Bot API message sender with rate-limit handling.

    Usage:
        dispatcher = TelegramDispatcher(bot_token="...", chat_id="...")
        success = await dispatcher.send(message)
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        max_retries: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ):
        settings = get_settings()
        self._bot_token = bot_token or settings.telegram_bot_token
        self._chat_id = chat_id or settings.telegram_chat_id
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._client: Optional[httpx.AsyncClient] = None

        # Rate limiting state
        self._last_send_time = 0.0
        self._min_interval = 1.0 / 30  # 30 msg/sec limit

        # Metrics
        self._total_sent = 0
        self._total_rate_limited = 0
        self._total_errors = 0

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create an httpx client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def send(self, message) -> bool:
        """
        Send a message to Telegram with rate-limit handling.

        Implements exponential backoff:
          Delay = min(base_backoff × 2^attempt, max_backoff)

        Args:
            message: QueuedMessage object with sender, body, timestamp

        Returns:
            True if message was sent successfully
        """
        if not self._bot_token or not self._chat_id:
            logger.error("Telegram not configured — missing bot_token or chat_id")
            return False

        # Format the message for Telegram
        text = self._format_message(message)

        for attempt in range(self._max_retries):
            try:
                # Respect rate limiting
                await self._throttle()

                client = await self._get_client()
                url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"

                response = await client.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )

                if response.status_code == 200:
                    self._total_sent += 1
                    logger.info("Telegram: delivered SMS %s (attempt %d)", message.sms_id, attempt + 1)
                    return True

                elif response.status_code == 429:
                    # Rate limited — extract retry_after from response
                    self._total_rate_limited += 1
                    retry_after = response.json().get("parameters", {}).get("retry_after", 0)
                    backoff = max(
                        retry_after,
                        min(self._base_backoff * (2 ** attempt), self._max_backoff),
                    )
                    logger.warning(
                        "Telegram: rate limited (429) for SMS %s — backing off %.1fs (attempt %d/%d)",
                        message.sms_id,
                        backoff,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue

                else:
                    self._total_errors += 1
                    logger.error(
                        "Telegram: HTTP %d for SMS %s (attempt %d/%d)",
                        response.status_code,
                        message.sms_id,
                        attempt + 1,
                        self._max_retries,
                    )

            except httpx.TimeoutException:
                self._total_errors += 1
                logger.error(
                    "Telegram: timeout for SMS %s (attempt %d/%d)",
                    message.sms_id,
                    attempt + 1,
                    self._max_retries,
                )
            except httpx.HTTPError as e:
                self._total_errors += 1
                logger.error(
                    "Telegram: HTTP error for SMS %s: %s (attempt %d/%d)",
                    message.sms_id,
                    type(e).__name__,
                    attempt + 1,
                    self._max_retries,
                )

            # Exponential backoff between retries
            if attempt < self._max_retries - 1:
                backoff = min(self._base_backoff * (2 ** attempt), self._max_backoff)
                await asyncio.sleep(backoff)

        logger.error("Telegram: ALL RETRIES EXHAUSTED for SMS %s", message.sms_id)
        return False

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _format_message(self, message) -> str:
        """Format SMS for Telegram display."""
        return (
            f"📱 <b>SMS Gateway Alert</b>\n\n"
            f"<b>From:</b> <code>{message.sender}</code>\n"
            f"<b>Time:</b> {message.timestamp}\n"
            f"<b>Node:</b> {message.node_id}\n\n"
            f"<b>Message:</b>\n<code>{message.body}</code>\n\n"
            f"<i>ID: {message.sms_id}</i>"
        )

    async def _throttle(self) -> None:
        """Simple rate limiter — ensures minimum interval between sends."""
        now = time.time()
        elapsed = now - self._last_send_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_send_time = time.time()

    @property
    def metrics(self) -> dict:
        return {
            "total_sent": self._total_sent,
            "total_rate_limited": self._total_rate_limited,
            "total_errors": self._total_errors,
        }
