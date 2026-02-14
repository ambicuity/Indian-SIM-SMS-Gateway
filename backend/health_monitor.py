"""
Health Monitor — Gateway System Health Tracking

Monitors the health of all gateway components:
  • ESP32 node heartbeat (is the edge device alive?)
  • Signal strength (can the SIM module receive SMS?)
  • Battery level (is the power supply adequate?)
  • Queue depth (is the backend overwhelmed?)
  • Consumer throughput (are messages being processed?)

Triggers CTO-Agent alerts when thresholds are breached.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

from config import get_settings

logger = logging.getLogger("sms_gateway.health")


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass
class NodeTelemetry:
    """Latest telemetry data from an ESP32 edge node."""
    node_id: str
    battery_mv: int = 0
    wifi_rssi: int = -127
    wifi_state: int = 0
    reconnects: int = 0
    wdt_resets: int = 0
    stored_sms_ids: int = 0
    uptime_sec: int = 0
    heap_free: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def battery_percent(self) -> int:
        """Estimate battery percentage from voltage (3.0V=0%, 4.2V=100%)."""
        if self.battery_mv <= 3000:
            return 0
        if self.battery_mv >= 4200:
            return 100
        return int((self.battery_mv - 3000) / 12)  # Linear approximation


class HealthMonitor:
    """
    Centralized health monitoring for the SMS gateway.

    Collects telemetry from edge nodes and backend metrics,
    evaluates health against configurable thresholds, and
    triggers alerts via callbacks.

    Usage:
        monitor = HealthMonitor()
        monitor.on_alert(cto_agent.trigger_alert)
        await monitor.start()
        monitor.update_telemetry(telemetry_data)
    """

    def __init__(self):
        settings = get_settings()
        self._nodes: dict[str, NodeTelemetry] = {}
        self._alert_callback: Optional[Callable] = None
        self._running = False
        self._check_task: Optional[asyncio.Task] = None

        # Thresholds
        self._battery_low = settings.battery_low_threshold
        self._signal_low = settings.signal_low_threshold
        self._heartbeat_timeout = settings.heartbeat_timeout_seconds
        self._check_interval = settings.health_check_interval_seconds

        # Queue metrics (updated by main app)
        self._queue_depth = 0
        self._queue_max_size = settings.queue_max_size

        # Overall status
        self._status = HealthStatus.UNKNOWN
        self._issues: list[str] = []

    def on_alert(self, callback: Callable) -> None:
        """Register alert callback (typically CTO-Agent)."""
        self._alert_callback = callback

    async def start(self) -> None:
        """Start periodic health checking."""
        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("Health monitor started (interval: %ds)", self._check_interval)

    async def stop(self) -> None:
        """Stop health monitoring."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    def update_telemetry(self, data: dict) -> None:
        """
        Update telemetry for an edge node.
        Called when MQTT telemetry message is received.
        """
        node_id = data.get("node_id", "unknown")

        if node_id not in self._nodes:
            self._nodes[node_id] = NodeTelemetry(node_id=node_id)
            logger.info("Health: new node registered: %s", node_id)

        node = self._nodes[node_id]
        node.battery_mv = data.get("battery_mv", node.battery_mv)
        node.wifi_rssi = data.get("wifi_rssi", node.wifi_rssi)
        node.wifi_state = data.get("wifi_state", node.wifi_state)
        node.reconnects = data.get("reconnects", node.reconnects)
        node.wdt_resets = data.get("wdt_resets", node.wdt_resets)
        node.stored_sms_ids = data.get("stored_sms_ids", node.stored_sms_ids)
        node.uptime_sec = data.get("uptime_sec", node.uptime_sec)
        node.heap_free = data.get("heap_free", node.heap_free)
        node.last_seen = time.time()

    def update_queue_depth(self, depth: int) -> None:
        """Update current queue depth for health evaluation."""
        self._queue_depth = depth

    def evaluate(self) -> dict:
        """
        Evaluate overall gateway health.
        Returns structured health report.
        """
        self._issues = []
        self._status = HealthStatus.HEALTHY

        now = time.time()

        # ─── Check each node ─────────────────────────────
        for node_id, node in self._nodes.items():
            # Heartbeat timeout
            if now - node.last_seen > self._heartbeat_timeout:
                self._issues.append(f"Node {node_id}: heartbeat timeout ({int(now - node.last_seen)}s ago)")
                self._status = HealthStatus.CRITICAL

            # Battery low
            elif node.battery_percent < self._battery_low:
                self._issues.append(f"Node {node_id}: battery low ({node.battery_percent}%)")
                self._status = max(self._status, HealthStatus.DEGRADED, key=lambda s: list(HealthStatus).index(s))

            # Signal low
            if node.wifi_rssi < self._signal_low and node.wifi_rssi > -127:
                self._issues.append(f"Node {node_id}: signal weak ({node.wifi_rssi} dBm)")
                self._status = max(self._status, HealthStatus.DEGRADED, key=lambda s: list(HealthStatus).index(s))

            # Frequent watchdog resets
            if node.wdt_resets > 5:
                self._issues.append(f"Node {node_id}: excessive watchdog resets ({node.wdt_resets})")
                self._status = max(self._status, HealthStatus.DEGRADED, key=lambda s: list(HealthStatus).index(s))

        # ─── Check queue health ──────────────────────────
        if self._queue_max_size > 0:
            queue_utilization = self._queue_depth / self._queue_max_size
            if queue_utilization > 0.9:
                self._issues.append(f"Queue near capacity ({self._queue_depth}/{self._queue_max_size})")
                self._status = HealthStatus.CRITICAL
            elif queue_utilization > 0.7:
                self._issues.append(f"Queue elevated ({self._queue_depth}/{self._queue_max_size})")
                self._status = max(self._status, HealthStatus.DEGRADED, key=lambda s: list(HealthStatus).index(s))

        # ─── No nodes registered ─────────────────────────
        if not self._nodes:
            self._status = HealthStatus.UNKNOWN
            self._issues.append("No edge nodes registered")

        return self.get_report()

    def get_report(self) -> dict:
        """Generate a structured health report."""
        return {
            "status": self._status.value,
            "timestamp": time.time(),
            "issues": self._issues,
            "nodes": {
                node_id: {
                    "battery_percent": node.battery_percent,
                    "battery_mv": node.battery_mv,
                    "wifi_rssi": node.wifi_rssi,
                    "uptime_sec": node.uptime_sec,
                    "wdt_resets": node.wdt_resets,
                    "last_seen": node.last_seen,
                    "last_seen_ago_sec": int(time.time() - node.last_seen),
                    "heap_free": node.heap_free,
                }
                for node_id, node in self._nodes.items()
            },
            "queue": {
                "depth": self._queue_depth,
                "max_size": self._queue_max_size,
                "utilization_percent": round(
                    (self._queue_depth / self._queue_max_size * 100)
                    if self._queue_max_size > 0
                    else 0,
                    1,
                ),
            },
        }

    async def _check_loop(self) -> None:
        """Periodic health check loop."""
        while self._running:
            try:
                report = self.evaluate()

                if report["status"] in ("degraded", "critical") and self._alert_callback:
                    await self._alert_callback(
                        alert_type=report["status"],
                        issues=report["issues"],
                        report=report,
                    )

                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check error: %s", e)
                await asyncio.sleep(self._check_interval)
