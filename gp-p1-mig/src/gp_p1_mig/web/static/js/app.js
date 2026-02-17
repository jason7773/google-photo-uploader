/* ── gp-p1-mig Web UI — Frontend Logic ── */

const socket = io();

// ── State ──
let appState = {};

// ── Socket.IO Events ──
socket.on('connect', () => {
    document.querySelector('#connStatus').innerHTML = '<span class="dot online"></span> 已連線';
    refresh();
});
socket.on('disconnect', () => {
    document.querySelector('#connStatus').innerHTML = '<span class="dot offline"></span> 未連線';
});

socket.on('log', (data) => {
    const area = document.getElementById('logArea');
    const line = document.createElement('div');
    const lvl = (data.level || '').substring(0, 5);
    line.className = 'log-line ' + lvl;
    line.textContent = data.message;
    area.appendChild(line);
    area.scrollTop = area.scrollHeight;
});

socket.on('progress', (data) => {
    const card = document.getElementById('progressCard');
    if (data.total > 0) {
        card.style.display = 'block';
        const pct = Math.round((data.current / data.total) * 100);
        document.getElementById('progressBar').style.width = pct + '%';
        document.getElementById('progressPct').textContent = pct + '%';
        document.getElementById('progressDesc').textContent = data.desc || '';
    } else {
        card.style.display = 'none';
        document.getElementById('progressBar').style.width = '0%';
    }
});

socket.on('task_start', (data) => {
    document.getElementById('progressCard').style.display = 'block';
    document.getElementById('taskLabel').textContent = '進度 — ' + data.name;
    document.getElementById('progressDesc').textContent = '執行中...';
    document.getElementById('progressBar').style.width = '0%';
    document.getElementById('progressPct').textContent = '';
    setButtonsDisabled(true);
    addLog('INFO', '▶ 開始: ' + data.name);
});

socket.on('task_done', (data) => {
    document.getElementById('progressBar').style.width = '100%';
    document.getElementById('progressPct').textContent = '100%';
    document.getElementById('progressDesc').textContent = '完成！';
    setButtonsDisabled(false);
    addLog('INFO', '✅ 完成: ' + data.name + ' → ' + JSON.stringify(data.result));
    setTimeout(refresh, 500);
    setTimeout(() => { document.getElementById('progressCard').style.display = 'none'; }, 3000);
});

socket.on('task_error', (data) => {
    document.getElementById('progressBar').style.width = '0%';
    document.getElementById('progressDesc').textContent = '失敗';
    setButtonsDisabled(false);
    addLog('ERROR', '❌ 錯誤: ' + data.name + ' — ' + data.error);
    setTimeout(refresh, 500);
});

socket.on('error', (data) => {
    addLog('ERROR', '⚠️ ' + data.message);
});

// ── Helpers ──
function addLog(level, msg) {
    const area = document.getElementById('logArea');
    const line = document.createElement('div');
    line.className = 'log-line ' + level.substring(0, 5);
    line.textContent = msg;
    area.appendChild(line);
    area.scrollTop = area.scrollHeight;
}

function clearLog() {
    document.getElementById('logArea').innerHTML = '';
}

function setButtonsDisabled(disabled) {
    document.querySelectorAll('.pipeline .btn, .form-row .btn').forEach(b => b.disabled = disabled);
}

function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

// ── API calls ──
function api(endpoint, body) {
    return fetch('/api/' + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : '{}',
    }).then(r => r.json()).then(data => {
        if (!data.ok) addLog('ERROR', data.error || '操作失敗');
        return data;
    }).catch(err => addLog('ERROR', '網路錯誤: ' + err.message));
}

