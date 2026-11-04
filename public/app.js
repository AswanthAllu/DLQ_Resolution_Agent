// IBM MQ DLQ Resolution Agent - Streamlined Frontend Logic

let currentMessages = [];
let pollTimer = null;
let isFetching = false;
const analysisCache = new Map();
const expandedAnalysisRows = new Set();

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
const btnTestConn = document.getElementById('btnTestConn');
const btnSaveConfig = document.getElementById('btnSaveConfig');
const testConnAlert = document.getElementById('testConnAlert');

const metricCurDepth = document.getElementById('metricCurDepth');
const tableFilter = document.getElementById('tableFilter');
const messagesTable = document.getElementById('messagesTable');
const messagesTableBody = document.getElementById('messagesTableBody');
const emptyState = document.getElementById('emptyState');
const loadingIndicator = document.getElementById('loadingIndicator');
const btnTogglePassword = document.getElementById('btnTogglePassword');

// Toast helper
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 200);
  }, 3500);
}

// Toggle password visibility
if (btnTogglePassword && passwordInput) {
  btnTogglePassword.addEventListener('click', () => {
    const isPass = passwordInput.getAttribute('type') === 'password';
    passwordInput.setAttribute('type', isPass ? 'text' : 'password');
    btnTogglePassword.textContent = isPass ? '🔒' : '👁️';
  });
}

// Toggle Configuration Settings Panel
if (btnToggleConnPanel && connPanel) {
  btnToggleConnPanel.addEventListener('click', () => {
    connPanel.classList.toggle('hidden');
  });
}

// Gather config object from form inputs
function getConnectionConfig() {
  return {
    qmgr: (qmgrInput.value || '').trim(),
    host: (hostInput.value || '').trim() || 'localhost',
    port: parseInt((portInput.value || '1415').trim(), 10) || 1415,
    channel: (channelInput.value || '').trim() || 'SYSTEM.ADMIN.SVRCONN',
    dlq: (dlqInput.value || '').trim() || 'SYSTEM.DEAD.LETTER.QUEUE',
    userId: (userIdInput.value || '').trim(),
    password: passwordInput.value || ''
  };
}

function setConnStatus(connected, qmgr = '') {
  if (connected) {
    connStatusBadge.className = 'status-pill status-connected';
    connStatusText.textContent = `Connected: ${qmgr || 'MQ'}`;
  } else {
    connStatusBadge.className = 'status-pill status-disconnected';
    connStatusText.textContent = 'Disconnected';
  }
}

// 1. Load initial config from server (reads config.json first, then .env)
async function loadInitialConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (data.qmgr) qmgrInput.value = data.qmgr;
    if (data.host) hostInput.value = data.host;
    if (data.port) portInput.value = data.port;
    if (data.channel) channelInput.value = data.channel;
    if (data.dlq) dlqInput.value = data.dlq;
    if (data.userId) userIdInput.value = data.userId;
    if (data.password) passwordInput.value = data.password;
  } catch (err) {
    console.warn('Could not load initial config:', err);
  }
}

// 2. Save settings button (persists to config.json; takes precedence over .env)
if (btnSaveConfig) {
  btnSaveConfig.addEventListener('click', async () => {
    const cfg = getConnectionConfig();
    btnSaveConfig.disabled = true;
    btnSaveConfig.textContent = 'Saving...';
    try {
      const res = await fetch('/api/config/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg)
      });
      const data = await res.json();
      if (data.success) {
        showToast('Settings saved successfully! Active config updated.', 'success');
        // Immediately fetch messages with newly saved connection settings
        fetchDlqMessagesDirect(true);
      } else {
        showToast(`Save failed: ${data.error}`, 'error');
      }
    } catch (err) {
      showToast(`Network error saving settings: ${err.message}`, 'error');
    } finally {
      btnSaveConfig.disabled = false;
      btnSaveConfig.textContent = 'Save Settings';
    }
  });
}

// 3. Test connection button
if (btnTestConn) {
  btnTestConn.addEventListener('click', async () => {
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
        testConnAlert.innerHTML = `<strong>Connected!</strong> Queue Manager <strong>${cfg.qmgr}</strong> on ${cfg.host}:${cfg.port}. Current DLQ Depth: <strong>${result.curDepth}</strong>.`;
        setConnStatus(true, cfg.qmgr);
        showToast(`Connected to ${cfg.qmgr}!`, 'success');
        metricCurDepth.textContent = result.curDepth;
      } else {
        testConnAlert.className = 'alert-box alert-error';
        testConnAlert.textContent = `Connection failed: ${result.error || 'Unknown error'}`;
        setConnStatus(false);
        showToast('Connection failed', 'error');
      }
    } catch (err) {
      testConnAlert.className = 'alert-box alert-error';
      testConnAlert.textContent = `Network error: ${err.message}`;
      setConnStatus(false);
    } finally {
      btnTestConn.disabled = false;
      btnTestConn.textContent = 'Test Connection';
    }
  });
}

