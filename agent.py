"""IBM MQ & ACE Dead Letter Queue (DLQ) Resolution Agent.

Agent definition, tools, and runner execution using Google ADK (Agent Development Kit).
Configuration is read from config.json via config_loader — no hard-coded values.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap: load .env then config.json
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
SCRIPT_PATH = ROOT / "Invoke-DlqTriage.ps1"


def _bootstrap_env() -> None:
    """Read .env and populate os.environ (credentials override; config.json is the source of truth for everything else)."""
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v and k not in os.environ:
                os.environ[k] = v
        if "GOOGLE_API_KEY" in os.environ and "GEMINI_API_KEY" not in os.environ:
            os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
        elif "GEMINI_API_KEY" in os.environ and "GOOGLE_API_KEY" not in os.environ:
            os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
    except Exception as exc:
        print(f"[agent.py] Warning: Failed to load .env: {exc}")


_bootstrap_env()

# ---------------------------------------------------------------------------
# Config (all tunable values come from here)
# ---------------------------------------------------------------------------
import config_loader
import diagnosis
import duplicate_check
import approval_log
import shared_platform

_agent_cfg = config_loader.agent()
MODEL_NAME = os.environ.get("GEMINI_MODEL") or _agent_cfg.get("model", "gemini-3.5-flash")

# ---------------------------------------------------------------------------
# Google ADK Imports
# ---------------------------------------------------------------------------
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part
from agent_instructions import SYSTEM_INSTRUCTION

# ---------------------------------------------------------------------------
# PowerShell helper
# ---------------------------------------------------------------------------

def _run_powershell(args: list[str]) -> dict[str, Any]:
    """Execute Invoke-DlqTriage.ps1 with *args* and return parsed JSON output."""
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(SCRIPT_PATH), *args, "-AsJson",
    ]
    try:
        res = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        raw = res.stdout.strip()
        if not raw and res.stderr:
            return {"success": False, "error": res.stderr.strip()}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"success": False, "error": raw or res.stderr.strip() or "Unknown PowerShell failure"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# ADK Tool: get_dlq_messages
# ---------------------------------------------------------------------------

def get_dlq_messages(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str = "SYSTEM.DEAD.LETTER.QUEUE",
    user_id: str = "",
    password: str = "",
) -> str:
    """Fetch and triage messages from the Dead Letter Queue.

    Inspects message descriptors (MQMD), parses the Dead Letter Header (MQDLH),
    extracts the target queue, extracts reason code, and previews the stripped payload.

    Args:
        qmgr: Target IBM MQ Queue Manager name (e.g. QMGR2).
        host: Hostname or IP address of the MQ listener.
        port: TCP port of the listener (e.g. 1415).
        channel: SVRCONN channel name (e.g. SYSTEM.ADMIN.SVRCONN).
        dlq_name: Dead Letter Queue name (defaults to SYSTEM.DEAD.LETTER.QUEUE).
        user_id: Optional MQ username.
        password: Optional MQ password.

    Returns:
        JSON string containing success, curDepth, and array of analyzed messages.
    """
    args = [
        "-Action", "Triage",
        "-TargetQMgr", qmgr,
        "-HostNameParam", host,
        "-PortParam", str(port),
        "-ChannelParam", channel,
        "-DlqParam", dlq_name,
    ]
    if user_id:
        args += ["-UserParam", user_id]
    if password:
        args += ["-PasswordParam", password]
    result = _run_powershell(args)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# ADK Tool: test_mq_connection
# ---------------------------------------------------------------------------

def test_mq_connection(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str = "SYSTEM.DEAD.LETTER.QUEUE",
    user_id: str = "",
    password: str = "",
) -> str:
    """Test connectivity to the specified IBM MQ Queue Manager and check DLQ depth.

    Args:
        qmgr: Target IBM MQ Queue Manager name.
        host: Hostname or IP of the listener.
        port: TCP port number.
        channel: SVRCONN channel name.
        dlq_name: Dead letter queue name.
        user_id: Optional username.
        password: Optional password.

    Returns:
        JSON string containing success status, current DLQ depth, and any error message.
    """
    args = [
        "-Action", "TestConnection",
        "-TargetQMgr", qmgr,
        "-HostNameParam", host,
        "-PortParam", str(port),
        "-ChannelParam", channel,
        "-DlqParam", dlq_name,
    ]
    if user_id:
        args += ["-UserParam", user_id]
    if password:
        args += ["-PasswordParam", password]
    result = _run_powershell(args)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# ADK Tool: lookup_mq_reason_code  (enriched — reads runbooks from config.json)
# ---------------------------------------------------------------------------

def lookup_mq_reason_code(reason_code: int) -> str:
    """Look up technical explanation, classification, remediation, and runbook URL for an IBM MQ reason code.

    Runbook links are configured in config.json → runbooks and can be updated without code changes.

    Args:
        reason_code: Numeric MQ reason code (e.g. 2085, 2035, 2053, 2058).

    Returns:
        JSON string with code constant, classification, root cause, recommended fix, and runbook URL.
    """
    return diagnosis.enrich_json(reason_code)


# ---------------------------------------------------------------------------
# ADK Tool: replay_message_dry_run  (duplicate-check + approval log)
# ---------------------------------------------------------------------------

def replay_message_dry_run(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str,
    target_msg_id: str,
    operator_id: str = "unknown",
) -> str:
    """Simulate replay of a DLQ message without modifying queue data.

    Blocks if the message has already been processed (duplicate detection).
    Records an approval log entry for every invocation (config.json → approvals).

    Args:
        qmgr: Queue Manager name.
        host: Hostname.
        port: TCP port.
        channel: SVRCONN channel.
        dlq_name: DLQ name.
        target_msg_id: Hexadecimal Message ID of the message to test replay for.
        operator_id: Identifier of the human operator requesting this action.

    Returns:
        JSON string with simulation result and status.
    """
    # --- Duplicate guard ---
    dup_status = duplicate_check.verify(target_msg_id)
    if dup_status == "duplicate":
        return json.dumps({
            "success": False,
            "duplicateStatus": "duplicate",
            "error": f"Message ID {target_msg_id} has already been processed. Duplicate replay blocked.",
        }, indent=2)
    if dup_status == "inconclusive":
        return json.dumps({
            "success": False,
            "duplicateStatus": "inconclusive",
            "error": "Message ID is empty or unverifiable. Replay blocked per safety policy.",
        }, indent=2)

    # --- Approval record ---
    approval_log.record(operator_id, "replay_dry_run", target_msg_id, f"qmgr={qmgr} dlq={dlq_name}")

    # --- Dry-run PowerShell call ---
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
    result = _run_powershell(args)
    result["duplicateStatus"] = dup_status
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# act() — safe replay entrypoint (server.py keeps live replay disabled)
# ---------------------------------------------------------------------------

def act(
    qmgr: str,
    host: str,
    port: int,
    channel: str,
    dlq_name: str,
    target_msg_id: str,
    operator_id: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Safe replay controller.

    Steps:
    1. Verify duplicate (block if duplicate or inconclusive).
    2. Log approval record.
    3. Execute dry-run (always) to confirm destination is reachable.
    4. If dry_run=False and pre-checks pass → live replay via PowerShell.
       Live replay is NOT enabled in this build; the branch exists for future
       integration once transactional syncpoint support is confirmed.

    Returns a result dict (never raises).
    """
    dup_status = duplicate_check.verify(target_msg_id)
    if dup_status in ("duplicate", "inconclusive"):
        return {
            "success": False,
            "duplicateStatus": dup_status,
            "error": (
                "Duplicate detected — replay blocked." if dup_status == "duplicate"
                else "Message ID inconclusive — replay blocked per safety policy."
            ),
        }

    action_label = "replay_dry_run" if dry_run else "replay_live"
    approval_log.record(operator_id, action_label, target_msg_id, f"qmgr={qmgr} dlq={dlq_name}")

    # Always run dry-run first
    dry_result = json.loads(replay_message_dry_run(qmgr, host, port, channel, dlq_name, target_msg_id, operator_id))
    if not dry_result.get("success"):
        return {**dry_result, "stage": "dry_run_failed"}

    if dry_run:
        return {**dry_result, "stage": "dry_run_passed", "liveReplayEnabled": False}

    # Live replay is intentionally disabled — enable only after transactional
    # syncpoint safety is validated against the target broker version.
    return {
        "success": False,
        "stage": "live_replay_blocked",
        "error": (
            "Live replay is disabled. Enable only after transactional syncpoint safety, "
            "downstream health integration, and authenticated approval are validated."
        ),
        "duplicateStatus": dup_status,
    }


