"""IBM MQ & ACE DLQ Resolution Console web server.

This is the Python equivalent of server.js.  The browser API and the
PowerShell MQ engine are intentionally kept compatible with the original
implementation so the existing UI and MQ behaviour do not change.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Import AI Agent module (Google ADK)
try:
    import agent
    _AGENT_AVAILABLE = True
except Exception as _agent_err:
    print(f"[server.py] Warning: Could not load agent module: {_agent_err}")
    agent = None  # type: ignore
    _AGENT_AVAILABLE = False


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
SCRIPT_PATH = ROOT / "Invoke-DlqTriage.ps1"

# ---------------------------------------------------------------------------
# All tunable values come from config.json via config_loader
# ---------------------------------------------------------------------------
try:
    import config_loader as _cfg
    import approval_log as _approval_log
    _server_cfg = _cfg.server()
    PORT = int(os.environ.get("PORT", str(_server_cfg.get("port", 4000))))
    SERVER_HOST = _server_cfg.get("host", "127.0.0.1")
    _mon_cfg = _cfg.monitoring()
except Exception as _cfg_err:
    print(f"[server.py] Warning: config_loader unavailable: {_cfg_err}. Using built-in defaults.")
    _cfg = None  # type: ignore
    _approval_log = None  # type: ignore
    PORT = int(os.environ.get("PORT", "4000"))
    SERVER_HOST = "127.0.0.1"
    _mon_cfg = {}

MQ_CONFIG_PATH = ROOT / "config.json"  # config.json is now the single config file

DEFAULT_CONFIG = {
    "qmgr": "QMGR2",
    "host": "localhost",
    "port": 1415,
    "channel": "SYSTEM.ADMIN.SVRCONN",
    "dlq": "SYSTEM.DEAD.LETTER.QUEUE",
    "userId": "",
    "password": "",
}


def parse_env() -> dict:
    """Read configuration prioritizing config.json then .env file."""
    config = DEFAULT_CONFIG.copy()

    # 1. Read from config.json first (user-saved configuration)
    try:
        cfg_path = ROOT / "config.json"
        if cfg_path.exists():
            cdata = json.loads(cfg_path.read_text(encoding="utf-8"))
            mq_data = cdata.get("mq", {})
            active_target = mq_data.get("activeTarget", "local")
            for t in mq_data.get("targets", []):
                if isinstance(t, dict) and (t.get("name") == active_target or t.get("name") == "active"):
                    if t.get("qmgr"): config["qmgr"] = t["qmgr"]
                    if t.get("host"): config["host"] = t["host"]
                    if t.get("port"): config["port"] = int(t["port"])
                    if t.get("channel"): config["channel"] = t["channel"]
                    if t.get("dlq"): config["dlq"] = t["dlq"]
                    break
            creds = mq_data.get("credentials", {})
            if creds.get("userId"): config["userId"] = creds["userId"]
            if creds.get("password"): config["password"] = creds["password"]
    except Exception as exc:
        print(f"[server.py] Warning reading config.json in parse_env: {exc}")

    # 2. Fall back to .env for keys not set in config.json
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "MQ_QMGR" and not config.get("qmgr"):
                config["qmgr"] = value
            elif key == "MQ_HOST" and not config.get("host"):
                config["host"] = value
            elif key == "MQ_PORT" and not config.get("port"):
                try:
                    config["port"] = int(value) or 1414
                except ValueError:
                    config["port"] = 1414
            elif key == "MQ_CHANNEL" and not config.get("channel"):
                config["channel"] = value
            elif key == "MQ_DLQ_NAME" and not config.get("dlq"):
                config["dlq"] = value
            elif key == "MQ_USER" and not config.get("userId"):
                config["userId"] = value
            elif key == "MQ_PASSWORD" and not config.get("password"):
                config["password"] = value
            elif key == "GOOGLE_API_KEY":
                config["googleApiKeySet"] = bool(value.strip())
    return config


def load_mq_config() -> dict:
    """Load connection profiles from config.json (mq section); secrets are intentionally excluded."""
    fallback = {"activeTarget": "local", "targets": [{
        "name": "local", "type": "local", "host": "localhost", "port": 1415,
        "channel": "SYSTEM.ADMIN.SVRCONN", "dlq": "SYSTEM.DEAD.LETTER.QUEUE", "qmgr": ""
    }]}
    try:
        if _cfg is not None:
            mq = _cfg.mq()
            return {k: v for k, v in mq.items() if k != "credentials"}  # strip credentials
        value = json.loads(MQ_CONFIG_PATH.read_text(encoding="utf-8"))
        mq = value.get("mq", value)
        return mq if isinstance(mq, dict) and isinstance(mq.get("targets"), list) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def run_powershell(args: list[str]) -> dict:
    """Run the existing MQ engine and return its JSON result.

    stdout is expected to be a single JSON object produced by -AsJson.
    stderr is captured separately and included in error responses so that
    IBM MQ .NET exceptions (which PowerShell writes to stderr) surface in
    the browser instead of silently disappearing.
    """
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT_PATH),
        *args,
        "-AsJson",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        raw = completed.stdout.strip()
        stderr = completed.stderr.strip()
        # Log PowerShell stderr to the server console for operator visibility.
        if stderr:
            print(f"[PowerShell stderr] {stderr}")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Build a helpful error message that includes both stdout and stderr
            # so the browser can display IBM MQ exception details.
            detail = raw or stderr or "Unknown PowerShell execution failure"
            return {"success": False, "error": detail}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "PowerShell triage script timed out after 300 s. The Queue Manager may be unreachable."}
    except OSError as exc:
        return {"success": False, "error": f"Failed to launch PowerShell: {exc}"}


def running_queue_managers() -> dict:
    """Return only queue managers reported as RUNNING by IBM MQ."""
    import re
    try:
        result = subprocess.run(
            ["dspmq", "-o", "status"], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "queueManagers": [], "error": str(exc)}
    managers = []
    for line in result.stdout.splitlines():
        match = re.search(r"QMNAME\(([^)]*)\).*STATUS\(([^)]*)\)", line, re.IGNORECASE)
        if match and match.group(2).upper() == "RUNNING":
            managers.append({"name": match.group(1)})
    return {"success": True, "queueManagers": managers}


def configured_targets() -> dict:
    """Return local running managers and reachable configured remote managers."""
    profiles = load_mq_config()
    env = parse_env()
    managers = []
    for target in profiles["targets"]:
        if not isinstance(target, dict):
            continue
        item = {
            "name": target.get("name") or target.get("qmgr"),
            "profileName": target.get("name") or target.get("qmgr"),
            "qmgr": target.get("qmgr", ""),
            "host": target.get("host", env["host"]),
            "port": target.get("port", env["port"]),
            "channel": target.get("channel", env["channel"]),
            "dlq": target.get("dlq", env["dlq"]),
            "type": target.get("type", "remote")
        }
        if item["type"].lower() == "local":
            for local in running_queue_managers().get("queueManagers", []):
                managers.append({**item, "name": local["name"], "qmgr": local["name"], "status": "RUNNING"})
        elif item["qmgr"]:
            credentials = {"userId": env.get("userId", ""), "password": env.get("password", "")}
            result = run_powershell(mq_args({**item, **credentials}, "TestConnection"))
            managers.append({
                **item,
                "status": "RUNNING" if result.get("success") else "UNREACHABLE",
                "connectionError": None if result.get("success") else result.get("error"),
            })
    # Always return the complete connection profile. This prevents a stale
    # browser or an older server response from losing host/port/channel/DLQ.
    return {"success": True, "activeTarget": profiles.get("activeTarget", "local"), "queueManagers": managers}


def request_body(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not length:
            return {}
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def mq_args(body: dict, action: str) -> list[str]:
    # Keep the JavaScript defaults and argument names exactly intact.
    args = [
        "-Action", action,
        "-TargetQMgr", body.get("qmgr") or "QMGR2",
        "-HostNameParam", body.get("host") or "localhost",
        "-PortParam", str(body.get("port") or 1415),
        "-ChannelParam", body.get("channel") or "SYSTEM.ADMIN.SVRCONN",
        "-DlqParam", body.get("dlq") or "SYSTEM.DEAD.LETTER.QUEUE",
    ]
    if body.get("userId"):
        args += ["-UserParam", str(body["userId"])]
    if body.get("password"):
        args += ["-PasswordParam", str(body["password"])]
    if action == "Triage" and body.get("maxMessages"):
        args += ["-MaxMessages", str(body["maxMessages"])]
    return args


class DlqMonitor:
    """Poll one IBM MQ DLQ and retain recent arrivals for operator review."""

    # Defaults — overridden by config.json → monitoring at runtime
    POLL_INTERVAL_SECONDS = _mon_cfg.get("pollIntervalSeconds", 15)
    MAX_BROWSE_MESSAGES   = _mon_cfg.get("maxBrowseMessages", 500)
    MAX_RETAINED_MESSAGES = _mon_cfg.get("maxRetainedMessages", 200)
    MAX_ALERTS            = _mon_cfg.get("maxAlerts", 50)

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._enabled = False
        self._config: dict = {}
        self._interval = self.POLL_INTERVAL_SECONDS
        self._generation = 0
        self._worker: Optional[threading.Thread] = None
        self._active_keys: set[str] = set()
        self._message_history: dict[str, dict] = {}
        self._next_alert_id = 0
        self._status = self._empty_status()

    @staticmethod
    def _empty_status() -> dict:
        return {
            "running": False,
            "state": "stopped",
            "detectedCount": 0,
            "sessionId": None,
            "currentDepth": None,
            "lastChecked": None,
            "error": None,
            "messages": [],
            "alerts": [],
            "truncated": False,
        }

    @staticmethod
    def _message_key(message: dict) -> str:
        message_id = str(message.get("msgId") or "").strip()
        if message_id:
            return message_id
        return "|".join(str(message.get(field) or "") for field in (
            "correlId", "putDateTime", "targetQueue", "index"
        ))

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def snapshot(self) -> dict:
        with self._condition:
            return deepcopy(self._status)

    def start(self, body: dict, secrets: dict) -> dict:
        config = {
            "qmgr": str(body.get("qmgr") or "").strip(),
            "host": str(body.get("host") or "").strip(),
            "channel": str(body.get("channel") or "").strip(),
            "dlq": str(body.get("dlq") or "").strip(),
        }
        try:
            config["port"] = int(body.get("port"))
            interval = int(body.get("intervalSeconds") or self.POLL_INTERVAL_SECONDS)
        except (TypeError, ValueError):
            raise ValueError("Port and polling interval must be valid integers.")
        if not config["qmgr"] or not config["host"] or not config["channel"] or not config["dlq"]:
            raise ValueError("Queue manager, host, channel, and DLQ are required.")
        if not 1 <= config["port"] <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        if not 5 <= interval <= 300:
            raise ValueError("Polling interval must be between 5 and 300 seconds.")
        config["userId"] = str(body.get("userId") or secrets.get("userId") or "")
        config["password"] = str(body.get("password") or secrets.get("password") or "")

        with self._condition:
            if self._enabled and self._config == config and self._interval == interval:
                return deepcopy(self._status)

            self._generation += 1
            self._enabled = True
            self._config = config
            self._interval = interval
            self._active_keys.clear()
            self._message_history.clear()
            self._status = self._empty_status()
            self._status.update({
                "running": True,
                "state": "starting",
                "sessionId": uuid.uuid4().hex,
                "qmgr": config["qmgr"],
                "dlq": config["dlq"],
                "pollIntervalSeconds": interval,
            })
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="ibm-mq-dlq-monitor", daemon=True
                )
                self._worker.start()
            self._condition.notify_all()
            return deepcopy(self._status)

    def stop(self) -> dict:
        with self._condition:
            self._enabled = False
            self._generation += 1
            self._status["running"] = False
            self._status["state"] = "stopped"
            self._status["error"] = None
            self._condition.notify_all()
            return deepcopy(self._status)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._enabled:
                    self._condition.wait()
                config = deepcopy(self._config)
                generation = self._generation
                interval = self._interval
                self._status["state"] = "checking"
                self._status["lastAttempt"] = self._timestamp()

            scan_started_at = time.monotonic()
            result = run_powershell(mq_args({
                **config,
                "maxMessages": self.MAX_BROWSE_MESSAGES,
            }, "Triage"))

            with self._condition:
                if not self._enabled or generation != self._generation:
                    continue
                if not result.get("success"):
                    self._status.update({
                        "state": "error",
                        "error": result.get("error") or "DLQ monitoring failed.",
                    })
                else:
                    current_messages = [
                        message for message in result.get("messages", [])
                        if isinstance(message, dict)
                    ]
                    current_keys = {self._message_key(message) for message in current_messages}
                    newly_detected = [
                        message for message in current_messages
                        if self._message_key(message) not in self._active_keys
                    ]
                    for message in newly_detected:
                        key = self._message_key(message)
                        self._next_alert_id += 1
                        message["monitorKey"] = key
                        message["monitorStatus"] = "Awaiting human review"
                        self._message_history.pop(key, None)
                        self._message_history[key] = message
                        self._status["alerts"].append({
                            "id": self._next_alert_id,
                            "timestamp": self._timestamp(),
                            "qmgr": config["qmgr"],
                            "messageId": message.get("msgId") or "",
                            "messageKey": key,
                            "targetQueue": message.get("targetQueue") or "Unknown destination",
                            "reasonCode": message.get("reasonCode"),
                            "reasonName": message.get("reasonName") or "Unknown reason",
                        })
                    for key, message in self._message_history.items():
                        if key in current_keys:
                            message["monitorStatus"] = "Awaiting human review"
                        else:
                            message["monitorStatus"] = "No longer in DLQ"
                    self._active_keys = current_keys
                    while len(self._message_history) > self.MAX_RETAINED_MESSAGES:
                        self._message_history.pop(next(iter(self._message_history)))
                    self._status["alerts"] = self._status["alerts"][-self.MAX_ALERTS:]
                    self._status.update({
                        "state": "monitoring",
                        "error": None,
                        "currentDepth": result.get("curDepth", 0),
                        "lastChecked": self._timestamp(),
                        "detectedCount": self._status["detectedCount"] + len(newly_detected),
                        "messages": list(reversed(list(self._message_history.values()))),
                        "truncated": int(result.get("curDepth") or 0) > len(current_messages),
                    })

                self._condition.wait_for(
                    lambda: not self._enabled or generation != self._generation,
                    timeout=max(0.0, interval - (time.monotonic() - scan_started_at)),
                )


dlq_monitor = DlqMonitor()


def _save_api_key_to_env(api_key: str) -> bool:
    """Save the Google API key into .env and update os.environ."""
    env_path = ROOT / ".env"
    api_key = api_key.strip()
    try:
        lines = []
        found = False
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("GOOGLE_API_KEY="):
                    lines.append(f"GOOGLE_API_KEY={api_key}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"GOOGLE_API_KEY={api_key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["GOOGLE_API_KEY"] = api_key
        os.environ["GEMINI_API_KEY"] = api_key
        return True
    except Exception as exc:
        print(f"[server.py] Error saving API key: {exc}")
        return False


def _build_agent_prompt(cfg: dict, messages: list[dict], custom_query: str = "") -> str:
    """Construct an in-depth analysis prompt for the AI Agent."""
    qmgr = cfg.get("qmgr") or "QMGR2"
    host = cfg.get("host") or "localhost"
    port = cfg.get("port") or 1415
    channel = cfg.get("channel") or "SYSTEM.ADMIN.SVRCONN"
    dlq = cfg.get("dlq") or "SYSTEM.DEAD.LETTER.QUEUE"

    parts = [
        f"# Dead Letter Queue Triage Request for Queue Manager `{qmgr}`",
        "",
        f"- **Queue Manager**: `{qmgr}` (`{host}:{port}`)",
        f"- **Channel**: `{channel}`",
        f"- **Dead Letter Queue**: `{dlq}`",
        f"- **Candidate Message Count**: {len(messages)}",
        "",
    ]

    if custom_query:
        parts.append(f"**Operator Inquiry**: {custom_query}\n")

    if not messages:
        parts.append(
            "There are currently 0 messages in the Dead Letter Queue. "
            "Please provide a general readiness check, explaining DLQ monitoring best practices, "
            "common DLH error conditions (such as 2085, 2035, 2053), and recommended runmqsc prevention rules."
        )
        return "\n".join(parts)

    parts.append("## DLQ Messages Identified for Analysis:")
    parts.append("")

    for i, m in enumerate(messages, 1):
        has_dlh = m.get("hasDlh") is True
        target_q = m.get("targetQueue") or ("[unknown: no MQDLH]" if not has_dlh else "[blank in MQDLH]")
        target_qm = m.get("targetQMgr") or ("[unknown: no MQDLH]" if not has_dlh else "[blank in MQDLH]")
        reason_code = m.get("reasonCode") if has_dlh else "[unknown: no MQDLH]"
        reason_name = m.get("reasonName") if has_dlh else "[unknown: no MQDLH]"
        failure_class = m.get("failureClass") or "Unknown"
        reason_desc = m.get("reasonDesc") or ""
        msg_id = m.get("msgId") or "N/A"
        correl_id = m.get("correlId") or "N/A"
        put_time = m.get("putDateTime") or "N/A"
        orig_app = m.get("originalApp") or "N/A"
        backout = m.get("backoutCount", 0)
        fmt = m.get("format") or "MQDEAD"
        payload_text = m.get("payloadText")
        payload_len = m.get("payloadLength")
        if payload_text is None:
            payload_text = "[Not supplied by triage]"
        elif payload_text == "" and payload_len == 0:
            payload_text = "[Triage reports zero-length payload]"

        parts.append(f"### Message #{i} (Index: {m.get('index', i)})")
        parts.append(f"- **MQDLH Detected**: {has_dlh}")
        parts.append(f"- **Message ID**: `{msg_id}`")
        parts.append(f"- **Correlation ID**: `{correl_id}`")
        parts.append(f"- **Intended Destination**: `{target_q}` on `{target_qm}`")
        parts.append(f"- **MQ Reason Code**: `{reason_code}` (`{reason_name}`)")
        parts.append(f"- **Failure Classification**: `{failure_class}`")
        parts.append(f"- **Reason Description**: {reason_desc}")
        parts.append(f"- **Put Timestamp**: {put_time}")
        parts.append(f"- **Original Application**: `{orig_app}`")
        parts.append(f"- **Backout Count**: {backout}")
        parts.append(f"- **MQMD Format**: `{fmt}`")
        parts.append(f"- **CCSID**: {m.get('ccsid', 'N/A')} | **Encoding**: {m.get('encoding', 'N/A')}")
        parts.append(f"- **MessageType**: {m.get('messageType', 'N/A')} | **Persistence**: {m.get('persistence', 'N/A')}")
        parts.append(f"- **ReplyToQueue**: `{m.get('replyToQueue') or 'None'}` on `{m.get('replyToQMgr') or 'None'}`")
        parts.append(f"- **Format after MQDLH**: `{m.get('originalFormat') or '[not supplied]'}`")
        parts.append(
            "- **Payload length reported by triage**: "
            f"{payload_len if payload_len is not None else '[not supplied]'}"
        )
        parts.append("- **Payload Preview (MQDLH stripped)**:")
        parts.append(f"```text\n{str(payload_text)[:600]}\n```")

        ace_events = m.get("aceEvents") or []
        if ace_events:
            parts.append("- **Correlated ACE / Broker Events**:")
            for ev in ace_events:
                parts.append(f"  * Event ID `{ev.get('id')}` at {ev.get('time')}: {ev.get('message')}")

        parts.append("")

    parts.append("Follow the required concise per-message output sections and safety rules from your system instructions.")

    return "\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    server_version = "IntegrationMQ-Python/1.0"

    def send_json(self, value: dict, status: int = 200) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.end_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def end_cors(self) -> None:
        return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            self.send_response(200)
            self.end_cors()
            config = parse_env()
            config.pop("password", None)
            payload = json.dumps(config).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/queue-managers":
            self.send_json(configured_targets())
            return
        if parsed.path == "/api/monitor/status":
            self.send_json(dlq_monitor.snapshot())
            return
        if parsed.path == "/api/agent/status":
            if _AGENT_AVAILABLE and agent:
                self.send_json(agent.get_agent_status())
            else:
                self.send_json({"available": False, "configured": False, "error": "Agent module unavailable"})
            return
        # Recent approval records (read-only audit view)
        if parsed.path == "/api/approvals":
            if _approval_log:
                self.send_json({"success": True, "approvals": _approval_log.recent(50)})
            else:
                self.send_json({"success": False, "error": "Approval log module unavailable"})
            return
        # Hot-reload config.json without restarting the server
        if parsed.path == "/api/config/reload":
            if _cfg:
                _cfg.reload()
                self.send_json({"success": True, "message": "config.json reloaded."})
            else:
                self.send_json({"success": False, "error": "config_loader unavailable"})
            return

        # UC2 Architecture Endpoints (Figure 2)
        if parsed.path == "/api/cases":
            import case_store
            self.send_json({"success": True, "cases": case_store.list_cases(50)})
            return

        if parsed.path == "/api/guardrails/status":
            import guardrails
            self.send_json({
                "success": True,
                "killSwitch": guardrails.is_kill_switch_active(),
                "pendingApprovals": guardrails.list_pending_approvals(),
            })
            return

        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        endpoint = urlparse(self.path).path
        body = request_body(self)

        if endpoint == "/api/monitor/start":
            try:
                status = dlq_monitor.start(body, parse_env())
            except ValueError as exc:
                self.send_json({"success": False, "error": str(exc)}, 400)
                return
            self.send_json({"success": True, **status})
            return
        if endpoint == "/api/monitor/stop":
            self.send_json({"success": True, **dlq_monitor.stop()})
            return

        # Agent save key endpoint
        if endpoint == "/api/agent/save-key":
            key = body.get("apiKey", "")
            if not key:
                self.send_json({"success": False, "error": "API key cannot be empty."})
                return
            saved = _save_api_key_to_env(key)
            self.send_json({"success": saved, "configured": bool(key)})
            return

        # Agent status endpoint (POST support)
        if endpoint == "/api/agent/status":
            if _AGENT_AVAILABLE and agent:
                self.send_json(agent.get_agent_status())
            else:
                self.send_json({"available": False, "configured": False, "error": "Agent module unavailable"})
            return

        # Agent analysis endpoint
        if endpoint == "/api/agent/analyze":
            if not _AGENT_AVAILABLE or not agent:
                self.send_json({
                    "success": False,
                    "error": "Google ADK Agent module is not available on this server.",
                })
                return

            # Check if key is configured
            if not agent.is_configured():
                self.send_json({
                    "success": False,
                    "error": "GOOGLE_API_KEY is not configured in .env. Please set your key to enable AI analysis.",
                    "configured": False,
                })
                return

            messages = body.get("messages", [])
            cfg = body.get("config", {})
            custom_query = body.get("customQuery", "")
            stream_requested = bool(body.get("stream", False))

            # If messages are not supplied in the request body, fetch them directly via triage
            if not messages and cfg.get("qmgr"):
                secrets = parse_env()
                cfg.setdefault("userId", secrets.get("userId", ""))
                cfg.setdefault("password", secrets.get("password", ""))
                triage_res = run_powershell(mq_args(cfg, "Triage"))
                if triage_res.get("success"):
                    messages = triage_res.get("messages", [])

            prompt = _build_agent_prompt(cfg, messages, custom_query)

            if stream_requested:
                self.send_response(200)
                self.end_cors()
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                import asyncio
                async def _stream_loop():
                    try:
                        async for chunk in agent.run_agent_stream_async(prompt):
                            data_line = f"data: {json.dumps({'chunk': chunk})}\n\n".encode("utf-8")
                            self.wfile.write(data_line)
                            self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception as err:
                        err_line = f"data: {json.dumps({'error': str(err)})}\n\n".encode("utf-8")
                        self.wfile.write(err_line)
                        self.wfile.flush()

                asyncio.run(_stream_loop())
                return

            result = agent.run_agent_sync(prompt)
            self.send_json(result)
            return

        # UC2 Orchestrator Pipeline Endpoint (9-Step Pipeline)
        if endpoint == "/api/pipeline/run":
            import orchestrator
            secrets = parse_env()
            cfg = body.get("config", {})
            cfg.setdefault("userId", secrets.get("userId", ""))
            cfg.setdefault("password", secrets.get("password", ""))

            res = orchestrator.orchestrator.run_pipeline(
                trigger_type=body.get("triggerType", "manual_triage"),
                config=cfg,
                target_msg_id=body.get("targetMsgId", ""),
                order_id=body.get("orderId", ""),
                custom_query=body.get("customQuery", ""),
                operator_id=body.get("operatorId", "operator"),
            )
            self.send_json(res)
            return

        # Guardrails Approval Gate Decision Endpoint
        if endpoint == "/api/guardrails/decide":
            import guardrails
            import orchestrator
            case_id = body.get("caseId", "")
            decision = body.get("decision", "REJECTED")
            operator_id = body.get("operatorId", "approver")
            reason = body.get("reason", "")

            ok, msg = guardrails.decide_approval(case_id, decision, operator_id, reason)
            if not ok:
                self.send_json({"success": False, "error": msg}, 400)
                return

            if decision == "APPROVED":
                secrets = parse_env()
                cfg = body.get("config", {})
                cfg.setdefault("userId", secrets.get("userId", ""))
                cfg.setdefault("password", secrets.get("password", ""))
                exec_res = orchestrator.orchestrator.execute_approved_remediation(case_id, operator_id, cfg)
                self.send_json({"success": True, "message": msg, "execution": exec_res})
            else:
                self.send_json({"success": True, "message": msg})
            return

        # Guardrails Kill-Switch Toggle Endpoint
        if endpoint == "/api/guardrails/kill-switch":
            import guardrails
            active = bool(body.get("active", False))
            new_state = guardrails.set_kill_switch(active)
            self.send_json({"success": True, "killSwitch": new_state})
            return

        # Save configuration to config.json (persists user changes)
        if endpoint == "/api/config/save":
            try:
                cfg_path = ROOT / "config.json"
                data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
                mq_section = data.setdefault("mq", {})
                targets = mq_section.setdefault("targets", [])
                
                # Update or add profile
                qmgr = str(body.get("qmgr", "")).strip()
                host = str(body.get("host", "localhost")).strip()
                port = int(body.get("port", 1415))
                channel = str(body.get("channel", "SYSTEM.ADMIN.SVRCONN")).strip()
                dlq = str(body.get("dlq", "SYSTEM.DEAD.LETTER.QUEUE")).strip()
                user_id = str(body.get("userId", "")).strip()
                password = str(body.get("password", "")).strip()
                
                # Store in targets
                found = False
                for t in targets:
                    if isinstance(t, dict) and (t.get("name") == "active" or t.get("qmgr") == qmgr):
                        t.update({"name": "active", "type": "remote", "host": host, "port": port, "channel": channel, "dlq": dlq, "qmgr": qmgr})
                        found = True
                        break
                if not found:
                    targets.insert(0, {"name": "active", "type": "remote", "host": host, "port": port, "channel": channel, "dlq": dlq, "qmgr": qmgr})
                
                mq_section["activeTarget"] = "active"
                creds = mq_section.setdefault("credentials", {})
                creds["userId"] = user_id
                if password:
                    creds["password"] = password
                
                cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                if _cfg:
                    _cfg.reload()
                self.send_json({"success": True, "message": "Configuration saved to config.json"})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
            return

        # Credentials stay on the server. If the browser does not provide
        # them, use the secrets from .env without exposing the password via
        # /api/config or the dashboard.
        secrets = parse_env()
        body.setdefault("userId", secrets.get("userId", ""))
        body.setdefault("password", secrets.get("password", ""))
        actions = {
            "/api/test-connection": "TestConnection",
            "/api/triage": "Triage",
            "/api/replay": "Replay",
        }
        if endpoint not in actions:
            self.send_json({"error": "Not Found"}, 404)
            return

        if endpoint == "/api/replay":
            target_msg_id = str(body.get("targetMsgId") or "")
            if not body.get("dryRun"):
                message = (
                    "Live replay is disabled: authenticated approval, trusted duplicate "
                    "verification, downstream-health evidence, and post-replay verification "
                    "are not integrated."
                )
                self.send_json({"success": False, "error": message}, 403)
                return
            if not target_msg_id:
                self.send_json(
                    {"success": False, "error": "Dry-run requires one specific targetMsgId."},
                    400,
                )
                return
            if body.get("forceReplay"):
                self.send_json(
                    {"success": False, "error": "Force replay is disabled; safety evidence cannot be overridden."},
                    400,
                )
                return

        args = mq_args(body, actions[endpoint])
        if endpoint == "/api/replay":
            args += ["-TargetMsgId", str(body["targetMsgId"]), "-DryRun:$true"]
        result = run_powershell(args)
        self.send_json(result)

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        candidate = (PUBLIC_DIR / relative).resolve()
        try:
            candidate.relative_to(PUBLIC_DIR.resolve())
        except ValueError:
            self.send_error(403, "Access Denied")
            return
        if not candidate.is_file():
            self.send_error(404, "File Not Found")
            return
        data = candidate.read_bytes()
        content_type = mimetypes.types_map.get(candidate.suffix.lower(), "application/octet-stream")
        if candidate.suffix.lower() == ".js":
            content_type = "application/javascript; charset=UTF-8"
        self.send_response(200)
        self.end_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    print("========================================================")
    print("IBM MQ & ACE DLQ Resolution Console")
    print(f"URL: http://localhost:{PORT}")
    print("========================================================")
    ThreadingHTTPServer((SERVER_HOST, PORT), Handler).serve_forever()
