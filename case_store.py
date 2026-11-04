"""case_store.py — Shared Platform Case Store for UC2 DLQ Resolution Agent.

Stores incidents, evidence snapshots, diagnostic findings, root cause analysis,
and remediation outcomes. Backed by SQLite for persistence across restarts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config_loader

ROOT = Path(__file__).resolve().parent


def _db_path() -> Path:
    cfg = config_loader.duplicate_check()
    return ROOT / cfg.get("dbPath", "dlq-audit.sqlite3")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cases (
            case_id        TEXT PRIMARY KEY,
            trigger_type   TEXT NOT NULL,
            qmgr           TEXT NOT NULL,
            dlq_name       TEXT NOT NULL,
            message_id     TEXT DEFAULT '',
            correl_id      TEXT DEFAULT '',
            order_id       TEXT DEFAULT '',
            reason_code    INTEGER DEFAULT 0,
            reason_name    TEXT DEFAULT '',
            failure_class  TEXT DEFAULT 'Unknown',
            root_cause     TEXT DEFAULT '',
            remediation    TEXT DEFAULT '',
            status         TEXT NOT NULL,
            guardrail_state TEXT DEFAULT 'APPROVED',
            evidence_json  TEXT DEFAULT '{}',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def create_case(
    trigger_type: str,
    qmgr: str,
    dlq_name: str,
    message_id: str = "",
    correl_id: str = "",
    order_id: str = "",
    reason_code: int = 0,
    reason_name: str = "",
    failure_class: str = "Unknown",
    root_cause: str = "",
    remediation: str = "",
    status: str = "OPEN",
    guardrail_state: str = "PENDING_APPROVAL",
    evidence: Optional[dict[str, Any]] = None,
) -> str:
    """Create a new case entry in the Case Store and return case_id."""
    case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    evidence_str = json.dumps(evidence or {})

    conn = _get_conn()
    conn.execute(
        """INSERT INTO cases (
            case_id, trigger_type, qmgr, dlq_name, message_id, correl_id,
            order_id, reason_code, reason_name, failure_class, root_cause,
            remediation, status, guardrail_state, evidence_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            case_id,
            trigger_type,
            qmgr,
            dlq_name,
            message_id,
            correl_id,
            order_id,
            reason_code,
            reason_name,
            failure_class,
            root_cause,
            remediation,
            status,
            guardrail_state,
            evidence_str,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()
    return case_id


def update_case(
    case_id: str,
    status: Optional[str] = None,
    guardrail_state: Optional[str] = None,
    root_cause: Optional[str] = None,
    remediation: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> bool:
    """Update an existing case's state, findings, or guardrail status."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _get_conn()
    fields = ["updated_at = ?"]
    params: list[Any] = [now]

    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if guardrail_state is not None:
        fields.append("guardrail_state = ?")
        params.append(guardrail_state)
    if root_cause is not None:
        fields.append("root_cause = ?")
        params.append(root_cause)
    if remediation is not None:
        fields.append("remediation = ?")
        params.append(remediation)
    if evidence is not None:
        fields.append("evidence_json = ?")
        params.append(json.dumps(evidence))

    params.append(case_id)
    cur = conn.cursor()
    cur.execute(f"UPDATE cases SET {', '.join(fields)} WHERE case_id = ?", params)
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def get_case(case_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single case by its ID."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    res = dict(row)
    try:
        res["evidence"] = json.loads(res.pop("evidence_json", "{}"))
    except Exception:
        res["evidence"] = {}
    return res


def list_cases(limit: int = 50) -> list[dict[str, Any]]:
    """List recent cases sorted by creation time descending."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cases ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["evidence"] = json.loads(item.pop("evidence_json", "{}"))
        except Exception:
            item["evidence"] = {}
        result.append(item)
    return result


def find_similar_cases(reason_code: int, limit: int = 3) -> list[dict[str, Any]]:
    """Search for historical cases with the same MQ reason code to leverage past findings."""
    if not reason_code:
        return []
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT case_id, reason_code, reason_name, failure_class, root_cause, remediation, status FROM cases WHERE reason_code = ? AND status = 'RESOLVED' ORDER BY created_at DESC LIMIT ?",
        (reason_code, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]
