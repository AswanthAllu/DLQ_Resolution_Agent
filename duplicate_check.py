"""duplicate_check.py — Trusted duplicate-message verification for DLQ replay safety.

Uses the SQLite database defined in config.json → duplicateCheck section.
A message is considered a duplicate if its message_id already exists in
the processed_messages table. Returns 'unique', 'duplicate', or 'inconclusive'.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import config_loader

ROOT = Path(__file__).resolve().parent


def _db_path() -> Path:
    cfg = config_loader.duplicate_check()
    return ROOT / cfg.get("dbPath", "dlq-audit.sqlite3")


def _table() -> str:
    return config_loader.duplicate_check().get("tableName", "processed_messages")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_table()} (
            message_id   TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            operator_id  TEXT DEFAULT ''
        )"""
    )
    conn.commit()
    return conn


def verify(message_id: str) -> Literal["unique", "duplicate", "inconclusive"]:
    """Check whether *message_id* has already been processed.

    Returns
    -------
    'duplicate'    — message_id found in the processed table.
    'unique'       — message_id not found; it is recorded as processed now.
    'inconclusive' — empty / invalid message_id; replay must be blocked.
    """
    mid = (message_id or "").strip()
    if not mid:
        return "inconclusive"
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM {_table()} WHERE message_id = ?", (mid,))
        if cur.fetchone():
            conn.close()
            return "duplicate"
        cur.execute(
            f"INSERT INTO {_table()} (message_id, processed_at) VALUES (?, ?)",
            (mid, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        return "unique"
    except sqlite3.Error as exc:
        print(f"[duplicate_check] DB error: {exc}")
        return "inconclusive"


def mark_processed(message_id: str, operator_id: str = "") -> bool:
    """Explicitly mark a message as processed (e.g. after confirmed replay).

    Returns True on success.
    """
    mid = (message_id or "").strip()
    if not mid:
        return False
    try:
        conn = _get_conn()
        conn.execute(
            f"""INSERT INTO {_table()} (message_id, processed_at, operator_id)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    processed_at = excluded.processed_at,
                    operator_id  = excluded.operator_id""",
            (mid, datetime.now(timezone.utc).isoformat(), operator_id),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as exc:
        print(f"[duplicate_check] DB error: {exc}")
        return False
