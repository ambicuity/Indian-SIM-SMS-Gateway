"""
Configuration Management — Indian SIM SMS Gateway

Loads all configuration from environment variables with Pydantic Settings.
Enforces Zero-Log Policy: no OTP content is ever logged to disk.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field


class GatewaySettings(BaseSettings):
    """
    Centralized configuration loaded from environment variables.
    All sensitive values are loaded at runtime, never hardcoded.
    """

    # ─── Telegram Bot ───────────────────────────────────────
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    telegram_chat_id: str = Field(default="", description="Telegram chat/group ID")

    # ─── Redis ──────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis connection URL")

    # ─── MQTT ───────────────────────────────────────────────
    mqtt_broker_host: str = Field(default="localhost", description="MQTT broker hostname")
    mqtt_broker_port: int = Field(default=1883, description="MQTT broker port")
    mqtt_topic_sms: str = Field(default="gateway/sms/inbound", description="MQTT topic for SMS")
    mqtt_topic_telemetry: str = Field(default="gateway/telemetry", description="MQTT topic for telemetry")

    # ─── n8n Webhook (CTO-Agent) ────────────────────────────
    n8n_webhook_url: str = Field(default="", description="n8n webhook endpoint for alerts")
    n8n_webhook_secret: str = Field(default="", description="HMAC secret for webhook auth")

    # ─── Email Fallback ─────────────────────────────────────
    smtp_host: str = Field(default="smtp.gmail.com", description="SMTP server host")
    smtp_port: int = Field(default=587, description="SMTP server port")
    smtp_username: str = Field(default="", description="SMTP username")
    smtp_password: str = Field(default="", description="SMTP password")
    email_recipient: str = Field(default="", description="Fallback email recipient")

    # ─── Encryption ─────────────────────────────────────────
    fernet_encryption_key: str = Field(default="", description="Fernet symmetric encryption key")

    # ─── Queue & Processing ─────────────────────────────────
    queue_max_size: int = Field(default=10000, description="Maximum in-memory queue size")
    max_retry_attempts: int = Field(default=5, description="Max retries before DLO")
    dlo_ttl_hours: int = Field(default=72, description="Dead letter retention (hours)")
    consumer_concurrency: int = Field(default=3, description="Number of consumer workers")

    # ─── Health & Alerts ────────────────────────────────────
    health_check_interval_seconds: int = Field(default=30, description="Health check interval")
    battery_low_threshold: int = Field(default=20, description="Battery % threshold for alert")
    signal_low_threshold: int = Field(default=-100, description="RSSI dBm threshold for alert")
    heartbeat_timeout_seconds: int = Field(default=120, description="Heartbeat timeout (sec)")
    alert_cooldown_seconds: int = Field(default=300, description="Min seconds between alerts")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


@lru_cache()
def get_settings() -> GatewaySettings:
    """Cached singleton for settings to avoid re-reading .env on every call."""
    return GatewaySettings()


def configure_logging() -> logging.Logger:
    """
    Configure logging with Zero-Log Policy.

    ZERO-LOG POLICY:
    - No OTP content is written to log files
    - No SMS body text appears in any log output
    - Only metadata (timestamps, IDs, status codes) are logged
    - Logs are written to stdout only (no file persistence)
    """
    logger = logging.getLogger("sms_gateway")
    logger.setLevel(logging.INFO)

    # Console handler only — no file handler (zero-log policy)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(handler)

    # Suppress verbose library logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logger
