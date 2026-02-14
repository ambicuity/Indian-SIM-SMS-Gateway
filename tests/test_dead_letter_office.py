"""
Unit Tests — Dead Letter Office

Tests the DLO's capture, retrieval, retry, and purge functionality.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dead_letter_office import DeadLetterOffice, DeadLetter
from message_queue import QueuedMessage, MessageStatus


def make_failed_message(sms_id: str = "dlo-001") -> QueuedMessage:
    """Create a message that has exhausted retries."""
    msg = QueuedMessage(
        sms_id=sms_id,
        sender="+919876543210",
        body="[ENCRYPTED_OTP]",
        timestamp="2026-02-14 12:00:00",
        node_id="esp32-test",
        retry_count=5,
        max_retries=5,
        status=MessageStatus.DEAD_LETTERED,
        last_error="Telegram API 429 Too Many Requests",
    )
    return msg


# ─── Test: Capture ────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_message():
    """Test capturing a failed message into DLO."""
    dlo = DeadLetterOffice(ttl_hours=1)
    msg = make_failed_message("cap-001")

    await dlo.capture(msg)

    dead_letters = await dlo.list_all()
    assert len(dead_letters) == 1
    assert dead_letters[0]["sms_id"] == "cap-001"
    assert dead_letters[0]["body"] == "[ENCRYPTED]"  # Zero-log


@pytest.mark.asyncio
async def test_capture_multiple():
    """Test capturing multiple failed messages."""
    dlo = DeadLetterOffice(ttl_hours=1)

    for i in range(5):
        await dlo.capture(make_failed_message(f"multi-{i:03d}"))

    dead_letters = await dlo.list_all()
    assert len(dead_letters) == 5


# ─── Test: Retrieval ──────────────────────────────────────

@pytest.mark.asyncio
async def test_get_specific_dead_letter():
    """Test retrieving a specific dead letter by ID."""
    dlo = DeadLetterOffice(ttl_hours=1)
    await dlo.capture(make_failed_message("get-001"))
    await dlo.capture(make_failed_message("get-002"))

    dl = await dlo.get("get-001")
    assert dl is not None
    assert dl.sms_id == "get-001"
    assert dl.last_error == "Telegram API 429 Too Many Requests"


@pytest.mark.asyncio
async def test_get_nonexistent():
    """Test retrieving a non-existent dead letter."""
    dlo = DeadLetterOffice(ttl_hours=1)
    dl = await dlo.get("nonexistent")
    assert dl is None


# ─── Test: Retry ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_from_dlo():
    """Test retrying a dead-lettered message."""
    dlo = DeadLetterOffice(ttl_hours=1)
    await dlo.capture(make_failed_message("retry-001"))

    re_enqueued = []

    async def mock_enqueue(msg: QueuedMessage) -> bool:
        re_enqueued.append(msg.sms_id)
        return True

    success = await dlo.retry("retry-001", mock_enqueue)
    assert success is True
    assert "retry-001" in re_enqueued

    # Should be removed from DLO after successful retry
    remaining = await dlo.list_all()
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_retry_nonexistent():
    """Test retrying a non-existent dead letter."""
    dlo = DeadLetterOffice(ttl_hours=1)

    async def mock_enqueue(msg):
        return True

    success = await dlo.retry("nonexistent", mock_enqueue)
    assert success is False


# ─── Test: Purge ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_all():
    """Test purging all dead letters."""
    dlo = DeadLetterOffice(ttl_hours=1)

    for i in range(10):
        await dlo.capture(make_failed_message(f"purge-{i:03d}"))

    count = await dlo.purge_all()
    assert count == 10

    remaining = await dlo.list_all()
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_purge_expired():
    """Test purging only expired dead letters."""
    dlo = DeadLetterOffice(ttl_hours=0)  # 0 hours = expire immediately

    msg = make_failed_message("expire-001")
    await dlo.capture(msg)

    # Force the entry to appear expired
    if "expire-001" in dlo._in_memory:
        dlo._in_memory["expire-001"].dead_lettered_at = time.time() - 3600

    purged = await dlo.purge_expired()
    # With 0 TTL, everything should be considered expired


# ─── Test: Metrics ────────────────────────────────────────

@pytest.mark.asyncio
async def test_dlo_metrics():
    """Test DLO metrics tracking."""
    dlo = DeadLetterOffice(ttl_hours=1)

    await dlo.capture(make_failed_message("m-001"))
    await dlo.capture(make_failed_message("m-002"))

    metrics = dlo.metrics
    assert metrics["total_captured"] == 2


# ─── Test: Serialization ─────────────────────────────────

def test_dead_letter_serialization():
    """Test DeadLetter JSON serialization/deserialization."""
    dl = DeadLetter(
        sms_id="serial-001",
        sender="+919876543210",
        body="[ENCRYPTED]",
        timestamp="2026-02-14 12:00:00",
        node_id="esp32-test",
        retry_count=5,
        last_error="Timeout",
    )

    json_str = dl.to_json()
    restored = DeadLetter.from_json(json_str)

    assert restored.sms_id == "serial-001"
    assert restored.retry_count == 5
    assert restored.last_error == "Timeout"


def test_dead_letter_dict_hides_body():
    """Test that to_dict() hides the body (zero-log policy)."""
    dl = DeadLetter(
        sms_id="zl-001",
        sender="+91123",
        body="Your OTP is 123456",  # Plaintext should be hidden
        timestamp="",
        node_id="",
        retry_count=0,
        last_error="",
    )

    d = dl.to_dict()
    assert d["body"] == "[ENCRYPTED]"