// 4. Fetch messages directly from connected IBM MQ DLQ (NOT from sqlite database)
async function fetchDlqMessagesDirect(showSpinner = false) {
  if (isFetching) return;
  isFetching = true;
  const cfg = getConnectionConfig();

  if (showSpinner && currentMessages.length === 0) {
    loadingIndicator.classList.remove('hidden');
    emptyState.classList.add('hidden');
    messagesTable.classList.add('hidden');
  }

  try {
    const res = await fetch('/api/triage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg)
    });
    const result = await res.json();

    if (result.success) {
      setConnStatus(true, result.qmgr || cfg.qmgr);
      metricCurDepth.textContent = result.curDepth != null ? result.curDepth : (result.messages || []).length;
      currentMessages = result.messages || [];
      renderTable();
    } else {
      setConnStatus(false);
      // If error occurs, keep last depth or show disconnected
      console.warn('Direct DLQ fetch reported error:', result.error);
    }
  } catch (err) {
    setConnStatus(false);
    console.warn('Network error fetching from DLQ:', err);
  } finally {
    isFetching = false;
    loadingIndicator.classList.add('hidden');
  }
}

// Lightweight Markdown Renderer with Copyable Code Blocks
function renderMarkdown(md) {
  if (!md) return '<p class="text-muted italic">No content generated.</p>';

  let html = md
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Code blocks: ```lang ... ```
  html = html.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (match, lang, code) => {
    return `<pre><code class="language-${lang}">${code.trim()}</code></pre>`;
  });

  // Headers
  html = html.replace(/^#### (.*$)/gim, '<h4>$1</h4>');
  html = html.replace(/^### (.*$)/gim, '<h3>$1</h3>');
  html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
  html = html.replace(/^# (.*$)/gim, '<h1>$1</h1>');

  // Inline formatting
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Unordered list items
  html = html.replace(/^\s*[-*]\s+(.*$)/gim, '<li>$1</li>');
  html = html.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
  html = html.replace(/<\/ul>\s*<ul>/g, ''); // merge adjacent lists

  // Line breaks & paragraphs
  html = html.replace(/\n\n+/g, '</p><p>');
  html = `<p>${html}</p>`;
  html = html.replace(/<p>\s*<\/p>/g, '');
  html = html.replace(/<p>\s*(<h[1-4]>)/g, '$1');
  html = html.replace(/(<\/h[1-4]>)\s*<\/p>/g, '$1');
  html = html.replace(/<p>\s*(<pre>[\s\S]*?<\/pre>)\s*<\/p>/g, '$1');
  html = html.replace(/<p>\s*(<ul>[\s\S]*?<\/ul>)\s*<\/p>/g, '$1');

  return html;
}

// AI Agent Message Analysis Execution
async function analyzeDlqMessage(message, panel, btn) {
  const cacheKey = message.msgId || String(message.index);
  const cached = analysisCache.get(cacheKey);
  if (cached) {
    panel.innerHTML = renderMarkdown(cached);
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Analyzing...';
  panel.innerHTML = `
    <div class="ai-thinking" style="padding: 1rem; display: flex; align-items: center; gap: 0.75rem; background: rgba(56, 189, 248, 0.05); border: 1px dashed rgba(56, 189, 248, 0.3); border-radius: var(--radius-sm);">
      <div class="spinner" style="width: 20px; height: 20px; border-width: 2px;"></div>
      <span style="color: var(--accent-cyan); font-size: 0.88rem;">AI Agent is inspecting message metadata, MQMD headers, and diagnosing root cause...</span>
    </div>
  `;

  try {
    const response = await fetch('/api/agent/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config: getConnectionConfig(),
        messages: [message],
        customQuery: `Analyze this DLQ message thoroughly. Provide:
1. Root Cause Analysis: Exactly why did this message fail to reach its destination?
2. MQMD & Header Diagnostics: Review CCSID (${message.ccsid || 'N/A'}), Encoding (${message.encoding || 'N/A'}), Format (${message.format || 'N/A'}), Target Queue (${message.targetQueue || 'N/A'}), Reason (${message.reasonCode || '0'} ${message.reasonName || ''}).
3. Remediation Action Recommendation: Based on your analysis, recommend and highlight ONE of:
   - REPLAY: Retry processing the failed message after fixing the issue.
   - HOLD: Keep the message in the DLQ until the issue is resolved.
   - ESCALATE: Forward the issue to a human engineer for further investigation.
4. Concrete Remediation: Provide exact copy-paste-ready runmqsc / MQ commands to fix the root cause.`,
        stream: false
      })
    });
    const result = await response.json();

    if (!response.ok || !result.success) {
      throw new Error(result.error || `AI Agent returned HTTP ${response.status}`);
    }

    const analysisText = result.analysis || 'The AI Agent finished analysis with no findings.';
    analysisCache.set(cacheKey, analysisText);
    panel.innerHTML = renderMarkdown(analysisText);
  } catch (err) {
    panel.innerHTML = `
      <div class="alert-box alert-error" style="margin: 0;">
        <strong>AI Analysis Failed:</strong> ${err.message}
      </div>
    `;
    showToast(`AI Analysis error: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Hide Analysis';
  }
}

// 5. Render DLQ Messages Table
function renderTable() {
  const query = (tableFilter ? tableFilter.value : '').toLowerCase().trim();

  const filtered = currentMessages.filter(m => {
    if (!query) return true;
    return (
      (m.msgId && m.msgId.toLowerCase().includes(query)) ||
      (m.targetQueue && m.targetQueue.toLowerCase().includes(query)) ||
      (m.reasonName && m.reasonName.toLowerCase().includes(query)) ||
      (m.payloadText && m.payloadText.toLowerCase().includes(query)) ||
      (m.format && m.format.toLowerCase().includes(query))
    );
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

    let classBadgeClass = 'badge-warning';
    if (m.failureClass === 'Transient') classBadgeClass = 'badge-info';
    if (m.failureClass === 'Data') classBadgeClass = 'badge-error';

    const msgIdShort = m.msgId ? (m.msgId.length > 20 ? m.msgId.substring(0, 20) + '...' : m.msgId) : '-';
    const reasonDisplay = `${m.reasonCode || ''} ${m.reasonName || ''}`.trim() || 'No Error / 0';
    const payloadPreview = m.payloadText ? (m.payloadText.length > 60 ? m.payloadText.substring(0, 60) + '...' : m.payloadText) : '[Empty/Binary]';

    const msgKey = m.msgId || String(m.index);
    const isExpanded = expandedAnalysisRows.has(msgKey);

    tr.innerHTML = `
      <td>${m.index}</td>
      <td class="font-mono text-sm" title="${m.msgId || ''}">${msgIdShort}</td>
      <td class="highlight font-mono">${m.targetQueue || '-'}</td>
      <td>${m.targetQMgr || '-'}</td>
      <td><span class="badge badge-error font-mono">${reasonDisplay}</span></td>
      <td><span class="badge ${classBadgeClass}">${m.failureClass || 'Direct Put'}</span></td>
      <td class="text-muted text-sm">${m.putDateTime || '-'}</td>
      <td class="font-mono text-sm">${m.format || '-'}</td>
      <td class="font-mono text-sm" style="max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${(m.payloadText || '').replace(/"/g, '&quot;')}">${payloadPreview}</td>
      <td>
        <button type="button" class="btn btn-xs btn-primary btn-analyze-msg" data-key="${msgKey}">
          ${isExpanded ? 'Hide Analysis' : '⚡ Analyze'}
        </button>
      </td>
    `;

    // Dropdown analysis card row for AI Agent results
    const analysisRow = document.createElement('tr');
    analysisRow.className = `analysis-dropdown-row ${isExpanded ? '' : 'hidden'}`;
    const analysisCell = document.createElement('td');
    analysisCell.colSpan = 10;
    const panel = document.createElement('div');
    panel.className = 'message-analysis ai-output-body';
    panel.style.padding = '1.25rem';
    panel.style.background = 'rgba(15, 23, 42, 0.9)';
    panel.style.border = '1px solid rgba(56, 189, 248, 0.25)';
    panel.style.borderRadius = 'var(--radius-sm)';
    panel.style.margin = '0.5rem 0';
    
    // If cached and already expanded, render cached content
    if (isExpanded && analysisCache.has(msgKey)) {
      panel.innerHTML = renderMarkdown(analysisCache.get(msgKey));
    }

    analysisCell.appendChild(panel);
    analysisRow.appendChild(analysisCell);

    const btnAnalyze = tr.querySelector('.btn-analyze-msg');
    btnAnalyze.addEventListener('click', () => {
      const willExpand = !expandedAnalysisRows.has(msgKey);
      if (willExpand) {
        expandedAnalysisRows.add(msgKey);
        analysisRow.classList.remove('hidden');
        btnAnalyze.textContent = 'Hide Analysis';
        analyzeDlqMessage(m, panel, btnAnalyze);
      } else {
        expandedAnalysisRows.delete(msgKey);
        analysisRow.classList.add('hidden');
        btnAnalyze.textContent = '⚡ Analyze';
      }
    });

    messagesTableBody.append(tr, analysisRow);
  });
}

if (tableFilter) {
  tableFilter.addEventListener('input', renderTable);
}

// 6. Application Startup & 5-Second Periodic Polling
async function initialize() {
  await loadInitialConfig();
  // Fetch immediately on startup
  await fetchDlqMessagesDirect(true);

  // Poll DLQ directly every 5 seconds (Requirement 4)
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    fetchDlqMessagesDirect(false);
  }, 5000);
}

initialize();
