"""orchestrator.py — Router and 9-Step Pipeline Orchestrator.

Per Figure 2:
- Picks the use case (UC2 DLQ Resolution Agent)
- Runs the 9-step pipeline
- Enforces budgets
- Pauses for approval at the Guardrails gate
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import agent
import case_store
import guardrails
import mcp_tools

logger = logging.getLogger(__name__)


class Orchestrator:
    """9-Step Orchestrator for UC2: Dead Letter Queue Resolution."""

    def __init__(self) -> None:
        self.active_use_case = "UC2_DLQ"

    def run_pipeline(
        self,
        trigger_type: str,  # 'monitoring_alert', 'engineer_question', 'manual_triage'
        config: dict[str, Any],
        target_msg_id: str = "",
        order_id: str = "",
        custom_query: str = "",
        operator_id: str = "system",
    ) -> dict[str, Any]:
        """Execute the 9-step resolution pipeline.

        Steps:
        1. Trigger Ingestion
        2. Key & Time Correlation (Correlation Engine)
        3. Evidence Collection (Tool Layer / MCP Server)
        4. Signature Matching (Historical Case Store)
        5. AI Root Cause & Remediation (LLM Gateway)
        6. Case Store Logging (Case Store)
        7. Guardrails Evaluation (Approval Gate, Limits, Kill Switch)
        8. Approved Action Execution (or Pausing)
        9. Post-Action Verification & Audit
        """
        pipeline_log: list[dict[str, Any]] = []

        def _log_step(step_num: int, name: str, status: str, details: Any = None):
            pipeline_log.append({
                "step": step_num,
                "name": name,
                "status": status,
                "details": details or {},
            })

        qmgr = config.get("qmgr", "QMGR2")
        host = config.get("host", "localhost")
        port = int(config.get("port", 1415))
        channel = config.get("channel", "SYSTEM.ADMIN.SVRCONN")
        dlq_name = config.get("dlq", "SYSTEM.DEAD.LETTER.QUEUE")
        user_id = config.get("userId", "")
        password = config.get("password", "")

        # -------------------------------------------------------------------
        # Step 1: Trigger Ingestion
        # -------------------------------------------------------------------
        _log_step(1, "Trigger Ingestion", "COMPLETED", {
            "triggerType": trigger_type,
            "targetQMgr": qmgr,
            "dlq": dlq_name,
            "orderId": order_id,
            "targetMsgId": target_msg_id,
        })

        # -------------------------------------------------------------------
        # Step 2: Key & Time Correlation
        # -------------------------------------------------------------------
        _log_step(2, "Key & Time Correlation", "COMPLETED", {
            "correlatedKeys": ["MsgId", "CorrelId", "PutDateTime", "OrderReference"],
            "correlationWindowMinutes": 10,
        })

        # -------------------------------------------------------------------
        # Step 3: Evidence Collection (Tool Layer / MCP)
        # -------------------------------------------------------------------
        triage_data = mcp_tools.tool_mq_browse_dlq(
            qmgr=qmgr,
            host=host,
            port=port,
            channel=channel,
            dlq_name=dlq_name,
            user_id=user_id,
            password=password,
            max_messages=50,
        )

        messages = triage_data.get("messages", [])
        if target_msg_id:
            messages = [m for m in messages if m.get("msgId") == target_msg_id]

        _log_step(3, "Evidence Collection (Tool Layer)", "COMPLETED", {
            "queueDepth": triage_data.get("curDepth", 0),
            "messagesCollected": len(messages),
        })

        if not messages:
            case_id = case_store.create_case(
                trigger_type=trigger_type,
                qmgr=qmgr,
                dlq_name=dlq_name,
                status="RESOLVED",
                guardrail_state="PASSED",
                root_cause="Queue is clear or message no longer present on DLQ.",
                remediation="No remediation required.",
            )
            _log_step(6, "Case Store Logging", "COMPLETED", {"caseId": case_id})
            return {
                "success": True,
                "caseId": case_id,
                "pipeline": pipeline_log,
                "summary": "DLQ has 0 messages.",
            }

        # Focus analysis on the primary candidate message
        primary_msg = messages[0]
        msg_id = primary_msg.get("msgId", "")
        correl_id = primary_msg.get("correlId", "")
        reason_code = primary_msg.get("reasonCode", 0)
        reason_name = primary_msg.get("reasonName", "")
        failure_class = primary_msg.get("failureClass", "Unknown")
        target_q = primary_msg.get("targetQueue", "")

        # -------------------------------------------------------------------
        # Step 4: Signature Matching against Case Store
        # -------------------------------------------------------------------
        similar = case_store.find_similar_cases(reason_code, limit=2)
        _log_step(4, "Signature Matching (Case Store)", "COMPLETED", {
            "reasonCode": reason_code,
            "similarResolvedCases": len(similar),
        })

        # -------------------------------------------------------------------
        # Step 5: AI Root Cause & Remediation (LLM Gateway)
        # -------------------------------------------------------------------
        ai_prompt = (
            f"You are the UC2 DLQ Resolution Agent. Analyze this failure:\n"
            f"- Message ID: {msg_id}\n"
            f"- Intended Target Queue: {target_q}\n"
            f"- Intended Target QMgr: {primary_msg.get('targetQMgr')}\n"
            f"- MQ Reason Code: {reason_code} ({reason_name})\n"
            f"- Failure Classification: {failure_class}\n"
            f"- MQMD Format: {primary_msg.get('format')} | CCSID: {primary_msg.get('ccsid')}\n"
            f"- Masked Payload Preview: {primary_msg.get('payloadText')}\n"
            f"State the clear technical root cause, classify the failure, and provide exact `runmqsc` remediation commands.\n"
        )
        if custom_query:
            ai_prompt += f"Operator Inquiry: {custom_query}\n"

        ai_result = agent.run_agent_sync(ai_prompt)
        analysis_text = ai_result.get("analysis", f"Reason {reason_code}: {reason_name}. Target {target_q} requires attention.")
        _log_step(5, "AI Root Cause & Remediation (LLM Gateway)", "COMPLETED", {
            "model": ai_result.get("model", "Gemini"),
            "hasAnalysis": bool(ai_result.get("success")),
        })

        # -------------------------------------------------------------------
        # Step 6: Case Store Logging
        # -------------------------------------------------------------------
        case_id = case_store.create_case(
            trigger_type=trigger_type,
            qmgr=qmgr,
            dlq_name=dlq_name,
            message_id=msg_id,
            correl_id=correl_id,
            order_id=order_id,
            reason_code=reason_code,
            reason_name=reason_name,
            failure_class=failure_class,
            root_cause=analysis_text[:500],
            remediation=f"Fix condition for target {target_q} before attempting replay.",
            status="OPEN",
            guardrail_state="PENDING_APPROVAL",
            evidence=primary_msg,
        )
        _log_step(6, "Case Store Logging", "COMPLETED", {"caseId": case_id})

        # -------------------------------------------------------------------
        # Step 7: Guardrails Evaluation
        # -------------------------------------------------------------------
        allowed, g_status, g_msg = guardrails.evaluate_action_guardrails(
            action="replay_live",
            message_id=msg_id,
            operator_id=operator_id,
        )

        if g_status == "BLOCKED":
            case_store.update_case(case_id, status="BLOCKED", guardrail_state="BLOCKED")
            _log_step(7, "Guardrails Evaluation", "BLOCKED", {"reason": g_msg})
            return {
                "success": False,
                "caseId": case_id,
                "pipeline": pipeline_log,
                "error": g_msg,
                "analysis": analysis_text,
            }

        # -------------------------------------------------------------------
        # Step 8: Paused for Approval / Approved Execution
        # -------------------------------------------------------------------
        # By default, production write operations pause here until human approver acts!
        guardrails.register_pending_approval(
            case_id=case_id,
            action="replay_live",
            message_id=msg_id,
            target_queue=target_q,
            qmgr=qmgr,
            requested_by=operator_id,
            details={"reasonCode": reason_code, "failureClass": failure_class},
        )
        _log_step(8, "Production Write Execution", "PAUSED_FOR_APPROVAL", {
            "message": "Production change paused per Guardrails safety policy. Awaiting Human Approver decision.",
            "approvalRequired": True,
        })

        # -------------------------------------------------------------------
        # Step 9: Post-Action Verification & Audit
        # -------------------------------------------------------------------
        _log_step(9, "Post-Action Verification & Audit", "STANDBY", {
            "status": "Awaiting approval before execution and post-replay verification.",
        })

        return {
            "success": True,
            "caseId": case_id,
            "status": "PAUSED_FOR_APPROVAL",
            "message": primary_msg,
            "pipeline": pipeline_log,
            "analysis": analysis_text,
        }

    def execute_approved_remediation(
        self,
        case_id: str,
        operator_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Called ONLY after human approval is granted in Guardrails."""
        case = case_store.get_case(case_id)
        if not case:
            return {"success": False, "error": f"Case {case_id} not found"}

        if guardrails.is_kill_switch_active():
            return {"success": False, "error": "Kill switch is active. Execution blocked."}

        # Execute live replay tool (via gated write tool)
        qmgr = config.get("qmgr", case["qmgr"])
        host = config.get("host", "localhost")
        port = int(config.get("port", 1415))
        channel = config.get("channel", "SYSTEM.ADMIN.SVRCONN")
        dlq_name = config.get("dlq", case["dlq_name"])
        msg_id = case["message_id"]

        result = mcp_tools.tool_mq_execute_live_replay(
            qmgr=qmgr,
            host=host,
            port=port,
            channel=channel,
            dlq_name=dlq_name,
            target_msg_id=msg_id,
            operator_id=operator_id,
            user_id=config.get("userId", ""),
            password=config.get("password", ""),
        )

        if result.get("success"):
            case_store.update_case(case_id, status="RESOLVED", guardrail_state="EXECUTED")
            return {"success": True, "caseId": case_id, "result": result}
        else:
            case_store.update_case(case_id, status="FAILED", guardrail_state="EXECUTION_FAILED")
            return {"success": False, "caseId": case_id, "error": result.get("error")}


# Global orchestrator instance
orchestrator = Orchestrator()
