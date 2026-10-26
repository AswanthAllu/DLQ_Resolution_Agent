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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
SCRIPT_PATH = ROOT / "Invoke-DlqTriage.ps1"
PORT = int(os.environ.get("PORT", "4000"))

DEFAULT_CONFIG = {
    "qmgr": "QMGR2",
    "host": "localhost",
    "port": 1415,
    "channel": "SYSTEM.ADMIN.SVRCONN",
    "dlq": "SYSTEM.DEAD.LETTER.QUEUE",
    "userId": "mqusermchi",
    "password": "",
}
MQ_CONFIG_PATH = ROOT / "mq-config.json"


def parse_env() -> dict:
    """Read the project's .env file using the same rules as server.js."""
    config = DEFAULT_CONFIG.copy()
    env_path = ROOT / ".env"
    if not env_path.exists():
        return config

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "MQ_QMGR":
            config["qmgr"] = value
        elif key == "MQ_HOST":
            config["host"] = value
        elif key == "MQ_PORT":
            try:
                config["port"] = int(value) or 1414
            except ValueError:
                config["port"] = 1414
        elif key == "MQ_CHANNEL":
            config["channel"] = value
        elif key == "MQ_DLQ_NAME":
            config["dlq"] = value
        elif key == "MQ_USER":
            config["userId"] = value
        elif key == "MQ_PASSWORD":
            config["password"] = value
    return config


def load_mq_config() -> dict:
    """Load connection profiles; secrets are intentionally excluded."""
    fallback = {"activeTarget": "local", "targets": [{
        "name": "local", "type": "local", "host": "localhost", "port": 1415,
        "channel": "SYSTEM.ADMIN.SVRCONN", "dlq": "SYSTEM.DEAD.LETTER.QUEUE", "qmgr": ""
    }]}
    try:
        value = json.loads(MQ_CONFIG_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and isinstance(value.get("targets"), list) else fallback
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
            if result.get("success"):
                managers.append({**item, "status": "RUNNING"})
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
    return args


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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

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
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        endpoint = urlparse(self.path).path
        body = request_body(self)
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

        args = mq_args(body, actions[endpoint])
        if endpoint == "/api/replay":
            if body.get("targetMsgId"):
                args += ["-TargetMsgId", str(body["targetMsgId"])]
            args += ["-DryRun:$true" if body.get("dryRun") else "-DryRun:$false"]
            if body.get("forceReplay"):
                args.append("-ForceReplay")
        self.send_json(run_powershell(args))

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
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
