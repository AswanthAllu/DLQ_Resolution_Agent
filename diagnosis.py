"""diagnosis.py — Enriched MQ reason-code diagnosis with runbook links.

Reads runbook URLs from config.json → runbooks so they can be updated without
touching code.  Call `enrich(reason_code)` to get a fully-populated diagnosis
dict that includes runbook URL when available.
"""

from __future__ import annotations

import json
from typing import Any

import config_loader

# ---------------------------------------------------------------------------
# Reason-code catalog (same data as agent.py but centralised here)
# ---------------------------------------------------------------------------

_CATALOG: dict[int, dict[str, str]] = {
    2001: {"name": "MQRC_ALIAS_APPL_TYPE_ERROR", "class": "Configuration",
           "desc": "Alias application type error. Target object of an alias queue does not match type.",
           "remediation": "Verify alias queue target definition: DISPLAY QALIAS(<NAME>) TARGET."},
    2009: {"name": "MQRC_CONNECTION_BROKEN", "class": "Transient",
           "desc": "Network connection to Queue Manager was broken mid-flight.",
           "remediation": "Ensure TCP listener is running, check firewall and network stability. Safe to retry."},
    2014: {"name": "MQRC_BACKED_OUT", "class": "Configuration",
           "desc": "Message transaction was backed out. Backout count reached threshold.",
           "remediation": "Check application backout queue and backout threshold (BOTHRESH) settings."},
    2016: {"name": "MQRC_GET_INHIBITED", "class": "Poison",
           "desc": "GET operations are inhibited on the destination queue.",
           "remediation": "Enable GET: ALTER QLOCAL('<TARGET_QUEUE>') GET(ENABLED)."},
    2033: {"name": "MQRC_NO_MSG_AVAILABLE", "class": "Transient",
           "desc": "No message available when GET with NO_WAIT was performed.",
           "remediation": "Normal operational response when queue is drained."},
    2035: {"name": "MQRC_NOT_AUTHORIZED", "class": "Configuration",
           "desc": "Security authority check failed. The user or application lacks +put authority.",
           "remediation": "Grant MQ permissions: SETMQAUT -m <QMGR> -t q -n <TARGET_QUEUE> -p <USER> +put. Or check CHLAUTH rules."},
    2040: {"name": "MQRC_NOT_OPEN_FOR_SET", "class": "Configuration",
           "desc": "Queue was not opened with MQOO_SET. Attempted MQSET or set context operation without required authorization.",
           "remediation": "Verify application opens queue with MQOO_SET or check MQOO_PASS_ALL_CONTEXT / +set authority on queue."},
    2052: {"name": "MQRC_Q_DELETED", "class": "Configuration",
           "desc": "Target queue was deleted after message was sent.",
           "remediation": "Recreate the queue: DEFINE QLOCAL('<TARGET_QUEUE>') DEFPSIST(YES) REPLACE."},
    2053: {"name": "MQRC_Q_FULL", "class": "Transient",
           "desc": "Target queue reached MAXDEPTH. Messages cannot be placed until queue drains.",
           "remediation": "Increase queue depth: ALTER QLOCAL('<TARGET_QUEUE>') MAXDEPTH(50000). Ensure consumer is running."},
    2058: {"name": "MQRC_Q_MGR_NAME_ERROR", "class": "Configuration",
           "desc": "Queue Manager name does not match the port or listener.",
           "remediation": "Verify the Queue Manager name in the connection string and check port mappings."},
    2059: {"name": "MQRC_Q_MGR_NOT_AVAILABLE", "class": "Transient",
           "desc": "Queue Manager is not running or the TCP listener is stopped.",
           "remediation": "Start QMGR: strmqm <QMGR>. Start listener: START LISTENER(SYSTEM.DEFAULT.LISTENER.TCP)."},
    2063: {"name": "MQRC_SECURITY_ERROR", "class": "Configuration",
           "desc": "Windows domain security validation failed for user under MUSR_MQADMIN service.",
           "remediation": "Have the MQ administrator review CONNAUTH, CHLAUTH, and the approved application identity mapping."},
    2080: {"name": "MQRC_TRUNCATED_MSG_FAILED", "class": "Data",
           "desc": "Message buffer was too small to hold the payload.",
           "remediation": "Increase receiving buffer size or specify MQGMO_ACCEPT_TRUNCATED_MSG."},
    2082: {"name": "MQRC_UNKNOWN_ALIAS_BASE_Q", "class": "Configuration",
           "desc": "Alias queue points to a base queue that does not exist.",
           "remediation": "Create the base queue or update the alias: ALTER QALIAS(<ALIAS>) TARGET(<NEW_BASE_Q>)."},
    2085: {"name": "MQRC_UNKNOWN_OBJECT_NAME", "class": "Configuration",
           "desc": "Target destination queue does not exist on the Queue Manager.",
           "remediation": "Create the missing target queue: DEFINE QLOCAL('<TARGET_QUEUE>') DEFPSIST(YES) REPLACE."},
    2087: {"name": "MQRC_UNKNOWN_REMOTE_Q_MGR", "class": "Configuration",
           "desc": "Remote Queue Manager name specified in transmission header is unknown.",
           "remediation": "Define transmission queue and remote queue object pointing to the correct remote QMGR."},
    2100: {"name": "MQRC_XMIT_Q_WRONG_TYPE", "class": "Configuration",
           "desc": "Transmission queue is not a local queue or has usage other than XMITQ.",
           "remediation": "ALTER QLOCAL(<XMITQ>) USAGE(XMITQ)."},
    2102: {"name": "MQRC_NO_XMIT_Q", "class": "Configuration",
           "desc": "No transmission queue defined for the remote Queue Manager destination.",
           "remediation": "DEFINE QLOCAL(<XMITQ>) USAGE(XMITQ) DEFPSIST(YES) REPLACE."},
    2110: {"name": "MQRC_FORMAT_ERROR", "class": "Data",
           "desc": "Message format data is inconsistent with Format name in MQMD.",
           "remediation": "Inspect payload format, verify character encoding (CCSID), check if binary data in text format."},
    2162: {"name": "MQRC_Q_MGR_QUIESCING", "class": "Transient",
           "desc": "Queue Manager is shutting down gracefully.",
           "remediation": "Wait for Queue Manager restart. Safe to retry once QMGR status is RUNNING."},
    2195: {"name": "MQRC_UNEXPECTED_ERROR", "class": "Transient",
           "desc": "Unexpected internal MQ failure.",
           "remediation": "Check MQ error logs (AMQERR01.LOG) and First Failure Support Technology (FFST) files."},
    2537: {"name": "MQRC_CHANNEL_NOT_AVAILABLE", "class": "Transient",
           "desc": "SVRCONN channel is currently stopped or has reached MAXINST / MAXINSTC limits.",
           "remediation": "Start channel: START CHANNEL(<CHANNEL>). Increase MAXINST: ALTER CHANNEL(<CHANNEL>) MAXINST(100)."},
    2540: {"name": "MQRC_UNKNOWN_CHANNEL_NAME", "class": "Configuration",
           "desc": "Channel name was not found on the Queue Manager.",
           "remediation": "Verify the approved SVRCONN channel definition and its least-privilege identity mapping."},
}


def lookup(reason_code: int) -> dict[str, Any]:
    """Return the catalog entry for *reason_code*, or a sensible unknown entry."""
    info = _CATALOG.get(reason_code)
    if info:
        return {"code": reason_code, **info}
    return {
        "code": reason_code,
        "name": f"MQRC_{reason_code}",
        "class": "Unknown",
        "desc": f"IBM MQ reason code {reason_code}.",
        "remediation": f"Consult IBM MQ Knowledge Center for reason code {reason_code}.",
    }


def enrich(reason_code: int) -> dict[str, Any]:
    """Return full diagnosis including runbook URL from config.json → runbooks."""
    info = lookup(reason_code)
    rb = config_loader.runbooks().get(str(reason_code))
    if rb:
        info["runbook"] = rb
    return info


def enrich_json(reason_code: int) -> str:
    """Convenience wrapper returning JSON string (for use as an ADK tool)."""
    return json.dumps(enrich(reason_code), indent=2)
