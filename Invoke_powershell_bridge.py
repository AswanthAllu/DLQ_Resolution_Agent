"""Invoke_powershell_bridge.py — Helper to run Invoke-DlqTriage.ps1 cleanly."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SCRIPT_PATH = ROOT / "Invoke-DlqTriage.ps1"


def run_ps(args: list[str]) -> dict[str, Any]:
    """Execute Invoke-DlqTriage.ps1 with args and return parsed JSON."""
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
            timeout=180,
        )
        raw = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if stderr:
            print(f"[PowerShell stderr] {stderr}")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            detail = raw or stderr or "Unknown PowerShell failure"
            return {"success": False, "error": detail}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
