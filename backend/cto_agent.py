"""
CTO-Agent — Homelab Automation Integration

Treats the SMS gateway as a 'managed service' and triggers corrective
actions via n8n webhooks when the system detects issues:

  • Signal loss → Restart network switch
  • Battery low → Push notification to operator
  • Heartbeat timeout → Full system health assessment
  • Queue overflow → Scale alert / emergency drain

The CTO-Agent acts as an autonomous operations layer:
  1. Receives health alerts from the HealthMonitor
  2. Evaluates severity and determines corrective action
  3. Fires structured webhook to n8n for automation
  4. Enforces cooldown to prevent alert storms
  5. Logs incident history for post-mortem analysis

n8n Webhook Integration:
  The webhook payload follows a structured schema that n8n can
  parse and route to different automation workflows.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

from config import get_settings

logger = logging.getLogger("sms_gateway.cto_agent")


class AlertSeverity(Enum):
    """Alert severity levels for routing in n8n."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class CorrectiveAction(Enum):
    """Predefined corrective actions for n8n to execute."""
    RESTART_NETWORK_SWITCH = "restart_network_switch"
    RESTART_GATEWAY_NODE = "restart_gateway_node"
    SEND_PUSH_NOTIFICATION = "send_push_notification"
    SEND_ESCALATION_EMAIL = "send_escalation_email"
    DRAIN_MESSAGE_QUEUE = "drain_message_queue"
    LOG_INCIDENT = "log_incident"
    NO_ACTION = "no_action"


@dataclass
class Incident:
    """Record of a detected issue and action taken."""
    incident_id: str
    alert_type: str
    severity: AlertSeverity
    issues: list[str]
    action: CorrectiveAction
    timestamp: float = field(default_factory=time.time)
    webhook_sent: bool = False
    webhook_response_code: int = 0
    resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "alert_type": self.alert_type,
            "severity": self.severity.value,
            "issues": self.issues,
            "action": self.action.value,
            "timestamp": self.timestamp,
            "webhook_sent": self.webhook_sent,
            "webhook_response_code": self.webhook_response_code,
            "resolved": self.resolved,
        }


