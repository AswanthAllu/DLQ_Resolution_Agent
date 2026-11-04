"""shared_platform.py — Lightweight shared-platform integration stub.

Provides a minimal in-process tool registry so the DLQ agent's tools can be
discovered and invoked by an external orchestrator.  The registry reads its
configuration from config.json → platform section.

When platform.enabled is false (default) the module operates in local-only
mode: tools are registered in memory and callable directly without any HTTP
calls.  Set platform.enabled = true and supply baseUrl + apiKey in config.json
to forward registrations and invocations to a real shared-platform endpoint.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import config_loader

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process registry
# ---------------------------------------------------------------------------

_registry: dict[str, Callable] = {}


def register_tool(name: str, fn: Callable) -> None:
    """Register *fn* under *name* in the local tool registry.

    If the shared platform is enabled (config.json → platform.enabled = true)
    this also pushes the tool descriptor to the platform endpoint.
    """
    _registry[name] = fn
    logger.debug("[shared_platform] Registered tool: %s", name)

    cfg = config_loader.platform()
    if not cfg.get("enabled"):
        return

    # --- remote registration (best-effort; never raises) ---
    try:
        import urllib.request
        payload = json.dumps({"name": name, "description": getattr(fn, "__doc__", "")}).encode()
        req = urllib.request.Request(
            url=cfg["baseUrl"].rstrip("/") + "/tools/register",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.get('apiKey', '')}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=cfg.get("timeoutSeconds", 10)):
            pass
        logger.info("[shared_platform] Tool '%s' registered with platform.", name)
    except Exception as exc:
        logger.warning("[shared_platform] Could not register '%s' with platform: %s", name, exc)


def invoke_tool(name: str, payload: dict[str, Any]) -> Any:
    """Invoke a registered tool by *name* with *payload* as keyword arguments.

    Falls back gracefully if the tool is not registered.
    """
    fn = _registry.get(name)
    if fn is None:
        return {"success": False, "error": f"Tool '{name}' is not registered."}
    try:
        return fn(**payload)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def list_tools() -> list[dict[str, str]]:
    """Return a list of registered tool descriptors."""
    return [
        {"name": name, "description": (getattr(fn, "__doc__", "") or "").split("\n")[0].strip()}
        for name, fn in _registry.items()
    ]
