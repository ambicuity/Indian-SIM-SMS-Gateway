"""
Indian SIM SMS Gateway — FastAPI Backend

High-availability OTP forwarding API that bridges MQTT messages
from ESP32 edge nodes to Telegram/Email delivery channels.

Endpoints:
  POST /api/sms/inbound     — Receive SMS from MQTT-HTTP bridge
  GET  /api/health           — System health status
  GET  /api/dlo              — List dead-lettered messages
  POST /api/dlo/{sms_id}/retry — Retry a dead-lettered message
  DELETE /api/dlo            — Purge all dead letters
  GET  /api/metrics          — System metrics
  GET  /api/incidents        — CTO-Agent incident history

Architecture:
  MQTT → /api/sms/inbound → asyncio.Queue → [Worker Pool] → Telegram/Email
                                                  ↓ (on failure)
                                            Dead Letter Office
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from config import get_settings, configure_logging
from message_queue import MessageQueue, QueuedMessage, MessagePriority
from telegram_dispatcher import TelegramDispatcher
from email_dispatcher import EmailDispatcher
from dead_letter_office import DeadLetterOffice
from health_monitor import HealthMonitor
from cto_agent import CTOAgent

# ─── Configure Logging ───────────────────────────────────
logger = configure_logging()

# ─── Global Components ───────────────────────────────────
settings = get_settings()
message_queue: Optional[MessageQueue] = None
telegram: Optional[TelegramDispatcher] = None
email_dispatch: Optional[EmailDispatcher] = None
dlo: Optional[DeadLetterOffice] = None
health_monitor: Optional[HealthMonitor] = None
cto_agent: Optional[CTOAgent] = None


# ─── Request / Response Models ────────────────────────────

class InboundSmsRequest(BaseModel):
    """Inbound SMS from MQTT-HTTP bridge or direct API call."""
    sender: str = Field(..., description="Phone number of SMS sender")
    body: str = Field(..., description="SMS content (encrypted)")
    timestamp: str = Field(default="", description="ISO-8601 timestamp")
    sms_id: str = Field(default="", description="Unique SMS identifier")
    node_id: str = Field(default="", description="ESP32 node identifier")
    encrypted: bool = Field(default=False, description="Whether body is encrypted")
    priority: str = Field(default="normal", description="Message priority: high, normal, low")


class TelemetryRequest(BaseModel):
    """Telemetry data from ESP32 edge nodes."""
    node_id: str
    battery_mv: int = 0
    wifi_rssi: int = -127
    wifi_state: int = 0
    reconnects: int = 0
    wdt_resets: int = 0
    stored_sms_ids: int = 0
    uptime_sec: int = 0
    heap_free: int = 0


class ApiResponse(BaseModel):
    """Standard API response."""
    success: bool
    message: str
    data: dict = {}


# ─── Application Lifecycle ────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle manager."""
    global message_queue, telegram, email_dispatch, dlo, health_monitor, cto_agent

    logger.info("╔══════════════════════════════════════╗")
    logger.info("║  SMS Gateway Backend — Starting      ║")
    logger.info("╚══════════════════════════════════════╝")

    # Initialize components
    telegram = TelegramDispatcher()
    email_dispatch = EmailDispatcher()
    dlo = DeadLetterOffice(ttl_hours=settings.dlo_ttl_hours)
    cto_agent = CTOAgent()
    health_monitor = HealthMonitor()
    health_monitor.on_alert(cto_agent.trigger_alert)

    # Initialize message queue
    message_queue = MessageQueue(
        max_size=settings.queue_max_size,
        concurrency=settings.consumer_concurrency,
        fernet_key=settings.fernet_encryption_key,
    )
    message_queue.register_consumer(telegram.send)
    message_queue.register_fallback(email_dispatch.send)
    message_queue.register_dlo(dlo.capture)

    # Start async workers
    await message_queue.start()
    await health_monitor.start()

    logger.info("✅ All systems initialized")

    yield  # App is running

    # ─── Graceful Shutdown ────────────────────────────────
    logger.info("Shutting down gracefully...")

    await health_monitor.stop()
    await message_queue.stop(drain_timeout=30.0)
    await telegram.close()
    await cto_agent.close()

    logger.info("✅ Shutdown complete")


# ─── FastAPI Application ──────────────────────────────────

