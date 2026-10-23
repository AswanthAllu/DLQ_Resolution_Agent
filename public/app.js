// IBM MQ & ACE DLQ Resolution Console - Frontend Logic

let currentMessages = [];
let selectedMessage = null;

// DOM Elements
const qmgrInput = document.getElementById('qmgr');
const hostInput = document.getElementById('host');
const portInput = document.getElementById('port');
const channelInput = document.getElementById('channel');
const dlqInput = document.getElementById('dlq');
const userIdInput = document.getElementById('userId');
const passwordInput = document.getElementById('password');

const connStatusBadge = document.getElementById('connStatusBadge');
const connStatusText = document.getElementById('connStatusText');
const btnToggleConnPanel = document.getElementById('btnToggleConnPanel');
const connPanel = document.getElementById('connPanel');
const connForm = document.getElementById('connForm');
const btnTestConn = document.getElementById('btnTestConn');
const testConnAlert = document.getElementById('testConnAlert');
const btnRunTriage = document.getElementById('btnRunTriage');

const metricCurDepth = document.getElementById('metricCurDepth');
const metricConfigCount = document.getElementById('metricConfigCount');
const metricTransientCount = document.getElementById('metricTransientCount');
const metricSafeCount = document.getElementById('metricSafeCount');
const messageCounter = document.getElementById('messageCounter');

const tableFilter = document.getElementById('tableFilter');
const classFilter = document.getElementById('classFilter');
const messagesTable = document.getElementById('messagesTable');
const messagesTableBody = document.getElementById('messagesTableBody');
const emptyState = document.getElementById('emptyState');
const loadingIndicator = document.getElementById('loadingIndicator');
const loadingMessage = document.getElementById('loadingMessage');
const btnReplayAllDryRun = document.getElementById('btnReplayAllDryRun');

// Modal Elements
const detailsModal = document.getElementById('detailsModal');
const btnCloseModal = document.getElementById('btnCloseModal');
const modalMsgId = document.getElementById('modalMsgId');
const detailMsgId = document.getElementById('detailMsgId');
const detailCorrelId = document.getElementById('detailCorrelId');
const detailTargetQueue = document.getElementById('detailTargetQueue');
const detailTargetQMgr = document.getElementById('detailTargetQMgr');
const detailOriginalApp = document.getElementById('detailOriginalApp');
const detailTimestamp = document.getElementById('detailTimestamp');
const detailFormatBackout = document.getElementById('detailFormatBackout');
const detailReasonBadge = document.getElementById('detailReasonBadge');
const detailClassBadge = document.getElementById('detailClassBadge');
const detailReasonDesc = document.getElementById('detailReasonDesc');
const detailPayload = document.getElementById('detailPayload');
const payloadLengthBadge = document.getElementById('payloadLengthBadge');
const btnCopyPayload = document.getElementById('btnCopyPayload');
const aceLogsContainer = document.getElementById('aceLogsContainer');
const detailReplaySafety = document.getElementById('detailReplaySafety');
const detailReplayAdvice = document.getElementById('detailReplayAdvice');
const chkForceReplay = document.getElementById('chkForceReplay');
const btnModalDryRun = document.getElementById('btnModalDryRun');
const btnModalReplay = document.getElementById('btnModalReplay');
const btnTogglePassword = document.getElementById('btnTogglePassword');

// Toast helper
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 200);
  }, 4000);
}

// Toggle password visibility
btnTogglePassword.addEventListener('click', () => {
  const isPass = passwordInput.getAttribute('type') === 'password';
  passwordInput.setAttribute('type', isPass ? 'text' : 'password');
  btnTogglePassword.textContent = isPass ? '🔒' : '👁️';
});

// Toggle Connection Panel
btnToggleConnPanel.addEventListener('click', () => {
  connPanel.classList.toggle('hidden');
});

