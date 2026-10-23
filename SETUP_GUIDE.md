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

---

## 3. IBM MQ Security & Channel Configuration

When connecting locally on Windows (especially on domain-joined machines), configure the following MQ settings to prevent `MQRC_NOT_AUTHORIZED (2035)` and `MQRC_SECURITY_ERROR (2063)`:

### A. Set Channel `MCAUSER`
Set `MCAUSER` to the local MQ administrator (`MUSR_MQADMIN`). This instructs IBM MQ to run the channel under the local service account, bypassing Active Directory domain RPC lookups:
```mqsc
DEFINE CHANNEL(SYSTEM.ADMIN.SVRCONN) CHLTYPE(SVRCONN) TRPTYPE(TCP) MCAUSER('MUSR_MQADMIN') REPLACE
```

### B. Disable Channel Blocking (`CHLAUTH`)
By default, IBM MQ blocks all remote administrative connections. For local development / testing:
```mqsc
ALTER QMGR CHLAUTH(DISABLED)
```
*(Alternatively, in IBM MQ Explorer: Navigate to **QMGR2** &rarr; **Advanced** &rarr; **Channel Authentication Records** &rarr; Delete or Disable the `*MQADMIN` block rule).*

### C. Configure Connection Authentication (`CONNAUTH`)
Set client connection checking to `OPTIONAL` on the OS authentication info object:
```mqsc
ALTER AUTHINFO(SYSTEM.DEFAULT.AUTHINFO.IDPWOS) AUTHTYPE(IDPWOS) CHCKCLNT(OPTIONAL)
REFRESH SECURITY TYPE(CONNAUTH)
```

---

## 4. One-Click MQSC Setup Script

Open an **Elevated (Run as Administrator)** Command Prompt or PowerShell, and execute:

```cmd
runmqsc QMGR2
```

Paste the following block and press **Enter**:

```mqsc
* 1. Ensure Dead Letter Queue exists and is assigned
DEFINE QLOCAL(SYSTEM.DEAD.LETTER.QUEUE) REPLACE
ALTER QMGR DEADQ(SYSTEM.DEAD.LETTER.QUEUE)

* 2. Define Server-Connection Channel with local MCA user
DEFINE CHANNEL(SYSTEM.ADMIN.SVRCONN) CHLTYPE(SVRCONN) TRPTYPE(TCP) MCAUSER('MUSR_MQADMIN') REPLACE

* 3. Relax Connection Auth and Disable Remote CHLAUTH Blocking
ALTER AUTHINFO(SYSTEM.DEFAULT.AUTHINFO.IDPWOS) AUTHTYPE(IDPWOS) CHCKCLNT(OPTIONAL)
ALTER QMGR CHLAUTH(DISABLED)
REFRESH SECURITY TYPE(CONNAUTH)

* 4. Ensure Listener is Running
START LISTENER(SYSTEM.DEFAULT.LISTENER.TCP)
END
```

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
MQ_USER=mqusermchi
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

## 7. The 8-Step Triage & Resolution Workflow in the UI

1. **Detection**: The dashboard polls `SYSTEM.DEAD.LETTER.QUEUE` and displays current queue depth and metrics.
2. **Identification**: Extracts message metadata (`MsgId`, `CorrelId`, put timestamps, and application names).
3. **Queue Identification**: Decodes `MQDLH.DestQName` (`UC2.MISSING`) and `MQDLH.DestQMgrName` (`QMGR2`).
4. **Inspection**: Click **Inspect** to preview the decoded business payload with syntax highlighting and length checks.
5. **MQ Error Analysis**: Translates raw numeric error codes (e.g. `2085`) into technical descriptions (`MQRC_UNKNOWN_OBJECT_NAME`).
6. **ACE Error Correlation**: Automatically scans the Windows Application Event Log for correlated `BIP` events within a &plusmn;10-minute window.
7. **Failure Classification**: Classifies each message into `Configuration`, `Transient`, or `Data`.
8. **Decision & Replay**:
   * Evaluates if automated retry is safe (e.g., transient network issues vs. missing queues).
   * **Dry-Run Replay**: Simulates requeuing without affecting messages.
   * **Execute Replay**: Once the target queue (`UC2.MISSING`) is created, re-puts the message with the `MQDLH` stripped.

---

## 8. CLI Alternative (Running Without UI)

You can also run the full triage pipeline directly from PowerShell:

```powershell
# 1. Run full triage analysis:
powershell -ExecutionPolicy Bypass -File .\Invoke-DlqTriage.ps1

# 2. Run simulation (Dry-Run Replay):
powershell -ExecutionPolicy Bypass -File .\Invoke-DlqTriage.ps1 -Replay -DryRun

# 3. Execute live replay back to target queues:
powershell -ExecutionPolicy Bypass -File .\Invoke-DlqTriage.ps1 -Replay -DryRun:$false
```