function refresh() {
    fetch('/api/state').then(r => r.json()).then(data => {
        appState = data;
        // Stats
        const s = data.stats || {};
        document.getElementById('statTotal').textContent = s.total || 0;
        document.getElementById('statPatched').textContent = s.patched || 0;
        document.getElementById('statReady').textContent = s.ready || 0;
        document.getElementById('statFailed').textContent = s.failed || 0;

        // Tools
        const toolDiv = document.getElementById('toolStatus');
        const tools = data.tools || {};
        let html = '';
        for (const [name, info] of Object.entries(tools)) {
            const ok = info.path && info.path !== 'NOT_FOUND';
            html += `<div class="tool-badge ${ok ? 'ok' : 'missing'}">${ok ? '✓' : '✗'} ${name}</div>`;
        }
        toolDiv.innerHTML = html;

        // Batches
        renderBatches(data.batches || []);

        // Settings
        document.getElementById('settingRoot').value = data.root || '';
        document.getElementById('settingDb').value = data.db || '';
    }).catch(() => { });
}

function renderBatches(batches) {
    const tbody = document.getElementById('batchTableBody');
    if (!batches.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">尚無批次</td></tr>';
        return;
    }
    tbody.innerHTML = batches.map(b => `
    <tr>
      <td><strong>${b.batch_id}</strong></td>
      <td><span class="status-badge ${b.status}">${b.status}</span></td>
      <td>${b.total_files}</td>
      <td>${formatBytes(b.total_bytes)}</td>
      <td>${b.created_at || ''}</td>
      <td>
        ${b.status === 'CREATED' ? `<button class="btn small" onclick="batchAction('push','${b.batch_id}')">Push</button>` : ''}
        ${b.status === 'PUSHED' ? `<button class="btn small" onclick="batchAction('export-verify','${b.batch_id}')">Verify</button>` : ''}
        ${b.status === 'VERIFIED' ? `<button class="btn small danger" onclick="batchAction('purge','${b.batch_id}')">Purge</button>` : ''}
      </td>
    </tr>
  `).join('');
}

// ── Tab Navigation ──
document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        const tab = link.dataset.tab;
        document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        link.classList.add('active');
        document.getElementById('tab-' + tab).classList.add('active');
        if (tab === 'batches' || tab === 'dashboard') refresh();
    });
});

// ── Help Sub-navigation ──
document.querySelectorAll('.help-link').forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        const section = link.dataset.section;
        document.querySelectorAll('.help-link').forEach(l => l.classList.remove('active'));
        document.querySelectorAll('.help-section').forEach(s => s.classList.remove('active'));
        link.classList.add('active');
        document.getElementById('help-' + section).classList.add('active');
    });
});

// ── Modals ──
function showIngestModal() {
    document.getElementById('ingestModal').classList.add('show');
}

function closeModal(id) {
    document.getElementById(id).classList.remove('show');
}

function runIngest() {
    const zipPath = document.getElementById('ingestZipPath').value.trim();
    if (!zipPath) { addLog('ERROR', '請輸入 ZIP 路徑'); return; }
    closeModal('ingestModal');
    api('ingest', { zip_path: zipPath });
}

// ── Batch Actions ──
function makeBatch() {
    const maxFiles = parseInt(document.getElementById('batchMaxFiles').value) || 1000;
    const maxBytes = parseInt(document.getElementById('batchMaxBytes').value) || 10737418240;
    api('make-batch', { max_files: maxFiles, max_bytes: maxBytes });
}

function batchAction(action, batchId) {
    if (action === 'push') {
        document.getElementById('batchActionTitle').textContent = 'Push 批次到手機';
        document.getElementById('batchActionId').value = batchId;
        document.getElementById('devicePathGroup').style.display = 'block';
        document.getElementById('batchActionBtn').onclick = () => {
            closeModal('batchActionModal');
            api('push', { batch_id: batchId, device_path: document.getElementById('devicePath').value });
        };
        document.getElementById('batchActionModal').classList.add('show');
    } else if (action === 'export-verify') {
        api('export-verify', { batch_id: batchId });
    } else if (action === 'purge') {
        if (confirm('確定要清除批次 ' + batchId + ' 嗎？此操作不可復原。')) {
            api('purge', { batch_id: batchId });
        }
    }
}

// ── Settings ──
function saveSettings() {
    const root = document.getElementById('settingRoot').value.trim();
    const db = document.getElementById('settingDb').value.trim();
    api('settings', { root, db }).then(() => {
        addLog('INFO', '✅ 設定已儲存');
        refresh();
    });
}

// ── Init ──
refresh();
setInterval(refresh, 10000);