app = FastAPI(
    title="Indian SIM SMS Gateway",
    description="High-Availability OTP Bridge — SMS to Telegram/Email",
    version="1.0.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════

@app.post("/api/sms/inbound", response_model=ApiResponse)
async def receive_sms(request: InboundSmsRequest):
    """
    Receive an inbound SMS from the MQTT-HTTP bridge.

    The message is enqueued for async delivery via Telegram (primary)
    or Email (fallback). If all delivery attempts fail, it is moved
    to the Dead Letter Office for manual recovery.
    """
    if not message_queue:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    # Map priority
    priority_map = {
        "high": MessagePriority.HIGH,
        "normal": MessagePriority.NORMAL,
        "low": MessagePriority.LOW,
    }

    msg = QueuedMessage(
        sms_id=request.sms_id or f"api-{int(time.time() * 1000)}",
        sender=request.sender,
        body=request.body,
        timestamp=request.timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
        node_id=request.node_id,
        max_retries=settings.max_retry_attempts,
        priority=priority_map.get(request.priority, MessagePriority.NORMAL),
    )

    success = await message_queue.enqueue(msg)

    if success:
        # Zero-log: only log metadata, never body content
        logger.info("API: Enqueued SMS %s from %s", msg.sms_id, msg.sender)
        return ApiResponse(
            success=True,
            message=f"SMS {msg.sms_id} enqueued for delivery",
            data={"sms_id": msg.sms_id, "queue_depth": message_queue.depth},
        )
    else:
        raise HTTPException(
            status_code=429,
            detail="Queue is full — backpressure active",
        )


@app.post("/api/telemetry", response_model=ApiResponse)
async def receive_telemetry(request: TelemetryRequest):
    """Receive telemetry from ESP32 edge nodes."""
    if health_monitor:
        health_monitor.update_telemetry(request.model_dump())
        if message_queue:
            health_monitor.update_queue_depth(message_queue.depth)
    return ApiResponse(success=True, message="Telemetry recorded")


@app.get("/api/health")
async def get_health():
    """
    System health endpoint.
    Returns status: healthy, degraded, critical, or unknown.
    """
    if not health_monitor:
        return {"status": "starting", "timestamp": time.time()}
    return health_monitor.evaluate()


@app.get("/api/dlo", response_model=ApiResponse)
async def list_dead_letters():
    """List all messages in the Dead Letter Office."""
    if not dlo:
        raise HTTPException(status_code=503, detail="DLO not initialized")

    dead_letters = await dlo.list_all()
    return ApiResponse(
        success=True,
        message=f"{len(dead_letters)} dead-lettered messages",
        data={"dead_letters": dead_letters, "count": len(dead_letters)},
    )


@app.post("/api/dlo/{sms_id}/retry", response_model=ApiResponse)
async def retry_dead_letter(sms_id: str):
    """Retry a dead-lettered message by re-enqueueing it."""
    if not dlo or not message_queue:
        raise HTTPException(status_code=503, detail="System not ready")

    success = await dlo.retry(sms_id, message_queue.enqueue)
    if success:
        return ApiResponse(success=True, message=f"SMS {sms_id} re-enqueued from DLO")
    else:
        raise HTTPException(status_code=404, detail=f"SMS {sms_id} not found in DLO")


@app.delete("/api/dlo", response_model=ApiResponse)
async def purge_dead_letters():
    """Purge all dead-lettered messages."""
    if not dlo:
        raise HTTPException(status_code=503, detail="DLO not initialized")

    count = await dlo.purge_all()
    return ApiResponse(success=True, message=f"Purged {count} dead letters", data={"purged": count})


@app.get("/api/metrics")
async def get_metrics():
    """Aggregate metrics from all subsystems."""
    metrics = {
        "timestamp": time.time(),
        "queue": message_queue.metrics if message_queue else {},
        "telegram": telegram.metrics if telegram else {},
        "email": email_dispatch.metrics if email_dispatch else {},
        "dlo": dlo.metrics if dlo else {},
        "cto_agent": cto_agent.metrics if cto_agent else {},
    }
    return metrics


@app.get("/api/incidents")
async def get_incidents(limit: int = 20):
    """Get CTO-Agent incident history."""
    if not cto_agent:
        return {"incidents": [], "count": 0}

    incidents = cto_agent.get_incidents(limit=limit)
    return {"incidents": incidents, "count": len(incidents)}


# ─── Root ─────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "Indian SIM SMS Gateway",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/health",
    }