// Format actionable error diagnostics for IBM MQ connection issues
function formatConnectionError(rawError, cfg) {
  let errText = rawError || 'Unknown connection failure';
  let title = 'Connection Failure';
  let cause = errText;
  let solution = '';
  let command = '';

  if (errText.includes('MQRC_SECURITY_ERROR')) {
    title = 'Windows Domain Security Lookup Failed (Reason 2063)';
    cause = `Queue Manager '${cfg.qmgr}' running under local service user 'MUSR_MQADMIN' cannot verify domain user credentials via Active Directory.`;
    solution = `In IBM MQ Explorer for '${cfg.qmgr}', right-click channel '${cfg.channel}' &rarr; Properties &rarr; General tab &rarr; Set <strong>MCA user ID</strong> to <code>MUSR_MQADMIN</code>.`;
    command = `ALTER CHANNEL(${cfg.channel}) CHLTYPE(SVRCONN) MCAUSER('MUSR_MQADMIN')`;
  } else if (errText.includes('MQRC_NOT_AUTHORIZED')) {
    title = 'Connection Blocked by Security Rule (Reason 2035)';
    cause = `IBM MQ default Channel Authentication (CHLAUTH) or Connection Authentication is blocking client connections on '${cfg.channel}'.`;
    solution = `Disable CHLAUTH on '${cfg.qmgr}' for local testing, or remove the *MQADMIN blocking rule in Channel Authentication Records.`;
    command = `ALTER QMGR CHLAUTH(DISABLED)`;
  } else if (errText.includes('MQRC_UNKNOWN_CHANNEL_NAME')) {
    title = 'Channel Not Found (Reason 2540)';
    cause = `Channel '${cfg.channel}' does not exist on Queue Manager '${cfg.qmgr}'. Verify spelling (e.g. check for 'SVRCONN' vs 'SVRCON').`;
    solution = `Create the channel '${cfg.channel}' on '${cfg.qmgr}'.`;
    command = `DEFINE CHANNEL(${cfg.channel}) CHLTYPE(SVRCONN) TRPTYPE(TCP) MCAUSER('MUSR_MQADMIN')`;
  } else if (errText.includes('MQRC_Q_MGR_NAME_ERROR')) {
    title = 'Queue Manager & Port Mismatch (Reason 2058)';
    cause = `Port ${cfg.port} belongs to a different Queue Manager than '${cfg.qmgr}'.`;
    solution = `Check port assignment: Port 1414 is standard for QMGR; Port 1415 is for QMGR2.`;
  } else if (errText.includes('MQRC_Q_MGR_NOT_AVAILABLE')) {
    title = 'Queue Manager Unavailable (Reason 2059)';
    cause = `Queue Manager '${cfg.qmgr}' or its TCP Listener on port ${cfg.port} is not running.`;
    solution = `Start the Queue Manager and listener: <code>strmqm ${cfg.qmgr}</code>.`;
    command = `START LISTENER(SYSTEM.DEFAULT.LISTENER.TCP)`;
  }

  return `
    <div class="diag-card">
      <div class="diag-header">
        <span>⚠️</span> <span>${title}</span>
      </div>
      <div class="diag-body">
        <p><strong>Cause:</strong> ${cause}</p>
        <p style="margin-top: 0.3rem;"><strong>Solution:</strong> ${solution}</p>
      </div>
      ${command ? `
        <div class="diag-code-box">
          <span class="diag-code-text">${command}</span>
          <button type="button" class="btn btn-xs" onclick="navigator.clipboard.writeText('${command}'); showToast('Command copied!', 'info');">Copy Fix</button>
        </div>
      ` : ''}
    </div>
  `;
}

// Queue Manager Tab Switching Function
window.switchQMgrTab = function(qmgrName, portNumber, runNow = true) {
  // 1. Update form inputs
  qmgrInput.value = qmgrName;
  portInput.value = portNumber;
  channelInput.value = 'SYSTEM.ADMIN.SVRCONN'; // Ensure exact channel name

  // 2. Update Tab UI States
  document.querySelectorAll('.qmgr-tab').forEach(tab => {
    tab.classList.toggle('active', tab.id === `tab${qmgrName}`);
  });
  document.querySelectorAll('.btn-preset').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.includes(qmgrName));
  });

  // 3. Update Currently Viewing Banner
  const banner = document.getElementById('activeQMgrBanner');
  if (banner) banner.textContent = `${qmgrName} (Port ${portNumber})`;

  // 4. Clear out previous Queue Manager data immediately
  currentMessages = [];
  messagesTableBody.innerHTML = '';
  messagesTable.classList.add('hidden');
  emptyState.classList.remove('hidden');
  metricCurDepth.textContent = '-';
  metricConfigCount.textContent = '0';
  metricTransientCount.textContent = '0';
  metricSafeCount.textContent = '0';
  messageCounter.textContent = `Loading ${qmgrName}...`;
  testConnAlert.className = 'alert-box hidden';
  testConnAlert.innerHTML = '';

  showToast(`Switching to ${qmgrName} (Port ${portNumber})...`, 'info');

  // 5. Run full triage immediately for the selected Queue Manager
  if (runNow) runTriage();
};

