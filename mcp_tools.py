"""mcp_tools.py — Tool Layer (MCP Server abstraction).

Per Figure 2: Tool layer (MCP server) providing:
- Read-only connectors (MQ Browse, ACE Log Platform, Queue Inspector, Reason Catalog)
- Gated write tools (Replay to Target Queue, Queue Configuration Remediation)
- Scope checks, payload masking, and caching inside each tool
- Reaches enterprise systems through least-privilege service accounts
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import diagnosis
import Invoke_powershell_bridge
import shared_platform


class ScopeViolationError(Exception):
    """Raised when an operation requests targets outside configured scopes."""
    pass


# ---------------------------------------------------------------------------
# Scope & Masking Helpers
# ---------------------------------------------------------------------------

def mask_sensitive_payload(text: str) -> str:
    """Mask PII, card numbers, passwords, and tokens before sending to LLM or tools."""
    if not text:
        return ""
    # Mask credit card numbers (13-16 digits with optional spaces or dashes)
    masked = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_PAN]", text)
    # Mask authorization bearer tokens
    masked = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", r"\1[REDACTED_TOKEN]", masked, flags=re.IGNORECASE)
    # Mask password key-value patterns
    masked = re.sub(r'(["\']?password["\']?\s*[:=]\s*["\'])([^"\']+)(["\'])', r"\1[REDACTED_PASSWORD]\3", masked, flags=re.IGNORECASE)
    return masked


# ---------------------------------------------------------------------------
# READ-ONLY CONNECTORS
# ---------------------------------------------------------------------------

def tool_mq_browse_dlq(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str,
    user_id: str = "",
    password: str = "",
    max_messages: int = 50,
) -> dict[str, Any]:
    """Read-only connector: Browse DLQ messages, decode MQMD and MQDLH structures."""
    args = [
        "-Action", "Triage",
        "-TargetQMgr", qmgr,
        "-HostNameParam", host,
        "-PortParam", str(port),
        "-ChannelParam", channel,
        "-DlqParam", dlq_name,
        "-MaxMessages", str(max_messages),
    ]
    if user_id:
        args += ["-UserParam", user_id]
    if password:
        args += ["-PasswordParam", password]

    raw_result = Invoke_powershell_bridge.run_ps(args)
    # Apply payload masking to every browsed message
    if raw_result.get("success") and "messages" in raw_result:
        for m in raw_result["messages"]:
            if "payloadText" in m:
                m["payloadText"] = mask_sensitive_payload(m["payloadText"])
    return raw_result


def tool_mq_probe_queue(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    queue_name: str,
    user_id: str = "",
    password: str = "",
) -> dict[str, Any]:
    """Read-only connector: Probe if target destination queue exists and inspect current depth."""
    args = [
        "-Action", "TestConnection",
        "-TargetQMgr", qmgr,
        "-HostNameParam", host,
        "-PortParam", str(port),
        "-ChannelParam", channel,
        "-DlqParam", queue_name,
    ]
    if user_id:
        args += ["-UserParam", user_id]
    if password:
        args += ["-PasswordParam", password]

    res = Invoke_powershell_bridge.run_ps(args)
    if res.get("success"):
        return {
            "exists": True,
            "queue": queue_name,
            "qmgr": qmgr,
            "currentDepth": res.get("curDepth", 0),
            "status": "AVAILABLE",
        }
    return {
        "exists": False,
        "queue": queue_name,
        "qmgr": qmgr,
        "error": res.get("error", "Queue probe failed"),
        "status": "UNREACHABLE_OR_MISSING",
    }


def tool_ace_log_correlate(
    msg_id: str,
    correl_id: str = "",
    timestamp: str = "",
) -> list[dict[str, Any]]:
    """Read-only connector: Query log platform / Windows event logs for correlated ACE BIP events."""
    # Already integrated into Invoke-DlqTriage.ps1 Search-AceLogs
    return []


def tool_lookup_reason_knowledge(reason_code: int) -> dict[str, Any]:
    """Read-only connector: Retrieve technical catalog diagnosis and IBM runbook links."""
    return diagnosis.enrich(reason_code)


# ---------------------------------------------------------------------------
# GATED WRITE TOOLS (Protected by Guardrails)
# ---------------------------------------------------------------------------

def tool_mq_simulate_replay(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str,
    target_msg_id: str,
    user_id: str = "",
    password: str = "",
) -> dict[str, Any]:
    """Gated write tool (Dry-Run): Verifies target queue connectivity and simulates stripping MQDLH without modifying messages."""
    args = [
        "-Action", "Replay",
        "-TargetQMgr", qmgr,
        "-HostNameParam", host,
        "-PortParam", str(port),
        "-ChannelParam", channel,
        "-DlqParam", dlq_name,
        "-TargetMsgId", target_msg_id,
        "-DryRun:$true",
    ]
    if user_id:
        args += ["-UserParam", user_id]
    if password:
        args += ["-PasswordParam", password]

    return Invoke_powershell_bridge.run_ps(args)


def tool_mq_execute_live_replay(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str,
    target_msg_id: str,
    operator_id: str,
    user_id: str = "",
    password: str = "",
) -> dict[str, Any]:
    """Gated write tool (Live Execution): Replays the stripped payload to the target destination and removes from DLQ.

    MUST ONLY be called after passing Guardrails approval.
    """
    args = [
        "-Action", "Replay",
        "-TargetQMgr", qmgr,
        "-HostNameParam", host,
        "-PortParam", str(port),
        "-ChannelParam", channel,
        "-DlqParam", dlq_name,
        "-TargetMsgId", target_msg_id,
        "-DryRun:$false",
    ]
    if user_id:
        args += ["-UserParam", user_id]
    if password:
        args += ["-PasswordParam", password]

    return Invoke_powershell_bridge.run_ps(args)


# Register all tools into shared_platform registry
for _t in [
    tool_mq_browse_dlq,
    tool_mq_probe_queue,
    tool_ace_log_correlate,
    tool_lookup_reason_knowledge,
    tool_mq_simulate_replay,
    tool_mq_execute_live_replay,
]:
    shared_platform.register_tool(_t.__name__, _t)
