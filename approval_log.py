"""approval_log.py — Human operator approval record for DLQ replay actions.

Appends a TSV entry to the approvals log file defined in config.json → approvals.
Each record stores: timestamp, operator_id, action, message_id, and notes.
This is intentionally simple (flat file) so it requires no extra dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import config_loader

ROOT = Path(__file__).resolve().parent


def _log_path() -> Path:
    cfg = config_loader.approvals()
    return ROOT / cfg.get("logPath", "approvals.log")


def require_approval() -> bool:
    """Return True if config mandates an approval record before replay."""
    return bool(config_loader.approvals().get("requireApprovalForReplay", True))


def record(
    operator_id: str,
    action: str,
    message_id: str,
    notes: str = "",
) -> None:
    """Append one approval entry to the log file.

    Parameters
    ----------
    operator_id : Identifier of the human approver (e.g. username, email, 'system').
    action      : Short label for the action (e.g. 'replay_dry_run', 'replay_live').
    message_id  : IBM MQ message ID (hex string) being acted upon.
    notes       : Optional free-text context.
    """
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Sanitise tab characters in free-text fields to keep TSV parseable.
    def _clean(v: str) -> str:
        return v.replace("\t", " ").replace("\n", " ").strip()

    line = "\t".join([
        timestamp,
        _clean(operator_id) or "unknown",
        _clean(action),
        _clean(message_id),
        _clean(notes),
    ]) + "\n"

    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"[approval_log] WARNING: Could not write approval record: {exc}")


def recent(n: int = 50) -> list[dict]:
    """Return the last *n* approval records as a list of dicts."""
    path = _log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in reversed(lines[-n:]):
        parts = line.split("\t")
        if len(parts) >= 4:
            records.append({
                "timestamp":  parts[0],
                "operatorId": parts[1],
                "action":     parts[2],
                "messageId":  parts[3],
                "notes":      parts[4] if len(parts) > 4 else "",
            })
    return records