// Also alias applyPreset
window.applyPreset = window.switchQMgrTab;

// Populate the dashboard from IBM MQ instead of using hard-coded managers.
async function loadRunningQueueManagers(selectedName, selectedPort) {
  const tabs = document.getElementById('qmgrTabs');
  if (!tabs) return;
  try {
    const response = await fetch('/api/queue-managers');
    const responseText = await response.text();
    let result;
    try {
      result = JSON.parse(responseText);
    } catch (_) {
      throw new Error(`Queue manager API returned HTTP ${response.status}. Restart server.py.`);
    }
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (!result.success) throw new Error(result.error || 'IBM MQ discovery failed');
    tabs.innerHTML = '';
    if (!result.queueManagers.length) {
      tabs.innerHTML = '<span class="text-muted">No running queue managers found.</span>';
      qmgrInput.value = '';
      const banner = document.getElementById('activeQMgrBanner');
      if (banner) banner.textContent = 'None running';
      return;
    }
    result.queueManagers.forEach(manager => {
      const managerName = manager.qmgr || manager.name;
      const managerHost = manager.host || hostInput.value || 'localhost';
      const managerPort = manager.port || parseInt(portInput.value, 10) || 1414;
      const managerChannel = manager.channel || channelInput.value || 'SYSTEM.ADMIN.SVRCONN';
      const managerDlq = manager.dlq || dlqInput.value || 'SYSTEM.DEAD.LETTER.QUEUE';
      const button = document.createElement('button');
      button.type = 'button';
      button.id = `tab${managerName}`;
      button.className = 'qmgr-tab';
      button.innerHTML = `<span class="tab-dot"></span><div class="tab-info"><span class="tab-name">${managerName}</span><span class="tab-detail">Running</span></div>`;
      button.addEventListener('click', () => {
        hostInput.value = managerHost;
        portInput.value = managerPort;
        channelInput.value = managerChannel;
        dlqInput.value = managerDlq;
        switchQMgrTab(managerName, managerPort);
      });
      tabs.appendChild(button);
    });
    const active = result.queueManagers.find(item => (item.qmgr || item.name) === selectedName) || result.queueManagers[0];
    // Always replace the saved value with a queue manager that is running now.
    // This makes a browser refresh reflect the current IBM MQ state.
    const activeName = active.qmgr || active.name;
    const activePort = activeName === selectedName && selectedPort
      ? selectedPort
      : (parseInt(portInput.value, 10) || 1414);
    hostInput.value = active.host || hostInput.value || 'localhost';
    portInput.value = active.port || activePort;
    channelInput.value = active.channel || channelInput.value || 'SYSTEM.ADMIN.SVRCONN';
    dlqInput.value = active.dlq || dlqInput.value || 'SYSTEM.DEAD.LETTER.QUEUE';
    switchQMgrTab(activeName, Number(portInput.value), true);
  } catch (error) {
    tabs.innerHTML = `<span class="text-muted">Unable to discover queue managers: ${error.message}</span>`;
  }
}

// Get Connection Payload from Form
function getConnectionConfig() {
  return {
    qmgr: qmgrInput.value.trim(),
    host: hostInput.value.trim(),
    port: parseInt(portInput.value.trim(), 10) || 1414,
    channel: channelInput.value.trim(),
    dlq: dlqInput.value.trim(),
    userId: userIdInput.value.trim(),
    password: passwordInput.value
  };
}

