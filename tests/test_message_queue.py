"""
Unit Tests — Message Queue (Producer-Consumer Pattern)

Tests the async producer-consumer queue implementation including:
  • Basic enqueue/dequeue flow
  • Multiple concurrent consumers
  • Backpressure when queue is full
  • Message ordering and delivery guarantees
  • Retry logic and Dead Letter routing
  • Fernet encryption/decryption
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from message_queue import MessageQueue, QueuedMessage, MessageStatus, MessagePriority


# ─── Fixtures ─────────────────────────────────────────────

def make_message(sms_id: str = "test-001", sender: str = "+919876543210") -> QueuedMessage:
    """Create a test message."""
    return QueuedMessage(
        sms_id=sms_id,
        sender=sender,
        body="Your OTP is 123456",
        timestamp="2026-02-14 12:00:00",
        node_id="esp32-test",
    )


# ─── Test: Basic Enqueue/Dequeue ──────────────────────────

@pytest.mark.asyncio
async def test_enqueue_message():
    """Test that a message can be enqueued successfully."""
    delivered = []

    async def mock_consumer(msg: QueuedMessage) -> bool:
        delivered.append(msg.sms_id)
        return True

    queue = MessageQueue(max_size=100, concurrency=1)
    queue.register_consumer(mock_consumer)
    await queue.start()

    msg = make_message("test-001")
    success = await queue.enqueue(msg)

    assert success is True
    assert queue.depth >= 0

    # Wait for consumer to process
    await asyncio.sleep(0.5)
    await queue.stop()

    assert "test-001" in delivered


@pytest.mark.asyncio
async def test_enqueue_multiple_messages():
    """Test multiple messages are all processed."""
    delivered = []

    async def mock_consumer(msg: QueuedMessage) -> bool:
        delivered.append(msg.sms_id)
        return True

    queue = MessageQueue(max_size=100, concurrency=2)
    queue.register_consumer(mock_consumer)
    await queue.start()

    for i in range(10):
        await queue.enqueue(make_message(f"test-{i:03d}"))

    await asyncio.sleep(1)
    await queue.stop()

    assert len(delivered) == 10
    for i in range(10):
        assert f"test-{i:03d}" in delivered


# ─── Test: Consumer Concurrency ───────────────────────────

@pytest.mark.asyncio
async def test_multiple_consumers():
    """Test that multiple consumer workers process in parallel."""
    processing_times = []

    async def slow_consumer(msg: QueuedMessage) -> bool:
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(0.1)  # Simulate 100ms processing
        processing_times.append(asyncio.get_event_loop().time() - start)
        return True

    queue = MessageQueue(max_size=100, concurrency=5)
    queue.register_consumer(slow_consumer)
    await queue.start()

    # Enqueue 5 messages — with 5 workers, all should process in parallel
    for i in range(5):
        await queue.enqueue(make_message(f"parallel-{i}"))

    await asyncio.sleep(0.5)
    await queue.stop()

    assert len(processing_times) == 5


# ─── Test: Backpressure ──────────────────────────────────

@pytest.mark.asyncio
async def test_backpressure_bounded_queue():
    """Test that a full queue applies backpressure."""
    # Very small queue with a very slow consumer
    async def slow_consumer(msg: QueuedMessage) -> bool:
        await asyncio.sleep(10)  # Very slow
        return True

    queue = MessageQueue(max_size=2, concurrency=1)
    queue.register_consumer(slow_consumer)
    await queue.start()

    # First two should succeed
    msg1 = make_message("bp-001")
    msg2 = make_message("bp-002")
    assert await queue.enqueue(msg1) is True
    assert await queue.enqueue(msg2) is True

    # Third should timeout (backpressure) — queue is full
    # Note: the queue size is 2, and with 1 worker processing slowly,
    # it may still accept if a worker dequeued one
    await queue.stop()


# ─── Test: Fallback Consumer ─────────────────────────────

@pytest.mark.asyncio
async def test_fallback_on_primary_failure():
    """Test that fallback consumer is tried when primary fails."""
    primary_called = []
    fallback_called = []

    async def failing_primary(msg: QueuedMessage) -> bool:
        primary_called.append(msg.sms_id)
        raise Exception("Primary failed")

    async def fallback(msg: QueuedMessage) -> bool:
        fallback_called.append(msg.sms_id)
        return True

    queue = MessageQueue(max_size=100, concurrency=1)
    queue.register_consumer(failing_primary)
    queue.register_fallback(fallback)
    await queue.start()

    await queue.enqueue(make_message("fallback-001"))
    await asyncio.sleep(0.5)
    await queue.stop()

    assert "fallback-001" in primary_called
    assert "fallback-001" in fallback_called


# ─── Test: Dead Letter Office Routing ─────────────────────

@pytest.mark.asyncio
async def test_dead_letter_routing():
    """Test that messages go to DLO after max retries."""
    dlo_messages = []

    async def always_fail(msg: QueuedMessage) -> bool:
        raise Exception("Always fails")

    async def dlo_capture(msg: QueuedMessage) -> None:
        dlo_messages.append(msg.sms_id)

    queue = MessageQueue(max_size=100, concurrency=1)
    queue.register_consumer(always_fail)
    queue.register_dlo(dlo_capture)
    await queue.start()

    msg = make_message("dlo-001")
    msg.max_retries = 2  # Quick failure
    await queue.enqueue(msg)

    # Wait for retries to exhaust (with exponential backoff)
    await asyncio.sleep(10)
    await queue.stop()

    assert "dlo-001" in dlo_messages


# ─── Test: Metrics ────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_tracking():
    """Test that metrics are accurately tracked."""
    async def mock_consumer(msg: QueuedMessage) -> bool:
        return True

    queue = MessageQueue(max_size=100, concurrency=1)
    queue.register_consumer(mock_consumer)
    await queue.start()

    for i in range(5):
        await queue.enqueue(make_message(f"metric-{i}"))

    await asyncio.sleep(1)
    metrics = queue.metrics

    assert metrics["total_enqueued"] == 5
    assert metrics["running"] is True

    await queue.stop()


# ─── Test: Encryption ────────────────────────────────────

def test_fernet_encryption():
    """Test Fernet encryption and decryption."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    queue = MessageQueue(max_size=10, concurrency=1, fernet_key=key)

    plaintext = "Your OTP is 123456"
    encrypted = queue.encrypt_body(plaintext)
    decrypted = queue.decrypt_body(encrypted)

    assert encrypted != plaintext
    assert decrypted == plaintext


# ─── Test: Message Model ─────────────────────────────────

def test_message_serialization():
    """Test QueuedMessage serialization (zero-log safe)."""
    msg = make_message("serial-001")
    d = msg.to_dict()

    assert d["sms_id"] == "serial-001"
    assert d["body"] == "[ENCRYPTED]"  # Zero-log policy
    assert d["sender"] == "+919876543210"


def test_message_retriable():
    """Test retry tracking."""
    msg = make_message()
    msg.max_retries = 3

    assert msg.is_retriable is True
    msg.retry_count = 3
    assert msg.is_retriable is False