# ---------------------------------------------------------------------------
# Register tools in shared-platform registry
# ---------------------------------------------------------------------------

for _tool in [get_dlq_messages, test_mq_connection, lookup_mq_reason_code, replay_message_dry_run]:
    shared_platform.register_tool(_tool.__name__, _tool)

# ---------------------------------------------------------------------------
# Google ADK Agent Definition
# ---------------------------------------------------------------------------

dlq_agent = Agent(
    name="dlq_resolution_agent",
    description="AI Agent that analyzes IBM MQ & ACE Dead Letter Queue messages, diagnoses root causes, and generates actionable MQSC remediations.",
    model=MODEL_NAME,
    instruction=SYSTEM_INSTRUCTION,
    tools=[get_dlq_messages, test_mq_connection, lookup_mq_reason_code, replay_message_dry_run],
)

# ---------------------------------------------------------------------------
# Execution Engine (Runner) & Streaming
# ---------------------------------------------------------------------------

_SHARED_SESSION_SERVICE = InMemorySessionService()

async def _execute_agent_async(prompt: str, user_id: str = "dlq_operator", session_id: str | None = None) -> str:
    """Run the ADK agent using InMemorySessionService and Runner."""
    runner = Runner(
        agent=dlq_agent,
        app_name="dlq_resolution_console",
        session_service=_SHARED_SESSION_SERVICE,
    )
    if not session_id:
        session = await _SHARED_SESSION_SERVICE.create_session(
            app_name="dlq_resolution_console",
            user_id=user_id,
        )
        session_id = session.id

    new_message = Content(role="user", parts=[Part.from_text(text=prompt)])
    text_chunks: list[str] = []
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=new_message):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    text_chunks.append(part.text)
    output = "".join(text_chunks).strip()
    return output or "The agent analyzed the request but produced an empty response."