// Load default config from backend on startup
async function loadInitialConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (data.host) hostInput.value = data.host;
    if (data.port) portInput.value = data.port;
    if (data.channel) channelInput.value = data.channel;
    if (data.dlq) dlqInput.value = data.dlq;
    if (data.userId) userIdInput.value = data.userId;
    if (data.password) passwordInput.value = data.password;
    
    // Queue manager name is deliberately obtained from IBM MQ, not restored
    // from .env. This runs on every page load/refresh.
    await loadRunningQueueManagers('', data.port || 1414);
  } catch (err) {
    console.warn('Could not load .env config:', err);
  }
}

// Test Connection Function
async function testConnection() {
  const cfg = getConnectionConfig();
  btnTestConn.disabled = true;
  btnTestConn.textContent = 'Testing...';
  testConnAlert.className = 'alert-box hidden';
  testConnAlert.innerHTML = '';

  try {
    const res = await fetch('/api/test-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg)
    });
    const result = await res.json();
    
    if (result.success) {
      testConnAlert.className = 'alert-box alert-success';
      testConnAlert.innerHTML = `<strong>Connected Successfully!</strong> Connected to <strong>${cfg.qmgr}</strong> on ${cfg.host}:${cfg.port}. Current DLQ Depth: <strong>${result.curDepth}</strong> message(s).`;
      setConnStatus(true, cfg.qmgr);
      showToast(`Connected to ${cfg.qmgr}!`, 'success');
    } else {
      testConnAlert.className = 'alert-box alert-error';
      testConnAlert.innerHTML = formatConnectionError(result.error, cfg);
      setConnStatus(false);
      showToast(`Connection failed`, 'error');
    }
  } catch (err) {
    testConnAlert.className = 'alert-box alert-error';
    testConnAlert.innerHTML = `<strong>Network Error:</strong> ${err.message}`;
    setConnStatus(false);
  } finally {
    btnTestConn.disabled = false;
    btnTestConn.textContent = 'Test Connection';
  }
}

btnTestConn.addEventListener('click', testConnection);

function setConnStatus(connected, qmgr = '') {
  if (connected) {
    connStatusBadge.className = 'status-pill status-connected';
    connStatusText.textContent = `Connected: ${qmgr}`;
  } else {
    connStatusBadge.className = 'status-pill status-disconnected';
    connStatusText.textContent = 'Disconnected';
  }
}

// Run Triage
async function runTriage() {
  const cfg = getConnectionConfig();
  loadingIndicator.classList.remove('hidden');
  loadingMessage.textContent = `Analyzing Dead Letter Queue on ${cfg.qmgr}...`;
  emptyState.classList.add('hidden');
  messagesTable.classList.add('hidden');
  btnRunTriage.disabled = true;

  try {
    const res = await fetch('/api/triage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg)
    });
    const result = await res.json();

    if (!result.success) {
      connPanel.classList.remove('hidden'); // Reveal connection panel with diagnostic card
      testConnAlert.className = 'alert-box alert-error';
      testConnAlert.innerHTML = formatConnectionError(result.error, cfg);
      showToast(`Triage failed for ${cfg.qmgr}`, 'error');
      setConnStatus(false);
      currentMessages = [];
      renderTable();
      metricCurDepth.textContent = '0';
      metricConfigCount.textContent = '0';
      metricTransientCount.textContent = '0';
      metricSafeCount.textContent = '0';
      messageCounter.textContent = `0 messages (${cfg.qmgr} disconnected)`;
      emptyState.classList.remove('hidden');
      return;
    }

    testConnAlert.className = 'alert-box hidden';
    testConnAlert.innerHTML = '';
    setConnStatus(true, cfg.qmgr);
    currentMessages = result.messages || [];
    metricCurDepth.textContent = result.curDepth;

    // Calculate metrics
    let configCount = 0;
    let transientCount = 0;
    let safeCount = 0;

    currentMessages.forEach(m => {
      if (m.failureClass === 'Configuration') configCount++;
      if (m.failureClass === 'Transient') transientCount++;
      if (m.isSafeToRetry) safeCount++;
    });

    metricConfigCount.textContent = configCount;
    metricTransientCount.textContent = transientCount;
    metricSafeCount.textContent = safeCount;
    messageCounter.textContent = `${currentMessages.length} message(s) analyzed`;

    renderTable();
    showToast(`Triaged ${currentMessages.length} messages on ${cfg.qmgr}`, 'success');

  } catch (err) {
    showToast(`Failed to execute triage: ${err.message}`, 'error');
    setConnStatus(false);
  } finally {
    loadingIndicator.classList.add('hidden');
    btnRunTriage.disabled = false;
  }
}

