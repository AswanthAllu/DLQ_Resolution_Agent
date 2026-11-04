"""tests for case_store.py and guardrails.py"""

import case_store
import guardrails


def test_case_store_create_and_get():
    case_id = case_store.create_case(
        trigger_type="test_trigger",
        qmgr="QMGR2",
        dlq_name="SYSTEM.DEAD.LETTER.QUEUE",
        message_id="1234567890ABCDEF",
        reason_code=2085,
        reason_name="MQRC_UNKNOWN_OBJECT_NAME",
        failure_class="Configuration",
        root_cause="Missing target queue",
        status="OPEN",
    )
    assert case_id.startswith("CASE-")
    fetched = case_store.get_case(case_id)
    assert fetched is not None
    assert fetched["qmgr"] == "QMGR2"
    assert fetched["reason_code"] == 2085
    assert fetched["status"] == "OPEN"


def test_guardrails_kill_switch():
    assert not guardrails.is_kill_switch_active()
    guardrails.set_kill_switch(True)
    assert guardrails.is_kill_switch_active()
    allowed, status, msg = guardrails.evaluate_action_guardrails("replay_live", "123456")
    assert not allowed
    assert status == "BLOCKED"
    guardrails.set_kill_switch(False)
    assert not guardrails.is_kill_switch_active()


def test_guardrails_approval_flow():
    case_id = "CASE-TEST-01"
    req = guardrails.register_pending_approval(
        case_id=case_id,
        action="replay_live",
        message_id="AAAABBBBCCCC",
        target_queue="TEST.QUEUE",
        qmgr="QMGR2",
    )
    assert req["status"] == "PENDING_APPROVAL"
    approvals = guardrails.list_pending_approvals()
    assert any(a["case_id"] == case_id for a in approvals)

    ok, msg = guardrails.decide_approval(case_id, "APPROVED", "operator1", "Approved for test")
    assert ok
    assert "approved" in msg.lower()