async def run_agent_stream_async(prompt: str, user_id: str = "dlq_operator", session_id: str | None = None):
    """Yield chunks of text as the agent generates its response (for SSE streaming)."""
    runner = Runner(
        agent=dlq_agent,
        app_name="dlq_resolution_console",
        session_service=_SHARED_SESSION_SERVICE,
    )
    if not session_id:
        session = await _SHARED_SESSION_SERVICE.create_session(
            app_name="dlq_resolution_console",
            user_id=user_id,
        )
        session_id = session.id

    new_message = Content(role="user", parts=[Part.from_text(text=prompt)])
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=new_message):
        # Stream tool calls if any
        if hasattr(event, "tool_calls") and event.tool_calls:
            for tc in event.tool_calls:
                fn_name = getattr(tc, "name", "") or getattr(getattr(tc, "function", None), "name", "")
                if fn_name:
                    yield f"\n*[Agent calling tool: `{fn_name}`...]*\n"
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    yield part.text


def run_agent_sync(prompt: str) -> dict[str, Any]:
    """Synchronous interface to run the DLQ AI Agent.

    Returns:
        dict with success (bool), analysis (str), and optional error (str).
    """
    _bootstrap_env()
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {
            "success": False,
            "error": "GOOGLE_API_KEY is not configured in .env. Please set your Google Gemini API key to enable AI agent analysis.",
            "configured": False,
        }
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, _execute_agent_async(prompt))
                    analysis_text = future.result(timeout=_agent_cfg.get("timeoutSeconds", 180))
            else:
                analysis_text = loop.run_until_complete(_execute_agent_async(prompt))
        except RuntimeError:
            analysis_text = asyncio.run(_execute_agent_async(prompt))
        return {"success": True, "analysis": analysis_text, "model": MODEL_NAME, "configured": True}
    except Exception as exc:
        err_msg = str(exc)
        if "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg:
            err_msg = "Google Gemini API quota reached. Please check your API quota in Google AI Studio or try again shortly."
        elif "API_KEY_INVALID" in err_msg:
            err_msg = "The provided GOOGLE_API_KEY is invalid. Please verify your key in Google AI Studio."
        return {"success": False, "error": f"AI Agent Error: {err_msg}", "configured": True}


def is_configured() -> bool:
    """Return True if GOOGLE_API_KEY or GEMINI_API_KEY is set in environment."""
    _bootstrap_env()
    return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def get_agent_status() -> dict[str, Any]:
    """Return current configuration and capabilities of the AI Agent."""
    configured = is_configured()
    return {
        "available": True,
        "configured": configured,
        "model": MODEL_NAME,
        "tools": [t.__name__ for t in [get_dlq_messages, test_mq_connection, lookup_mq_reason_code, replay_message_dry_run]],
        "framework": "Google ADK (Agent Development Kit)",
        "platformToolsRegistered": len(shared_platform.list_tools()),
    }