// Form submit runs triage
connForm.addEventListener('submit', (e) => {
  e.preventDefault();
  runTriage();
});
btnRunTriage.addEventListener('click', runTriage);

// Render Messages Table
function renderTable() {
  const query = tableFilter.value.toLowerCase();
  const classFilterVal = classFilter.value;

  const filtered = currentMessages.filter(m => {
    const matchClass = (classFilterVal === 'ALL') || (m.failureClass === classFilterVal);
    const matchQuery = !query || 
      (m.msgId && m.msgId.toLowerCase().includes(query)) ||
      (m.targetQueue && m.targetQueue.toLowerCase().includes(query)) ||
      (m.reasonName && m.reasonName.toLowerCase().includes(query)) ||
      (m.payloadText && m.payloadText.toLowerCase().includes(query));
    return matchClass && matchQuery;
  });

  messagesTableBody.innerHTML = '';

  if (filtered.length === 0) {
    emptyState.classList.remove('hidden');
    messagesTable.classList.add('hidden');
    return;
  }

  emptyState.classList.add('hidden');
  messagesTable.classList.remove('hidden');

  filtered.forEach(m => {
    const tr = document.createElement('tr');
    
    // Class Badge
    let classBadgeClass = 'badge-warning';
    if (m.failureClass === 'Transient') classBadgeClass = 'badge-info';
    if (m.failureClass === 'Data') classBadgeClass = 'badge-error';

    // Safe Retry Badge
    const safeBadge = m.isSafeToRetry
      ? '<span class="badge badge-success">YES</span>'
      : '<span class="badge badge-error">NO</span>';

    tr.innerHTML = `
      <td>${m.index}</td>
      <td class="font-mono text-sm">${m.msgId ? m.msgId.substring(0, 16) + '...' : '-'}</td>
      <td class="highlight font-mono">${m.targetQueue || '-'}</td>
      <td>${m.targetQMgr || '-'}</td>
      <td><span class="badge badge-error font-mono">${m.reasonCode || ''} ${m.reasonName || ''}</span></td>
      <td><span class="badge ${classBadgeClass}">${m.failureClass || 'Unknown'}</span></td>
      <td class="text-muted text-sm">${m.putDateTime || '-'}</td>
      <td>${safeBadge}</td>
      <td>
        <button class="btn btn-xs btn-primary btn-inspect" data-id="${m.msgId}">Inspect</button>
      </td>
    `;
    messagesTableBody.appendChild(tr);
  });

  // Attach inspect listeners
  document.querySelectorAll('.btn-inspect').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const msgId = e.currentTarget.getAttribute('data-id');
      const msg = currentMessages.find(m => m.msgId === msgId);
      if (msg) openModal(msg);
    });
  });
}

tableFilter.addEventListener('input', renderTable);
classFilter.addEventListener('change', renderTable);

