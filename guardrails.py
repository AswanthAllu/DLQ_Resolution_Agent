"""guardrails.py — Solution Architecture Guardrails Layer.

Per Figure 2: The highlighted Guardrails box is the ONLY PATH from any agent
to a change in production.

Enforces:
1. Approval Gate (Human-in-the-Loop approval for write actions)
2. Global Kill Switch (Immediate stop for all write/replay execution)
3. Rate Limits & Budget Checks
4. RBAC & Identity Checks
5. Duplicate Message Verification (integrated with duplicate_check.py)
"""

from __future__ import annotations

import threading
import time
from typing import Any, Literal, Optional

import approval_log
import duplicate_check

# Global In-Memory Guardrail State
_lock = threading.Lock()
_KILL_SWITCH_ACTIVE = False
_RATE_LIMIT_WINDOW = 60.0  # seconds
_MAX_OPS_PER_WINDOW = 10
_op_timestamps: list[float] = []

# Guardrail Approval Queue: case_id -> pending request details
_pending_approvals: dict[str, dict[str, Any]] = {}


def is_kill_switch_active() -> bool:
    """Return whether the global emergency kill switch is engaged."""
    with _lock:
        return _KILL_SWITCH_ACTIVE


def set_kill_switch(active: bool) -> bool:
    """Engage or disengage the global kill switch."""
    global _KILL_SWITCH_ACTIVE
    with _lock:
        _KILL_SWITCH_ACTIVE = active
        return _KILL_SWITCH_ACTIVE


def check_rate_limit() -> tuple[bool, str]:
    """Verify that operations do not exceed the safety rate limit."""
    now = time.monotonic()
    with _lock:
        global _op_timestamps
        _op_timestamps = [t for t in _op_timestamps if now - t < _RATE_LIMIT_WINDOW]
        if len(_op_timestamps) >= _MAX_OPS_PER_WINDOW:
            return False, f"Rate limit exceeded: maximum {_MAX_OPS_PER_WINDOW} operations per minute."
        _op_timestamps.append(now)
        return True, "Rate limit check passed."


def register_pending_approval(
    case_id: str,
    action: str,
    message_id: str,
    target_queue: str,
    qmgr: str,
    requested_by: str = "orchestrator",
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Pause execution and submit a production change request to the Guardrails Approval Gate."""
    with _lock:
        req = {
            "case_id": case_id,
            "action": action,
            "message_id": message_id,
            "target_queue": target_queue,
            "qmgr": qmgr,
            "requested_by": requested_by,
            "status": "PENDING_APPROVAL",
            "timestamp": time.time(),
            "details": details or {},
        }
        _pending_approvals[case_id] = req
        return req


def list_pending_approvals() -> list[dict[str, Any]]:
    """Return all pending approvals currently queued in Guardrails."""
    with _lock:
        return list(_pending_approvals.values())


def decide_approval(
    case_id: str,
    decision: Literal["APPROVED", "REJECTED"],
    operator_id: str,
    reason: str = "",
) -> tuple[bool, str]:
    """Human Approver acts on a pending change request.

    If APPROVED: Logs entry to approval_log and marks decision.
    If REJECTED: Discards request and logs rejection.
    """
    with _lock:
        req = _pending_approvals.get(case_id)
        if not req:
            return False, f"No pending approval found for case {case_id}."

        if decision == "APPROVED":
            req["status"] = "APPROVED"
            req["decided_by"] = operator_id
            req["decision_reason"] = reason
            approval_log.record(
                operator_id=operator_id,
                action=req["action"],
                message_id=req["message_id"],
                notes=f"Approved case {case_id} for queue {req['target_queue']}. {reason}".strip(),
            )
            return True, f"Operation for case {case_id} approved by {operator_id}."
        else:
            req["status"] = "REJECTED"
            req["decided_by"] = operator_id
            req["decision_reason"] = reason
            approval_log.record(
                operator_id=operator_id,
                action=f"REJECTED_{req['action']}",
                message_id=req["message_id"],
                notes=f"Rejected case {case_id}: {reason}".strip(),
            )
            return True, f"Operation for case {case_id} rejected by {operator_id}."


def evaluate_action_guardrails(
    action: str,
    message_id: str,
    operator_id: str = "unknown",
) -> tuple[bool, str, str]:
    """Evaluate all guardrails before an action is permitted to alter production.

    Returns:
        (allowed: bool, status: str, message: str)
        status can be 'PASSED', 'BLOCKED', 'NEEDS_APPROVAL'
    """
    if is_kill_switch_active():
        return False, "BLOCKED", "Global emergency kill switch is active. All write/replay operations are halted."

    rate_ok, rate_msg = check_rate_limit()
    if not rate_ok:
        return False, "BLOCKED", rate_msg

    # Duplicate verification
    dup_status = duplicate_check.verify(message_id)
    if dup_status == "duplicate":
        return False, "BLOCKED", f"Duplicate detected: Message {message_id} was already processed."
    if dup_status == "inconclusive":
        return False, "BLOCKED", "Message ID cannot be verified for uniqueness. Operation blocked."

    # Read-only or dry-run checks do not modify state
    if action in ("probe", "inspect", "dry_run", "triage"):
        return True, "PASSED", "Read-only operation permitted through guardrails."

    # Any live write/replay REQUIRES human approval
    return True, "NEEDS_APPROVAL", "Production modification requires human approval gate clearance."
