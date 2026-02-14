"""
Unit Tests — CTO-Agent (n8n Webhook Integration)

Tests the CTO-Agent's alert evaluation, action determination,
cooldown enforcement, and incident logging.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from cto_agent import CTOAgent, AlertSeverity, CorrectiveAction, Incident


# ─── Test: Alert Triggering ───────────────────────────────

@pytest.mark.asyncio
async def test_trigger_alert_basic():
    """Test basic alert triggering without webhook."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0)

    incident = await agent.trigger_alert(
        alert_type="critical",
        issues=["Node esp32-01: heartbeat timeout (180s ago)"],
        report={"status": "critical"},
    )

    assert incident is not None
    assert incident.alert_type == "critical"
    assert incident.severity == AlertSeverity.CRITICAL
    assert incident.action == CorrectiveAction.RESTART_NETWORK_SWITCH
    assert len(incident.issues) == 1


@pytest.mark.asyncio
async def test_battery_low_alert():
    """Test that low battery triggers push notification action."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0)

    incident = await agent.trigger_alert(
        alert_type="degraded",
        issues=["Node esp32-01: battery low (15%)"],
        report={"status": "degraded"},
    )

    assert incident is not None
    assert incident.severity == AlertSeverity.WARNING
    assert incident.action == CorrectiveAction.SEND_PUSH_NOTIFICATION


@pytest.mark.asyncio
async def test_queue_overflow_alert():
    """Test that queue overflow triggers emergency action."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0)

    incident = await agent.trigger_alert(
        alert_type="critical",
        issues=["Queue near capacity (9500/10000)"],
        report={"status": "critical"},
    )

    assert incident is not None
    assert incident.severity == AlertSeverity.EMERGENCY
    assert incident.action == CorrectiveAction.DRAIN_MESSAGE_QUEUE


@pytest.mark.asyncio
async def test_signal_weak_alert():
    """Test that weak signal triggers network restart."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0)

    incident = await agent.trigger_alert(
        alert_type="degraded",
        issues=["Node esp32-01: signal weak (-105 dBm)"],
        report={"status": "degraded"},
    )

    assert incident is not None
    assert incident.action == CorrectiveAction.RESTART_NETWORK_SWITCH


# ─── Test: Cooldown ───────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_suppresses_alerts():
    """Test that cooldown prevents alert storms."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=300)

    # First alert should go through
    incident1 = await agent.trigger_alert(
        alert_type="critical",
        issues=["Test issue 1"],
        report={},
    )
    assert incident1 is not None

    # Second alert of same type should be suppressed
    incident2 = await agent.trigger_alert(
        alert_type="critical",
        issues=["Test issue 2"],
        report={},
    )
    assert incident2 is None


@pytest.mark.asyncio
async def test_different_alert_types_not_suppressed():
    """Test that different alert types have independent cooldowns."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=300)

    incident1 = await agent.trigger_alert(
        alert_type="critical",
        issues=["Critical issue"],
        report={},
    )
    assert incident1 is not None

    incident2 = await agent.trigger_alert(
        alert_type="degraded",
        issues=["Degraded issue"],
        report={},
    )
    assert incident2 is not None


# ─── Test: Webhook Payload ────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_payload_structure():
    """Test the structure of the webhook payload sent to n8n."""
    agent = CTOAgent(
        webhook_url="https://n8n.test/webhook/test",
        webhook_secret="test-secret",
        cooldown_seconds=0,
    )

    # Mock the HTTP client
    with patch.object(agent, '_get_client') as mock_get_client:
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        await agent.trigger_alert(
            alert_type="critical",
            issues=["Node esp32-01: heartbeat timeout"],
            report={"status": "critical", "nodes": {}},
        )

        # Verify webhook was called
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args

        # Verify payload structure
        payload = call_args.kwargs.get("json", {})
        assert payload["event"] == "gateway_alert"
        assert "incident" in payload
        assert "health_report" in payload
        assert "metadata" in payload

        # Verify HMAC signature header
        headers = call_args.kwargs.get("headers", {})
        assert "X-Webhook-Signature" in headers
        assert headers["X-Webhook-Signature"].startswith("sha256=")


# ─── Test: Incident History ──────────────────────────────

@pytest.mark.asyncio
async def test_incident_history():
    """Test incident history tracking."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0)

    for i in range(5):
        await agent.trigger_alert(
            alert_type=f"type-{i}",
            issues=[f"Issue {i}"],
            report={},
        )

    incidents = agent.get_incidents(limit=10)
    assert len(incidents) == 5

    # Verify most recent is last
    assert incidents[-1]["alert_type"] == "type-4"


@pytest.mark.asyncio
async def test_incident_history_limit():
    """Test that incident history respects max limit."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0, max_incidents=3)

    for i in range(10):
        await agent.trigger_alert(
            alert_type=f"type-{i}",
            issues=[f"Issue {i}"],
            report={},
        )

    incidents = agent.get_incidents()
    assert len(incidents) <= 3


# ─── Test: Metrics ────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_metrics():
    """Test CTO-Agent metrics tracking."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=0)

    await agent.trigger_alert("critical", ["Issue 1"], {})
    await agent.trigger_alert("degraded", ["Issue 2"], {})

    metrics = agent.metrics
    assert metrics["total_alerts"] == 2
    assert metrics["total_suppressed"] == 0


@pytest.mark.asyncio
async def test_suppression_metrics():
    """Test that suppressed alerts are tracked in metrics."""
    agent = CTOAgent(webhook_url="", cooldown_seconds=300)

    await agent.trigger_alert("critical", ["Issue 1"], {})
    await agent.trigger_alert("critical", ["Issue 2"], {})  # Suppressed

    metrics = agent.metrics
    assert metrics["total_alerts"] == 2
    assert metrics["total_suppressed"] == 1


# ─── Test: Severity Evaluation ────────────────────────────

def test_severity_evaluation():
    """Test severity mapping logic."""
    agent = CTOAgent(webhook_url="")

    assert agent._evaluate_severity("critical", ["heartbeat timeout"]) == AlertSeverity.CRITICAL
    assert agent._evaluate_severity("degraded", ["battery low"]) == AlertSeverity.WARNING
    assert agent._evaluate_severity("critical", ["queue near capacity"]) == AlertSeverity.EMERGENCY
    assert agent._evaluate_severity("degraded", ["general issue"]) == AlertSeverity.WARNING


# ─── Test: Action Determination ───────────────────────────

def test_action_determination():
    """Test corrective action routing logic."""
    agent = CTOAgent(webhook_url="")

    assert agent._determine_action(["heartbeat timeout"], {}) == CorrectiveAction.RESTART_NETWORK_SWITCH
    assert agent._determine_action(["battery low"], {}) == CorrectiveAction.SEND_PUSH_NOTIFICATION
    assert agent._determine_action(["queue near capacity"], {}) == CorrectiveAction.DRAIN_MESSAGE_QUEUE
    assert agent._determine_action(["watchdog resets"], {}) == CorrectiveAction.RESTART_GATEWAY_NODE
    assert agent._determine_action(["unknown issue"], {}) == CorrectiveAction.LOG_INCIDENT