// Open Details Modal (8-Step Details)
function openModal(m) {
  selectedMessage = m;
  modalMsgId.textContent = `Message: ${m.msgId}`;
  detailMsgId.textContent = m.msgId;
  detailCorrelId.textContent = m.correlId || '000000000000000000000000000000000000000000000000';
  detailTargetQueue.textContent = m.targetQueue || 'N/A';
  detailTargetQMgr.textContent = m.targetQMgr || 'N/A';
  detailOriginalApp.textContent = m.originalApp || 'N/A';
  detailTimestamp.textContent = m.putDateTime || 'N/A';
  detailFormatBackout.textContent = `${m.format || 'MQDEAD'} (Backout Count: ${m.backoutCount || 0})`;

  // Error Details
  detailReasonBadge.textContent = `${m.reasonCode} ${m.reasonName}`;
  detailClassBadge.textContent = m.failureClass;
  detailReasonDesc.textContent = m.reasonDesc;

  // Payload
  detailPayload.textContent = m.payloadText || '[Empty or Non-string Payload]';
  payloadLengthBadge.textContent = `${m.payloadLength || 0} bytes`;

  // ACE Logs
  aceLogsContainer.innerHTML = '';
  if (m.aceEvents && m.aceEvents.length > 0) {
    m.aceEvents.forEach(ev => {
      const div = document.createElement('div');
      div.className = 'ace-event-item';
      div.innerHTML = `<strong>[Event ${ev.id}]</strong> ${ev.time}<br><span>${ev.message}</span>`;
      aceLogsContainer.appendChild(div);
    });
  } else {
    aceLogsContainer.innerHTML = '<p class="text-muted italic">No matching ACE BIP error logs found in Windows Application log for this timestamp.</p>';
  }

  // Replay Safety
  if (m.isSafeToRetry) {
    detailReplaySafety.className = 'badge badge-success';
    detailReplaySafety.textContent = 'SAFE FOR AUTOMATED REPLAY';
    detailReplayAdvice.textContent = 'This issue was classified as transient. Safe to replay back to target queue.';
    chkForceReplay.checked = false;
  } else {
    detailReplaySafety.className = 'badge badge-error';
    detailReplaySafety.textContent = 'MANUAL ACTION REQUIRED';
    detailReplayAdvice.textContent = `Reason ${m.reasonCode} (${m.reasonName}): Target queue '${m.targetQueue}' must exist before replaying. If you already created it, check 'Force Replay' below.`;
  }

  detailsModal.classList.remove('hidden');
}

// Close Modal
btnCloseModal.addEventListener('click', () => {
  detailsModal.classList.add('hidden');
});
detailsModal.addEventListener('click', (e) => {
  if (e.target === detailsModal) detailsModal.classList.add('hidden');
});

// Copy Payload
btnCopyPayload.addEventListener('click', () => {
  if (selectedMessage && selectedMessage.payloadText) {
    navigator.clipboard.writeText(selectedMessage.payloadText);
    showToast('Payload copied to clipboard!', 'success');
  }
});

// Execute Replay from Modal
async function executeReplay(dryRun) {
  if (!selectedMessage) return;
  const cfg = getConnectionConfig();
  const force = chkForceReplay.checked;

  const replayBtn = dryRun ? btnModalDryRun : btnModalReplay;
  const originalText = replayBtn.textContent;
  replayBtn.disabled = true;
  replayBtn.textContent = dryRun ? 'Simulating...' : 'Replaying...';

  try {
    const res = await fetch('/api/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...cfg,
        targetMsgId: selectedMessage.msgId,
        dryRun: dryRun,
        forceReplay: force
      })
    });
    const result = await res.json();
    
    if (result.success) {
      if (dryRun) {
        showToast(`[Dry Run] Simulated replay to '${selectedMessage.targetQueue}' successfully.`, 'info');
      } else {
        showToast(`[Replay Success] Message replayed to '${selectedMessage.targetQueue}'!`, 'success');
        detailsModal.classList.add('hidden');
        runTriage(); // Refresh
      }
    } else {
      showToast(`Replay failed: ${result.error}`, 'error');
    }
  } catch (err) {
    showToast(`Replay request failed: ${err.message}`, 'error');
  } finally {
    replayBtn.disabled = false;
    replayBtn.textContent = originalText;
  }
}

btnModalDryRun.addEventListener('click', () => executeReplay(true));
btnModalReplay.addEventListener('click', () => {
  if (confirm(`Are you sure you want to replay message to target queue '${selectedMessage.targetQueue}'?`)) {
    executeReplay(false);
  }
});

// Replay All Dry-Run
btnReplayAllDryRun.addEventListener('click', async () => {
  const cfg = getConnectionConfig();
  showToast('Simulating dry-run replay for all eligible messages...', 'info');
  try {
    const res = await fetch('/api/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...cfg,
        dryRun: true,
        forceReplay: false
      })
    });
    const result = await res.json();
    if (result.success) {
      showToast('Dry run completed! Check console for results.', 'success');
    } else {
      showToast(`Dry run failed: ${result.error}`, 'error');
    }
  } catch (err) {
    showToast(`Dry run error: ${err.message}`, 'error');
  }
});

// Initialize on page load
loadInitialConfig();
