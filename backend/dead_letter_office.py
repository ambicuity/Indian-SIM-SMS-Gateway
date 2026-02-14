"""
Dead Letter Office (DLO) — Failed Message Retention

Captures messages that exhaust all delivery retries and provides:
  • Persistent storage in Redis (with configurable TTL)
  • REST API for listing, retrying, and purging dead letters
  • Alert triggers via CTO-Agent when messages are dead-lettered

Design Philosophy:
  Messages are too important to silently drop. The DLO ensures every
  failed delivery is tracked, alertable, and manually recoverable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import get_settings

logger = logging.getLogger("sms_gateway.dlo")

# Redis key prefix for DLO entries
DLO_REDIS_KEY = "sms_gateway:dlo"


@dataclass
class DeadLetter:
    """A message that has been moved to the Dead Letter Office."""
    sms_id: str
    sender: str
    body: str  # Encrypted — Zero-Log Policy
    timestamp: str
    node_id: str
    retry_count: int
    last_error: str
    dead_lettered_at: float = field(default_factory=time.time)
    manual_retry_count: int = 0

    def to_dict(self) -> dict:
        return {
            "sms_id": self.sms_id,
            "sender": self.sender,
            "body": "[ENCRYPTED]",  # Zero-log: never expose OTP
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "dead_lettered_at": self.dead_lettered_at,
            "manual_retry_count": self.manual_retry_count,
        }

    def to_json(self) -> str:
        """Serialize for Redis storage (includes encrypted body for retry)."""
        return json.dumps({
            "sms_id": self.sms_id,
            "sender": self.sender,
            "body": self.body,  # Kept encrypted for retry capability
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "dead_lettered_at": self.dead_lettered_at,
            "manual_retry_count": self.manual_retry_count,
        })

    @classmethod
    def from_json(cls, data: str) -> "DeadLetter":
        d = json.loads(data)
        return cls(**d)


class DeadLetterOffice:
    """
    Manages failed message retention and recovery.

    Supports two storage backends:
      1. Redis (production) — persistent with TTL
      2. In-memory (development/testing) — volatile

    Usage:
        dlo = DeadLetterOffice(redis_client=redis)
        await dlo.capture(message)
        dead_letters = await dlo.list_all()
        await dlo.retry(sms_id, re_enqueue_callback)
        await dlo.purge_expired()
    """

    def __init__(self, redis_client=None, ttl_hours: int = 72):
        settings = get_settings()
        self._redis = redis_client
        self._ttl_seconds = (ttl_hours or settings.dlo_ttl_hours) * 3600
        self._in_memory: dict[str, DeadLetter] = {}

        # Metrics
        self._total_captured = 0
        self._total_retried = 0
        self._total_purged = 0

    async def capture(self, message) -> None:
        """
        Capture a failed message into the Dead Letter Office.

        Args:
            message: QueuedMessage that exhausted all retries
        """
        dead_letter = DeadLetter(
            sms_id=message.sms_id,
            sender=message.sender,
            body=message.body,
            timestamp=message.timestamp,
            node_id=getattr(message, "node_id", ""),
            retry_count=message.retry_count,
            last_error=message.last_error,
        )

        if self._redis:
            try:
                await self._redis.hset(DLO_REDIS_KEY, message.sms_id, dead_letter.to_json())
                # Set TTL on the hash (applied to entire hash — acceptable trade-off)
                await self._redis.expire(DLO_REDIS_KEY, self._ttl_seconds)
            except Exception as e:
                logger.error("DLO: Redis capture failed: %s — falling back to memory", e)
                self._in_memory[message.sms_id] = dead_letter
        else:
            self._in_memory[message.sms_id] = dead_letter

        self._total_captured += 1
        logger.warning(
            "DLO: Captured SMS %s (error: %s, retries: %d)",
            message.sms_id,
            message.last_error[:100],
            message.retry_count,
        )

    async def list_all(self) -> list[dict]:
        """List all dead-lettered messages (metadata only — no OTP content)."""
        if self._redis:
            try:
                raw = await self._redis.hgetall(DLO_REDIS_KEY)
                return [
                    DeadLetter.from_json(v).to_dict()
                    for v in raw.values()
                ]
            except Exception as e:
                logger.error("DLO: Redis list failed: %s", e)

        return [dl.to_dict() for dl in self._in_memory.values()]

    async def get(self, sms_id: str) -> Optional[DeadLetter]:
        """Get a specific dead letter by SMS ID."""
        if self._redis:
            try:
                raw = await self._redis.hget(DLO_REDIS_KEY, sms_id)
                if raw:
                    return DeadLetter.from_json(raw)
            except Exception:
                pass

        return self._in_memory.get(sms_id)

    async def retry(self, sms_id: str, re_enqueue_callback) -> bool:
        """
        Retry a dead-lettered message by re-enqueueing it.

        Args:
            sms_id: The SMS ID to retry
            re_enqueue_callback: async function to put the message back in the queue

        Returns:
            True if successfully re-enqueued
        """
        dead_letter = await self.get(sms_id)
        if not dead_letter:
            logger.warning("DLO: SMS %s not found in dead letters", sms_id)
            return False

        dead_letter.manual_retry_count += 1

        try:
            from message_queue import QueuedMessage, MessageStatus
            message = QueuedMessage(
                sms_id=dead_letter.sms_id,
                sender=dead_letter.sender,
                body=dead_letter.body,
                timestamp=dead_letter.timestamp,
                node_id=dead_letter.node_id,
                retry_count=0,  # Reset retries for manual re-try
                status=MessageStatus.QUEUED,
            )
            success = await re_enqueue_callback(message)
            if success:
                await self.remove(sms_id)
                self._total_retried += 1
                logger.info("DLO: Re-enqueued SMS %s (manual retry #%d)", sms_id, dead_letter.manual_retry_count)
                return True
        except Exception as e:
            logger.error("DLO: Retry failed for SMS %s: %s", sms_id, e)

        return False

    async def remove(self, sms_id: str) -> bool:
        """Remove a dead letter (after successful retry or manual purge)."""
        if self._redis:
            try:
                removed = await self._redis.hdel(DLO_REDIS_KEY, sms_id)
                return removed > 0
            except Exception:
                pass

        if sms_id in self._in_memory:
            del self._in_memory[sms_id]
            return True
        return False

    async def purge_expired(self) -> int:
        """Remove dead letters older than TTL. Returns count of purged entries."""
        cutoff = time.time() - self._ttl_seconds
        purged = 0

        if self._redis:
            try:
                all_entries = await self._redis.hgetall(DLO_REDIS_KEY)
                for sms_id, raw in all_entries.items():
                    dl = DeadLetter.from_json(raw)
                    if dl.dead_lettered_at < cutoff:
                        await self._redis.hdel(DLO_REDIS_KEY, sms_id)
                        purged += 1
            except Exception as e:
                logger.error("DLO: Redis purge failed: %s", e)
        else:
            expired_ids = [
                sms_id for sms_id, dl in self._in_memory.items()
                if dl.dead_lettered_at < cutoff
            ]
            for sms_id in expired_ids:
                del self._in_memory[sms_id]
                purged += 1

        self._total_purged += purged
        if purged > 0:
            logger.info("DLO: Purged %d expired dead letters", purged)
        return purged

    async def purge_all(self) -> int:
        """Purge all dead letters. Returns count of purged entries."""
        if self._redis:
            try:
                count = await self._redis.hlen(DLO_REDIS_KEY)
                await self._redis.delete(DLO_REDIS_KEY)
                self._total_purged += count
                return count
            except Exception:
                pass

        count = len(self._in_memory)
        self._in_memory.clear()
        self._total_purged += count
        return count

    @property
    def metrics(self) -> dict:
        return {
            "total_captured": self._total_captured,
            "total_retried": self._total_retried,
            "total_purged": self._total_purged,
            "current_count": len(self._in_memory),  # Approximate for Redis
        }
