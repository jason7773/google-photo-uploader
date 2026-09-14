/* ── gp-p1-mig Web UI — Frontend Logic ── */

const appToken = document.querySelector('meta[name="app-token"]').content;
const socket = io({ auth: { token: appToken } });

// ── State ──
let appState = {};
window.addEventListener('unhandledrejection', e => { addLog('ERROR', e.reason?.message || String(e.reason)); e.preventDefault(); });

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
        headers: { 'Content-Type': 'application/json', 'X-App-Token': appToken },
        body: body ? JSON.stringify(body) : '{}',
    }).then(r => r.json()).then(data => {
        if (!data.ok) throw new Error(data.error || '操作失敗');
        return data;
    });
}

function refresh() {
    fetch('/api/state').then(r => r.json()).then(data => {
        appState = data;
        // Stats
        const s = data.stats || {};
        document.getElementById('statTotal').textContent = s.total || 0;
        document.getElementById('statNew').textContent = s.new_count || 0;
        document.getElementById('statReady').textContent = s.ready || 0;
        document.getElementById('statPatched').textContent = s.patched || 0;
        document.getElementById('statBatched').textContent = s.batched || 0;
        document.getElementById('statUploaded').textContent = s.uploaded || 0;
        document.getElementById('statFailed').textContent = s.failed || 0;

        // Tools
        const toolDiv = document.getElementById('toolStatus');
        const tools = data.tools || {};
        let html = '';
        for (const [name, info] of Object.entries(tools)) {
            const ok = info.path && info.path !== 'NOT FOUND';
            html += `<div class="tool-badge ${ok ? 'ok' : 'missing'}">${ok ? '✓' : '✗'} ${name}</div>`;
        }
        toolDiv.innerHTML = html;

        // Batches
        renderBatches(data.batches || []);

        // Settings
        if (!document.getElementById('settingRoot').dataset.dirty) {
            document.getElementById('settingRoot').value = data.root || '';
            document.getElementById('settingDb').value = data.db || '';
        }
        document.getElementById('workspaceError').textContent = data.db_error || '';
        setButtonsDisabled(!!data.busy);
    }).catch(() => { });
}

