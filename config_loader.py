"""config_loader.py — Single source of truth for all application configuration.

All modules import from here. To change any access point, interval, or setting,
edit config.json. No code changes are required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_CONFIG_PATH = ROOT / "config.json"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_config() -> dict[str, Any]:
    """Load config.json, falling back to safe defaults on any error."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config_loader] WARNING: Could not read config.json: {exc}. Using built-in defaults.")
        return {}


def _merge(base: dict, override: dict) -> dict:
    """Shallow-merge *override* keys into *base* (one level deep)."""
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = {**result[k], **v}
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Raw config (lazy-loaded once per process)
# ---------------------------------------------------------------------------

_raw: dict[str, Any] | None = None


def raw() -> dict[str, Any]:
    """Return the full parsed config.json as a dict (cached)."""
    global _raw
    if _raw is None:
        _raw = _read_config()
    return _raw


def reload() -> dict[str, Any]:
    """Force a fresh reload of config.json (useful after edits)."""
    global _raw
    _raw = _read_config()
    return _raw


# ---------------------------------------------------------------------------
# Typed accessors — each returns a plain Python dict / scalar
# ---------------------------------------------------------------------------

def server() -> dict[str, Any]:
    return _merge({"port": 4000, "host": "127.0.0.1"}, raw().get("server", {}))


def mq() -> dict[str, Any]:
    return _merge(
        {
            "activeTarget": "local",
            "targets": [],
            "credentials": {"userId": "", "password": ""},
        },
        raw().get("mq", {}),
    )


def mq_credentials() -> dict[str, str]:
    creds = mq().get("credentials", {})
    # Env vars always win (so .env still works as an override)
    return {
        "userId": os.environ.get("MQ_USER", creds.get("userId", "")),
        "password": os.environ.get("MQ_PASSWORD", creds.get("password", "")),
    }


def mq_active_target() -> dict[str, Any]:
    """Return the currently active MQ connection profile."""
    cfg = mq()
    active = cfg.get("activeTarget", "")
    for t in cfg.get("targets", []):
        if isinstance(t, dict) and t.get("name") == active:
            return t
    targets = [t for t in cfg.get("targets", []) if isinstance(t, dict)]
    return targets[0] if targets else {}


def monitoring() -> dict[str, Any]:
    return _merge(
        {
            "pollIntervalSeconds": 15,
            "minIntervalSeconds": 5,
            "maxIntervalSeconds": 300,
            "maxBrowseMessages": 500,
            "maxRetainedMessages": 200,
            "maxAlerts": 50,
        },
        raw().get("monitoring", {}),
    )


def agent() -> dict[str, Any]:
    return _merge(
        {"model": "gemini-3.5-flash", "timeoutSeconds": 180},
        raw().get("agent", {}),
    )


def ace() -> dict[str, Any]:
    return _merge(
        {
            "integrationNode": "",
            "serverName": "",
            "remoteHost": "",
            "remoteUser": "",
            "eventLogSource": "IBM App Connect Enterprise",
            "lookbackMinutes": 60,
        },
        raw().get("ace", {}),
    )


def duplicate_check() -> dict[str, Any]:
    return _merge(
        {"dbPath": "dlq-audit.sqlite3", "tableName": "processed_messages"},
        raw().get("duplicateCheck", {}),
    )


def approvals() -> dict[str, Any]:
    return _merge(
        {"logPath": "approvals.log", "requireApprovalForReplay": True},
        raw().get("approvals", {}),
    )


def platform() -> dict[str, Any]:
    return _merge(
        {"enabled": False, "baseUrl": "", "apiKey": "", "timeoutSeconds": 10},
        raw().get("platform", {}),
    )


def runbooks() -> dict[str, str]:
    return raw().get("runbooks", {})


# ---------------------------------------------------------------------------
# Convenience: Google API key (env > config)
# ---------------------------------------------------------------------------

def google_api_key() -> str:
    return (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    )