class CTOAgent:
    """
    Autonomous operations agent for the SMS gateway.

    Monitors health events and triggers n8n webhook automations
    for corrective actions in the homelab environment.

    Usage:
        agent = CTOAgent(webhook_url="https://n8n.local/webhook/...")
        await agent.trigger_alert(
            alert_type="critical",
            issues=["Node esp32-01: heartbeat timeout (180s ago)"],
            report={...}
        )
    """

    def __init__(
        self,
        webhook_url: str = "",
        webhook_secret: str = "",
        cooldown_seconds: int = 300,
        max_incidents: int = 100,
    ):
        settings = get_settings()
        self._webhook_url = webhook_url or settings.n8n_webhook_url
        self._webhook_secret = webhook_secret or settings.n8n_webhook_secret
        self._cooldown_seconds = cooldown_seconds or settings.alert_cooldown_seconds
        self._max_incidents = max_incidents

        self._last_alert_time: dict[str, float] = {}  # Per-type cooldown
        self._incidents: list[Incident] = []
        self._client: Optional[httpx.AsyncClient] = None

        # Metrics
        self._total_alerts = 0
        self._total_suppressed = 0
        self._total_webhooks_sent = 0
        self._total_webhook_errors = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
            )
        return self._client

    async def trigger_alert(
        self,
        alert_type: str,
        issues: list[str],
        report: dict,
    ) -> Optional[Incident]:
        """
        Process a health alert and determine corrective action.

        Args:
            alert_type: "degraded" or "critical"
            issues: List of human-readable issue descriptions
            report: Full health report from HealthMonitor

        Returns:
            Incident record if alert was processed, None if suppressed
        """
        self._total_alerts += 1

        # ─── Cooldown Check ───────────────────────────────
        now = time.time()
        last_alert = self._last_alert_time.get(alert_type, 0)
        if now - last_alert < self._cooldown_seconds:
            self._total_suppressed += 1
            logger.info(
                "CTO-Agent: Alert suppressed (cooldown: %ds remaining)",
                int(self._cooldown_seconds - (now - last_alert)),
            )
            return None

        self._last_alert_time[alert_type] = now

        # ─── Determine Severity & Action ──────────────────
        severity = self._evaluate_severity(alert_type, issues)
        action = self._determine_action(issues, report)

        # ─── Create Incident ──────────────────────────────
        incident = Incident(
            incident_id=self._generate_incident_id(alert_type, now),
            alert_type=alert_type,
            severity=severity,
            issues=issues,
            action=action,
        )

        logger.warning(
            "CTO-Agent: INCIDENT %s | Severity: %s | Action: %s | Issues: %s",
            incident.incident_id,
            severity.value,
            action.value,
            "; ".join(issues[:3]),
        )

        # ─── Fire Webhook ─────────────────────────────────
        if self._webhook_url:
            await self._send_webhook(incident, report)
        else:
            logger.warning("CTO-Agent: No webhook URL configured — alert logged only")

        # ─── Store Incident ───────────────────────────────
        self._incidents.append(incident)
        if len(self._incidents) > self._max_incidents:
            self._incidents = self._incidents[-self._max_incidents:]

        return incident

    def _evaluate_severity(self, alert_type: str, issues: list[str]) -> AlertSeverity:
        """Map alert type and issues to severity level."""
        issue_text = " ".join(issues).lower()

        if "heartbeat timeout" in issue_text:
            return AlertSeverity.CRITICAL
        if "battery" in issue_text and "low" in issue_text:
            return AlertSeverity.WARNING
        if "queue near capacity" in issue_text:
            return AlertSeverity.EMERGENCY
        if alert_type == "critical":
            return AlertSeverity.CRITICAL
        if alert_type == "degraded":
            return AlertSeverity.WARNING

        return AlertSeverity.INFO

    def _determine_action(self, issues: list[str], report: dict) -> CorrectiveAction:
        """
        Determine the appropriate corrective action based on issues.
        This is the 'brain' of the CTO-Agent.
        """
        issue_text = " ".join(issues).lower()

        # Priority-ordered action determination
        if "heartbeat timeout" in issue_text:
            return CorrectiveAction.RESTART_NETWORK_SWITCH

        if "queue near capacity" in issue_text:
            return CorrectiveAction.DRAIN_MESSAGE_QUEUE

        if "battery low" in issue_text:
            return CorrectiveAction.SEND_PUSH_NOTIFICATION

        if "signal weak" in issue_text:
            return CorrectiveAction.RESTART_NETWORK_SWITCH

        if "watchdog resets" in issue_text:
            return CorrectiveAction.RESTART_GATEWAY_NODE

        return CorrectiveAction.LOG_INCIDENT

    async def _send_webhook(self, incident: Incident, report: dict) -> None:
        """Send structured webhook payload to n8n."""
        payload = {
            "event": "gateway_alert",
            "incident": incident.to_dict(),
            "health_report": report,
            "metadata": {
                "gateway_version": "1.0.0",
                "total_alerts": self._total_alerts,
                "total_suppressed": self._total_suppressed,
            },
        }

        headers = {
            "Content-Type": "application/json",
            "X-Gateway-Event": "alert",
            "X-Incident-ID": incident.incident_id,
        }

        # Add HMAC signature if secret is configured
        if self._webhook_secret:
            payload_bytes = json.dumps(payload, sort_keys=True).encode()
            signature = hmac.new(
                self._webhook_secret.encode(),
                payload_bytes,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={signature}"

        try:
            client = await self._get_client()
            response = await client.post(
                self._webhook_url,
                json=payload,
                headers=headers,
            )

            incident.webhook_sent = True
            incident.webhook_response_code = response.status_code
            self._total_webhooks_sent += 1

            if response.status_code == 200:
                logger.info(
                    "CTO-Agent: Webhook delivered for %s (action: %s)",
                    incident.incident_id,
                    incident.action.value,
                )
            else:
                logger.error(
                    "CTO-Agent: Webhook returned HTTP %d for %s",
                    response.status_code,
                    incident.incident_id,
                )

        except Exception as e:
            self._total_webhook_errors += 1
            logger.error("CTO-Agent: Webhook FAILED for %s: %s", incident.incident_id, e)

    def _generate_incident_id(self, alert_type: str, timestamp: float) -> str:
        """Generate a unique incident ID."""
        raw = f"{alert_type}:{timestamp}"
        return hashlib.md5(raw.encode()).hexdigest()[:12].upper()

    async def close(self) -> None:
        """Clean up HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def get_incidents(self, limit: int = 20) -> list[dict]:
        """Get recent incidents."""
        return [i.to_dict() for i in self._incidents[-limit:]]

    @property
    def metrics(self) -> dict:
        return {
            "total_alerts": self._total_alerts,
            "total_suppressed": self._total_suppressed,
            "total_webhooks_sent": self._total_webhooks_sent,
            "total_webhook_errors": self._total_webhook_errors,
            "active_incidents": len([i for i in self._incidents if not i.resolved]),
        }
