"""
Asynchronous Producer-Consumer Message Queue

Implements a bounded async queue with multiple consumer workers to ensure
no OTP is lost even when the Telegram API is rate-limited.

Architecture:
    MQTT Subscriber (Producer)
        |
        v
    asyncio.Queue (Bounded Buffer)
        |
    ┌───┴───┐
    v       v
  Worker1  Worker2  Worker3  (Consumers)
    |       |       |
    v       v       v
  Telegram/Email Dispatch

Backpressure: When the queue reaches max_size, producers block until
a consumer frees a slot — ensuring we never silently drop messages.
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable
from cryptography.fernet import Fernet

from config import get_settings

logger = logging.getLogger("sms_gateway.queue")


class MessagePriority(Enum):
    """Message priority levels for queue ordering."""
    HIGH = 0      # OTP messages (time-sensitive)
    NORMAL = 1    # Regular SMS
    LOW = 2       # Telemetry / system messages


class MessageStatus(Enum):
    """Lifecycle status of a queued message."""
    QUEUED = "queued"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


@dataclass
class QueuedMessage:
    """
    Represents an SMS message in the processing pipeline.

    NOTE: Zero-Log Policy — the `body` field is encrypted in transit
    and only decrypted in-memory at the point of dispatch. Never logged.
    """
    sms_id: str
    sender: str
    body: str                                   # Encrypted content
    timestamp: str
    node_id: str = ""
    status: MessageStatus = MessageStatus.QUEUED
    retry_count: int = 0
    max_retries: int = 5
    created_at: float = field(default_factory=time.time)
    last_error: str = ""
    priority: MessagePriority = MessagePriority.NORMAL

    @property
    def is_retriable(self) -> bool:
        return self.retry_count < self.max_retries

    def to_dict(self) -> dict:
        """Serialize for DLO storage (body remains encrypted)."""
        return {
            "sms_id": self.sms_id,
            "sender": self.sender,
            "body": "[ENCRYPTED]",  # Zero-log: never serialize plaintext
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "last_error": self.last_error,
        }


# ─── Consumer Callback Type ──────────────────────────────

ConsumerCallback = Callable[[QueuedMessage], Awaitable[bool]]


class MessageQueue:
    """
    Async producer-consumer queue with bounded buffer and
    configurable concurrency.

    Usage:
        queue = MessageQueue(max_size=10000, concurrency=3)
        queue.register_consumer(telegram_dispatcher.send)
        queue.register_fallback(email_dispatcher.send)
        await queue.start()

        # Producer side:
        await queue.enqueue(message)

        # Shutdown:
        await queue.stop()
    """

    def __init__(
        self,
        max_size: int = 10000,
        concurrency: int = 3,
        fernet_key: str = "",
    ):
        self._queue: asyncio.Queue[QueuedMessage] = asyncio.Queue(maxsize=max_size)
        self._concurrency = concurrency
        self._consumers: list[ConsumerCallback] = []
        self._fallback: Optional[ConsumerCallback] = None
        self._dlo_callback: Optional[Callable[[QueuedMessage], Awaitable[None]]] = None
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._fernet: Optional[Fernet] = None

        # Metrics
        self._total_enqueued = 0
        self._total_delivered = 0
        self._total_failed = 0
        self._total_dead_lettered = 0

        if fernet_key:
            try:
                self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
            except Exception:
                logger.warning("Invalid Fernet key — encryption disabled")

    # ─── Registration ─────────────────────────────────────

    def register_consumer(self, callback: ConsumerCallback) -> None:
        """Register a primary consumer (e.g., Telegram dispatcher)."""
        self._consumers.append(callback)
        logger.info("Registered primary consumer: %s", callback.__qualname__)

    def register_fallback(self, callback: ConsumerCallback) -> None:
        """Register a fallback consumer (e.g., Email dispatcher)."""
        self._fallback = callback
        logger.info("Registered fallback consumer: %s", callback.__qualname__)

    def register_dlo(self, callback: Callable[[QueuedMessage], Awaitable[None]]) -> None:
        """Register Dead Letter Office callback."""
        self._dlo_callback = callback
        logger.info("Registered DLO handler")

    # ─── Lifecycle ────────────────────────────────────────

    async def start(self) -> None:
        """Start consumer worker tasks."""
        if self._running:
            logger.warning("Queue already running")
            return

        if not self._consumers:
            raise RuntimeError("No consumers registered — call register_consumer() first")

        self._running = True
        for i in range(self._concurrency):
            task = asyncio.create_task(self._worker(i), name=f"queue-worker-{i}")
            self._workers.append(task)

        logger.info(
            "Message queue started: %d workers, max_size=%d",
            self._concurrency,
            self._queue.maxsize,
        )

    async def stop(self, drain_timeout: float = 30.0) -> None:
        """
        Gracefully stop the queue.
        Waits up to drain_timeout seconds for remaining messages to process.
        """
        self._running = False
        logger.info("Stopping queue — draining %d remaining messages...", self._queue.qsize())

        # Wait for queue to drain
        try:
            async with asyncio.timeout(drain_timeout):
                await self._queue.join()
        except asyncio.TimeoutError:
            logger.warning(
                "Drain timeout reached — %d messages still in queue",
                self._queue.qsize(),
            )

        # Cancel workers
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Queue stopped")

    # ─── Producer ─────────────────────────────────────────

    async def enqueue(self, message: QueuedMessage) -> bool:
        """
        Add a message to the queue. Blocks if queue is full (backpressure).
        Returns True if enqueued successfully.
        """
        try:
            await asyncio.wait_for(self._queue.put(message), timeout=10.0)
            self._total_enqueued += 1
            # Zero-log: log ID only, never body content
            logger.info(
                "Enqueued SMS %s from %s (queue depth: %d)",
                message.sms_id,
                message.sender,
                self._queue.qsize(),
            )
            return True
        except asyncio.TimeoutError:
            logger.error("Queue full — backpressure timeout for SMS %s", message.sms_id)
            return False

    # ─── Consumer Worker ──────────────────────────────────

    async def _worker(self, worker_id: int) -> None:
        """
        Consumer worker loop. Dequeues messages and dispatches them
        through registered consumers with retry and fallback logic.
        """
        logger.info("Worker-%d started", worker_id)

        while self._running:
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            message.status = MessageStatus.PROCESSING
            delivered = False

            # Try primary consumers
            for consumer in self._consumers:
                try:
                    success = await consumer(message)
                    if success:
                        message.status = MessageStatus.DELIVERED
                        self._total_delivered += 1
                        delivered = True
                        logger.info(
                            "Worker-%d delivered SMS %s via %s",
                            worker_id,
                            message.sms_id,
                            consumer.__qualname__,
                        )
                        break
                except Exception as e:
                    message.last_error = str(e)
                    logger.error(
                        "Worker-%d consumer %s failed for SMS %s: %s",
                        worker_id,
                        consumer.__qualname__,
                        message.sms_id,
                        type(e).__name__,
                    )

            # Try fallback consumer
            if not delivered and self._fallback:
                try:
                    success = await self._fallback(message)
                    if success:
                        message.status = MessageStatus.DELIVERED
                        self._total_delivered += 1
                        delivered = True
                        logger.info(
                            "Worker-%d delivered SMS %s via FALLBACK",
                            worker_id,
                            message.sms_id,
                        )
                except Exception as e:
                    message.last_error = str(e)

            # Handle failure — retry or DLO
            if not delivered:
                message.retry_count += 1
                if message.is_retriable:
                    # Re-enqueue with exponential backoff delay
                    backoff = min(2 ** message.retry_count, 60)
                    logger.warning(
                        "Worker-%d retry %d/%d for SMS %s (backoff: %ds)",
                        worker_id,
                        message.retry_count,
                        message.max_retries,
                        message.sms_id,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    message.status = MessageStatus.QUEUED
                    await self._queue.put(message)
                else:
                    # Exhausted retries — send to Dead Letter Office
                    message.status = MessageStatus.DEAD_LETTERED
                    self._total_dead_lettered += 1
                    self._total_failed += 1
                    logger.error(
                        "Worker-%d SMS %s → DEAD LETTER OFFICE (retries exhausted)",
                        worker_id,
                        message.sms_id,
                    )
                    if self._dlo_callback:
                        await self._dlo_callback(message)

            self._queue.task_done()

    # ─── Encryption ───────────────────────────────────────

    def decrypt_body(self, encrypted_body: str) -> str:
        """Decrypt an encrypted SMS body. In-memory only — never logged."""
        if self._fernet:
            try:
                return self._fernet.decrypt(encrypted_body.encode()).decode()
            except Exception:
                logger.warning("Decryption failed — returning raw content")
        return encrypted_body

    def encrypt_body(self, plaintext: str) -> str:
        """Encrypt SMS body for storage/transit."""
        if self._fernet:
            return self._fernet.encrypt(plaintext.encode()).decode()
        return plaintext

    # ─── Metrics ──────────────────────────────────────────

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def metrics(self) -> dict:
        return {
            "queue_depth": self._queue.qsize(),
            "max_size": self._queue.maxsize,
            "total_enqueued": self._total_enqueued,
            "total_delivered": self._total_delivered,
            "total_failed": self._total_failed,
            "total_dead_lettered": self._total_dead_lettered,
            "active_workers": len([w for w in self._workers if not w.done()]),
            "running": self._running,
        }