// ── XSS escape helper ──
function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function renderBatches(batches) {
    const tbody = document.getElementById('batchTableBody');
    if (!batches.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">尚無批次</td></tr>';
        return;
    }
    tbody.innerHTML = batches.map(b => `
    <tr>
      <td><strong>${esc(b.batch_id)}</strong></td>
      <td><span class="status-badge ${esc(b.status)}">${esc(b.status)}</span></td>
      <td>${esc(b.total_files)}</td>
      <td>${formatBytes(b.total_bytes)}</td>
      <td>${esc(b.created_at || '')}</td>
      <td>
        ${b.status === 'CREATED' ? `<button class="btn small" onclick="batchAction('push','${esc(b.batch_id)}')">Push</button>` : ''}
        ${b.status === 'PUSHED' ? `<button class="btn small" onclick="verifyBatch('${esc(b.batch_id)}')">Verify</button>` : ''}
        ${b.status === 'VERIFIED' ? `<button class="btn small danger" onclick="batchAction('purge','${esc(b.batch_id)}')">移動副本到回收目錄</button>` : ''}
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
    const raw = document.getElementById('ingestZipPath').value.trim();
    if (!raw) { addLog('ERROR', '請輸入至少一個 ZIP 路徑'); return; }
    // Split by newline or semicolon, strip quotes and whitespace, filter empties
    const paths = raw.split(/[\n;]+/)
        .map(p => p.trim().replace(/^["']|["']$/g, ''))
        .filter(p => p.length > 0);
    if (!paths.length) { addLog('ERROR', '請輸入至少一個 ZIP 路徑'); return; }
    closeModal('ingestModal');
    addLog('INFO', '匯入 ' + paths.length + ' 個 ZIP: ' + paths.map(p => p.split('\\').pop()).join(', '));
    api('ingest', { zip_paths: paths });
}

// ── Batch Actions ──
let currentVerifyBatchId = null;

function openVerifyModal(batchId) {
    currentVerifyBatchId = batchId;
    const list = document.getElementById('verifyList');
    list.innerHTML = '<div style="padding:20px;text-align:center">載入中...</div>';
    // No longer need to disable button - it's always enabled
    document.getElementById('verifyConfirmBtn').disabled = false;

    // Fix: Use classList to toggle visibility so flex centering works
    // document.getElementById('verifyModal').style.display = 'block'; 
    document.getElementById('verifyModal').classList.add('show');

    fetch('/api/batch-samples?batch_id=' + batchId)
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                renderVerifyList(data.samples || []);
            } else {
                list.textContent = '無法取得範例: ' + data.error;
            }
        });
}

function renderVerifyList(samples) {
    const list = document.getElementById('verifyList');
    if (!samples.length) {
        list.innerHTML = '<div style="padding:20px;text-align:center">此批次無檔案</div>';
        return;
    }
    list.replaceChildren();
    for (const name of samples) {
        const item = document.createElement('div');
        item.className = 'verify-item';
        const label = document.createElement('span');
        label.textContent = name;
        const button = document.createElement('button');
        button.className = 'copy-btn';
        button.textContent = '複製';
        button.addEventListener('click', () => copyToClipboard(name));
        item.append(label, button);
        list.append(item);
    }
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        // Optional: show a brief success indicator
    });
}

function confirmVerifyBatch() {
    if (!currentVerifyBatchId) return;
    verifyBatch(currentVerifyBatchId);
    closeVerifyModal();
}

function closeVerifyModal() {
    document.getElementById('verifyModal').classList.remove('show');
    currentVerifyBatchId = null;
}

function verifyBatch(batchId) {
    if (!confirm('確認要將批次 ' + batchId + ' 標記為 VERIFIED 嗎？\n\n請先在 Google Photos 確認檔案已成功上傳。')) {
        return;
    }

    api('mark-verified', { batch_id: batchId }).then(() => {
        addLog('INFO', '✅ 批次 ' + batchId + ' 已標記為 VERIFIED');
        refresh();
    }).catch(err => {
        addLog('ERROR', 'Verify 失敗: ' + err);
    });
}

function makeBatch() {
    const maxFiles = parseInt(document.getElementById('batchMaxFiles').value) || 1000;
    const maxBytes = parseInt(document.getElementById('batchMaxBytes').value) || 10737418240;
    api('make-batch', { max_files: maxFiles, max_bytes: maxBytes });
}

function cleanDuplicates() {
    if (!confirm('確定要清理重複檔案嗎？\n\n安全機制：\n- 已在 media_items 中的檔案不會被移動\n- 檔案會移至 duplicates_trash 資料夾（不永久刪除）')) return;

    addLog('INFO', '⏳ 正在清理重複檔案...');
    api('clean-duplicates', {});
}

function batchAction(action, batchId) {
    if (action === 'push') {
        const devicePath = '/sdcard/DCIM/Camera'; // defaulting to what user wants
        // If we want the modal back, we can uncomment previous logic
        // But for "User Experience", maybe just pushing to default is faster?
        // Let's stick to the modal for safety (user checks USB)
        // Re-implementing the simple push confirmation
        if (confirm('確認 Push 到 /sdcard/DCIM/Camera ?')) {
            api('push', { batch_id: batchId, device_path: devicePath });
        }
    } else if (action === 'purge') {
        fetch('/api/purge-preview?batch_id=' + encodeURIComponent(batchId))
            .then(r => r.json()).then(data => {
                if (!data.ok) throw new Error(data.error);
                const preview = data.plan;
                const message = preview.note + '\n\n即將移動 ' + preview.count + ' 個檔案／目錄：\n' + preview.paths.slice(0, 20).join('\n') + (preview.count > 20 ? '\n（其餘項目省略，完整清單可由預覽 API 查看）' : '');
                if (confirm(message)) return api('purge', { batch_id: batchId });
            });
    }
}

// ── Settings ──
function saveSettings() {
    const root = document.getElementById('settingRoot').value.trim();
    const db = document.getElementById('settingDb').value.trim();
    api('settings', { root, db }).then(() => {
        delete document.getElementById('settingRoot').dataset.dirty;
        addLog('INFO', '✅ 設定已儲存');
        refresh();
    });
}

for (const id of ['settingRoot', 'settingDb']) {
    document.getElementById(id).addEventListener('input', () => { document.getElementById('settingRoot').dataset.dirty = '1'; });
}
// ── Init ──
refresh();
setInterval(refresh, 10000);
