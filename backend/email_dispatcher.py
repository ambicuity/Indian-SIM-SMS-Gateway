"""
Email Dispatcher — SMTP Fallback Delivery

Fallback delivery channel for when Telegram is unavailable.
Uses async SMTP (aiosmtplib) with TLS encryption.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import aiosmtplib
except ImportError:
    aiosmtplib = None  # type: ignore

from config import get_settings

logger = logging.getLogger("sms_gateway.email")


class EmailDispatcher:
    """
    Async email sender via SMTP with TLS.
    Acts as a fallback when Telegram delivery fails.

    Usage:
        dispatcher = EmailDispatcher()
        success = await dispatcher.send(message)
    """

    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 0,
        username: str = "",
        password: str = "",
        recipient: str = "",
        max_retries: int = 3,
    ):
        settings = get_settings()
        self._smtp_host = smtp_host or settings.smtp_host
        self._smtp_port = smtp_port or settings.smtp_port
        self._username = username or settings.smtp_username
        self._password = password or settings.smtp_password
        self._recipient = recipient or settings.email_recipient
        self._max_retries = max_retries

        # Metrics
        self._total_sent = 0
        self._total_errors = 0

    async def send(self, message) -> bool:
        """
        Send an SMS notification via email.

        Args:
            message: QueuedMessage object

        Returns:
            True if email was sent successfully
        """
        if not all([self._smtp_host, self._username, self._password, self._recipient]):
            logger.warning("Email not configured — skipping fallback")
            return False

        if aiosmtplib is None:
            logger.error("aiosmtplib not installed — email fallback unavailable")
            return False

        for attempt in range(self._max_retries):
            try:
                msg = self._build_email(message)

                await aiosmtplib.send(
                    msg,
                    hostname=self._smtp_host,
                    port=self._smtp_port,
                    username=self._username,
                    password=self._password,
                    use_tls=False,
                    start_tls=True,
                    timeout=30,
                )

                self._total_sent += 1
                logger.info("Email: delivered SMS %s (attempt %d)", message.sms_id, attempt + 1)
                return True

            except Exception as e:
                self._total_errors += 1
                logger.error(
                    "Email: failed for SMS %s: %s (attempt %d/%d)",
                    message.sms_id,
                    type(e).__name__,
                    attempt + 1,
                    self._max_retries,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        return False

    def _build_email(self, message) -> MIMEMultipart:
        """Build the email message."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📱 SMS Gateway: Message from {message.sender}"
        msg["From"] = self._username
        msg["To"] = self._recipient

        # Plain text version
        text_body = (
            f"SMS Gateway Notification\n\n"
            f"From: {message.sender}\n"
            f"Time: {message.timestamp}\n"
            f"Node: {message.node_id}\n\n"
            f"Message:\n{message.body}\n\n"
            f"SMS ID: {message.sms_id}"
        )

        # HTML version
        html_body = f"""
        <html>
        <body style="font-family: 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        padding: 20px; border-radius: 10px 10px 0 0;">
                <h2 style="color: white; margin: 0;">📱 SMS Gateway Alert</h2>
            </div>
            <div style="background: #f8f9fa; padding: 20px; border-radius: 0 0 10px 10px;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 8px; font-weight: bold;">From:</td>
                        <td style="padding: 8px;"><code>{message.sender}</code></td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-weight: bold;">Time:</td>
                        <td style="padding: 8px;">{message.timestamp}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; font-weight: bold;">Node:</td>
                        <td style="padding: 8px;">{message.node_id}</td>
                    </tr>
                </table>
                <div style="background: white; padding: 15px; border-radius: 8px;
                            margin-top: 15px; border-left: 4px solid #667eea;">
                    <p style="margin: 0; white-space: pre-wrap;">{message.body}</p>
                </div>
                <p style="color: #6c757d; font-size: 12px; margin-top: 15px;">
                    ID: {message.sms_id}
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        return msg

    @property
    def metrics(self) -> dict:
        return {
            "total_sent": self._total_sent,
            "total_errors": self._total_errors,
        }
