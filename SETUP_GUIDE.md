# IBM MQ & ACE Dead Letter Queue (DLQ) Resolution Console
## Local Setup, Configuration & Testing Guide

This guide details everything required to configure, test, and run the **IBM MQ & App Connect Enterprise (ACE) DLQ Resolution Application** on a local Windows machine.

---

## 1. Prerequisites (Software Requirements)

Ensure the following are installed on the local system:

| Software | Minimum Version | Purpose |
| :--- | :--- | :--- |
| **IBM MQ** | v9.0+ / v10.0+ | MQ Queue Manager runtime and native .NET client (`C:\Program Files\IBM\MQ\bin\amqmdnet.dll`). |
| **Python** | 3.9+ | Lightweight web server running the dashboard UI (`python server.py`). |
| **PowerShell** | 5.1+ (Windows built-in) | Backend execution engine for MQ API calls & Event Log triage. |
| **IBM MQ Explorer** *(Optional)* | v9.0+ | Visual GUI for inspecting queues, channels, and listener ports. |

---

## 2. IBM MQ Objects Required

Ensure the following objects exist on your Queue Manager (e.g., `QMGR2` or `QMGR`):

| Object Type | Object Name | Description |
| :--- | :--- | :--- |
| **Queue Manager** | `QMGR2` *(or custom)* | The active IBM MQ Queue Manager. |
| **Dead Letter Queue** | `SYSTEM.DEAD.LETTER.QUEUE` | Local queue designated as the Queue Manager's `DEADQ`. |
| **TCP Listener** | `SYSTEM.DEFAULT.LISTENER.TCP` | Listener running on dedicated port (e.g., `1415` for QMGR2, `1414` for QMGR). |
| **SVRCONN Channel** | `SYSTEM.ADMIN.SVRCONN` | Server-Connection channel used by client applications to connect. |
| **Target Queue** | `UC2.MISSING` *(or app queue)* | The destination queue where the original messages were intended to be delivered. |

### Remote Queue Manager Profile

The active dashboard profile is configured for queue manager `QMGR` at
`172.28.128.1`, using port `1415`, channel `SYSTEM.ADMIN.SVRCONN`, and
`SYSTEM.DEAD.LETTER.QUEUE`. Port and channel were carried forward from the
previous local setup; confirm the remote MQ listener and channel values with
the MQ administrator. The remote host must allow network access to the listener
port, and the configured application identity must have authority to connect
and browse the DLQ. If connection testing fails, the remote profile remains
selectable and the dashboard displays the diagnostic.

---

## 3. IBM MQ Security & Channel Configuration

Use your organization's approved IBM MQ security design. Grant the application
identity only the minimum required authorities on the DLQ and target queues.
Keep CHLAUTH enabled and do not map application channels to administrative
service accounts. Do not weaken CONNAUTH or remove default blocking rules to
make the POC connect; ask an MQ administrator to diagnose authorization errors
and configure a dedicated, least-privilege identity.

---

## 4. MQ Object Setup

Have an MQ administrator confirm that the DLQ, target queues, listener, and
application channel are configured according to local standards. This guide
does not provide blanket MQSC commands that disable authentication or assign
administrative channel identities.

---

## 5. Simulating Failed DLQ Messages

To test the 8-step triage lifecycle, generate candidate failed messages on the DLQ:

### Scenario: Message routed to a non-existent queue (Reason 2085)
1. Send messages to a queue that does not exist (`UC2.MISSING`) over a message flow or remote queue definition.
2. IBM MQ automatically wraps the original message with an **`MQDLH`** (Dead Letter Header), attaches Reason Code `2085 (MQRC_UNKNOWN_OBJECT_NAME)`, and moves it to `SYSTEM.DEAD.LETTER.QUEUE`.

---

## 6. Running and Using the Application

### Step 1: Open the Project Directory
```powershell
cd c:\Users\<username>\Documents\IntegrationMQ
```

### Step 2: Review or Update Configuration (Optional)
The application reads defaults from `.env`:
```ini
MQ_QMGR=QMGR2
MQ_HOST=localhost
MQ_PORT=1415
MQ_CHANNEL=SYSTEM.ADMIN.SVRCONN
MQ_DLQ_NAME=SYSTEM.DEAD.LETTER.QUEUE
MQ_USER=your_least_privilege_user
MQ_PASSWORD=your_password_here
```
*(Note: You can also change all of these settings directly inside the Web UI at any time).*

### Step 3: Start the Web UI
```powershell
python server.py
```
The server will start listening at: **`http://localhost:4000`**

### Step 4: Open in Your Browser
Navigate to:
```
http://localhost:4000
```

---

## 7. DLQ Triage & Per-Message Analysis

1. The dashboard discovers running queue managers and displays one selected manager's configured DLQ at a time. Continuous monitoring starts automatically for the selected manager.
2. The monitor polls every 15 seconds, updates queue depth, counts newly observed message IDs, and raises dashboard alerts. The server process must remain running. Use **Stop Monitoring** to pause it; selecting a different queue manager switches the monitored target.
3. Newly observed messages are retained in server memory for operator review (up to 200 messages and 50 recent alert entries). The counter and history reset when monitoring is restarted, the target changes, or the server restarts. If the browse limit is reached, the dashboard warns that the queue may contain additional messages.
4. Click **Review message** on an alert to locate it in the message list, then click **Analyze** to request a per-message diagnosis. The prescription is concise: **Root Cause**, **Evidence and Unknowns**, **Recommended Action** (`HOLD`, `REPLAY`, or `ESCALATE`), and **Remediation Steps**.
5. The agent must distinguish observed evidence from inference. A missing `MQDLH` means the original destination and reason are unknown. An operator must validate suggested remediation against the live environment.
6. Monitoring, triage, and analysis only browse DLQ messages. They do not change message persistence or remove messages. This POC has no authenticated approval, trusted duplicate evidence, downstream-health evidence, or post-replay verification. The agent must not recommend `REPLAY` based on the POC data alone. Live replay remains disabled.

## 8. Message and Audit Retention

The application does not write operational audit events to SQLite. Monitor state, recent alerts, and observed message details are held in server memory and are cleared when monitoring is restarted or the server stops. Configuration and the optional AI API key continue to be stored in their existing local configuration files.

---

## 9. CLI Alternative (Running Without UI)

You can run read-only triage or a read-only replay-safety check directly from PowerShell:

```powershell
# 1. Run full triage analysis:
powershell -ExecutionPolicy Bypass -File .\Invoke-DlqTriage.ps1

# 2. Run simulation (Dry-Run Replay):
powershell -ExecutionPolicy Bypass -File .\Invoke-DlqTriage.ps1 -Replay -DryRun

# Live replay is intentionally blocked until the required approval, duplicate,
# downstream health, transactional, and post-action verification integrations exist.
```
