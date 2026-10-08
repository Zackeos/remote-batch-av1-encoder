let ws = null;
let options = {};
let paths = {};
let currentFolder = '';
let currentSuggestedOut = '';
let scannedFiles = [];
let probedSubtitles = [];
let currentJobs = [];
let currentRawFiles = [];
let queueState = {is_running: false, is_paused: false};
let lastCompletedTransfers = 0;

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

async function api(url, body) {
    const opts = body === undefined
        ? {method: 'POST'}
        : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    return data;
}

async function getJson(url) {
    const res = await fetch(url);
    return res.json();
}

function formatBytes(bytes) {
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function formatEta(sec) {
    if (!sec) return '-';
    const m = Math.floor(sec / 60), s = sec % 60;
    return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
}

// --- WebSocket ---------------------------------------------------------------

function connectWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        switch (msg.event) {
            case 'init':
                options = msg.options || {};
                paths = msg.paths || {};
                if (!$('cfg-audio-language').value) $('cfg-audio-language').value = (msg.defaults || {}).audio_language || '';
                populateOptionSelects();
                document.querySelectorAll('.input-dir-label').forEach(el => el.innerText = paths.input_dir);
                currentJobs = msg.jobs || [];
                renderJobs();
                renderAutoTune(msg.autotune);
                loadFolders();
                break;
            case 'telemetry':
                updateTelemetry(msg);
                break;
            case 'job_status':
                updateSingleJob(msg.job);
                break;
            case 'queue_updated':
            case 'jobs_added':
            case 'job_added':
            case 'job_removed':
                currentJobs = msg.jobs || [];
                renderJobs();
                break;
            case 'log':
                appendLog(msg.log);
                break;
            case 'autotune':
                renderAutoTune(msg.state);
                break;
        }
    };
    ws.onclose = () => setTimeout(connectWS, 2000);
}

function populateOptionSelects() {
    document.querySelectorAll('select[data-options]').forEach(sel => {
        const values = options[sel.dataset.options] || [];
        const previous = sel.value || sel.dataset.default;
        sel.innerHTML = values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
        if (values.includes(previous)) sel.value = previous;
    });
    toggleRateMode();
}

function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.toggle('active', el.id === tabId));
    document.querySelectorAll('.nav-btn').forEach(el => el.classList.toggle('active', el.dataset.tab === tabId));
    if (tabId === 'tab-encoder') loadFolders();
    if (tabId === 'tab-raw') loadRawFiles();
}

function toggleRateMode() {
    const isCq = $('cfg-rate-mode').value === (options.rate_control || [])[0];
    $('group-cq').style.display = isCq ? 'flex' : 'none';
    $('group-size').style.display = isCq ? 'none' : 'flex';
}

function updateTelemetry(data) {
    $('cpu-cores').innerText = data.cpu_count;
    $('cpu-val').innerText = `${data.cpu_percent}%`;
    $('cpu-fill').style.width = `${data.cpu_percent}%`;
    $('ram-val').innerText = `${data.ram_used_gb} / ${data.ram_total_gb} GB`;
    $('ram-fill').style.width = `${data.ram_percent}%`;
    $('disk-val').innerText = `${data.disk_free_gb} GB`;

    if (data.downloads) {
        renderDownloads(data.downloads);
        const completed = data.downloads.filter(d => d.status === 'completed').length;
        if (completed !== lastCompletedTransfers) {
            lastCompletedTransfers = completed;
            if ($('tab-raw').classList.contains('active')) loadRawFiles();
        }
    }

    if (data.queue) {
        const q = data.queue;
        queueState = {is_running: q.is_running, is_paused: q.is_paused};
        $('queue-sub').innerText = `${q.total} jobs | ${q.active} active | ${q.pending} pending | ${q.completed} completed`;
        const btn = $('btn-start');
        if (q.is_running) {
            btn.innerText = q.is_paused ? 'Resume Queue' : 'Pause Queue';
            btn.className = 'btn ' + (q.is_paused ? 'btn-success' : 'btn-warning');
        } else {
            btn.innerText = 'Start Queue';
            btn.className = 'btn btn-success';
        }
    }
}

function appendLog(text) {
    const box = $('log-box');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 5;
    box.textContent += text;
    if (box.textContent.length > 200000) box.textContent = box.textContent.slice(-150000);
    if (atBottom) box.scrollTop = box.scrollHeight;
}

// --- Folder scan -------------------------------------------------------------

async function loadFolders() {
    const data = await getJson('/api/raw/folders');
    if (!data.folders) return;
    const sel = $('sel-folder');
    const prev = sel.value;
    sel.innerHTML = data.folders.map(f =>
        `<option value="${escapeHtml(f.path)}">${escapeHtml(f.name)} (${f.file_count} files, ${f.size_gb} GB)</option>`
    ).join('');
    if (data.folders.some(f => f.path === prev)) sel.value = prev;
    currentFolder = sel.value;
    renderFolderSummary(data.folders);
    scanFolder(currentFolder);
}

async function promptNewFolder() {
    const name = prompt('New folder name:');
    if (!name || !name.trim()) return;
    try {
        const data = await api('/api/raw/folders/create', {folder_name: name.trim()});
        await loadFolders();
        $('sel-folder').value = data.path;
        currentFolder = data.path;
        scanFolder(currentFolder);
    } catch (e) {
        alert(e.message);
    }
}

async function scanFolder(folderPath) {
    let data;
    try {
        data = await api('/api/scan', {path: folderPath, recursive: false});
    } catch (e) {
        appendLog(`[Scan] ${e.message}\n`);
        return;
    }
    scannedFiles = data.files;
    currentSuggestedOut = data.suggested_output_dir;
    $('out-dest-label').innerText = currentSuggestedOut;
    $('scanned-container').style.display = 'block';
    $('scanned-title').innerText = `${data.count} files (${data.new_count} new, ${data.already_encoded_count} already encoded)`;

    $('scanned-list').innerHTML = data.count === 0
        ? '<p class="empty">No video files in this folder.</p>'
        : data.files.map((f, idx) => {
            const badge = f.already_encoded
                ? `<span class="badge badge-done">Encoded (${f.encoded_size_mb} MB, -${f.compression_pct}%)</span>`
                : '<span class="badge badge-pending">Not encoded</span>';
            return `
                <div class="spread" style="padding:5px 0; border-bottom:1px solid #222228;">
                    <label class="check-label">
                        <input type="checkbox" class="scanned-chk" data-idx="${idx}" ${f.already_encoded ? '' : 'checked'}>
                        <span><strong>[${escapeHtml(f.label)}]</strong> ${escapeHtml(f.filename)}</span>
                    </label>
                    <div class="row">
                        <span class="muted">${f.size_mb} MB</span>
                        ${badge}
                    </div>
                </div>`;
        }).join('');

    probedSubtitles = (data.sample_details && data.sample_details.subtitles) || [];
    renderSubtitleTracks();
}

function renderSubtitleTracks() {
    const list = $('subs-list');
    if (probedSubtitles.length === 0) {
        list.innerHTML = '<p class="muted">No embedded subtitle tracks found.</p>';
        return;
    }
    list.innerHTML = probedSubtitles.map(s => {
        const lang = (s.language || 'und').toUpperCase();
        const title = s.title ? ` - "${escapeHtml(s.title)}"` : '';
        return `
            <div class="spread" style="padding:4px 0;">
                <label class="check-label">
                    <input type="checkbox" class="sub-stream-chk" data-sub-idx="${s.index}" checked>
                    <span><strong>Track ${s.index}:</strong> [${escapeHtml(lang)}] ${escapeHtml(s.codec || 'sub')}${title}</span>
                </label>
                <input type="text" class="sub-custom-title" data-sub-idx="${s.index}" placeholder="${escapeHtml(s.title || lang)}" style="width:160px; padding:2px 6px; font-size:11px;">
            </div>`;
    }).join('');
}

function toggleSelectAllScanned() {
    const chks = document.querySelectorAll('.scanned-chk');
    const allChecked = Array.from(chks).every(c => c.checked);
    chks.forEach(c => c.checked = !allChecked);
}

function selectedScannedFiles() {
    return Array.from(document.querySelectorAll('.scanned-chk:checked'))
        .map(c => scannedFiles[parseInt(c.dataset.idx)])
        .filter(Boolean);
}

function collectEncodeSettings() {
    const customSubTitles = {};
    document.querySelectorAll('.sub-custom-title').forEach(inp => {
        if (inp.value.trim()) customSubTitles[parseInt(inp.dataset.subIdx)] = inp.value.trim();
    });
    return {
        video_cq: $('cfg-cq').value,
        svt_preset: $('cfg-preset').value,
        film_grain: $('cfg-grain').value,
        bit_depth: $('cfg-depth').value,
        resolution: $('cfg-res').value,
        audio_bitrate: $('cfg-audio-bitrate').value,
        audio_channels: $('cfg-channels').value,
        norm_mode: $('cfg-norm').value,
        audio_tracks: $('cfg-audio-tracks').value,
        audio_language: $('cfg-audio-language').value.trim() || 'und',
        smart_sub_matching: $('cfg-smart-subs').checked,
        selected_subs: Array.from(document.querySelectorAll('.sub-stream-chk:checked')).map(c => parseInt(c.dataset.subIdx)),
        custom_sub_titles: customSubTitles,
        rate_control_mode: $('cfg-rate-mode').value,
        target_size_mb: parseFloat($('cfg-target-size').value) || null,
        workers: parseInt($('cfg-workers').value) || 1
    };
}

async function addScannedToQueue() {
    const files = selectedScannedFiles();
    if (files.length === 0) return alert('No files selected.');
    try {
        const data = await api('/api/queue/add', {
            ...collectEncodeSettings(),
            paths: files.map(f => f.path),
            output_dir: currentSuggestedOut
        });
        currentJobs = data.jobs || [];
        renderJobs();
        appendLog(`[Queue] Added ${data.added_count} file(s).\n`);
    } catch (e) {
        alert(e.message);
    }
}

// --- Queue -------------------------------------------------------------------

function badgeClass(status) {
    switch ((status || '').toLowerCase()) {
        case 'encoding': return 'badge-active';
        case 'completed': return 'badge-done';
        case 'skipped': return 'badge-skipped';
        case 'failed':
        case 'canceled': return 'badge-error';
        default: return 'badge-pending';
    }
}

function renderJobs() {
    const container = $('jobs-container');
    if (!currentJobs || currentJobs.length === 0) {
        container.innerHTML = '<p class="empty">The queue is empty. Scan a folder above and add files.</p>';
        return;
    }
    container.innerHTML = currentJobs.map(j => {
        const status = (j.status || '').toLowerCase();
        const pct = (j.progress * 100).toFixed(1);
        const showBar = status === 'encoding' || j.progress > 0;
        const rate = j.rate_control_mode === (options.rate_control || [])[0] ? `CQ ${j.video_cq}` : `${j.target_size_mb} MB target`;
        return `
            <div class="job-card" id="card-${j.id}">
                <div class="job-row">
                    <span class="badge ${badgeClass(j.status)}">${escapeHtml(j.status)}</span>
                    <span class="job-title" title="${escapeHtml(j.source_path)}">${escapeHtml(j.title)}</span>
                    <div class="row">
                        ${status === 'failed' || status === 'canceled' ? `<button class="btn btn-warning btn-sm" data-action="retry" data-id="${j.id}">Retry</button>` : ''}
                        <button class="btn btn-outline btn-sm" data-action="up" data-id="${j.id}">Up</button>
                        <button class="btn btn-outline btn-sm" data-action="down" data-id="${j.id}">Down</button>
                        <button class="btn btn-danger btn-sm" data-action="remove" data-id="${j.id}">Remove</button>
                    </div>
                </div>
                <div class="job-meta">
                    ${escapeHtml(j.resolution)} | ${escapeHtml(rate)} | Preset ${escapeHtml(j.svt_preset)} | ${escapeHtml(j.bit_depth)}
                    | <span id="status-${j.id}">${escapeHtml(j.status_text || 'Ready')}</span>
                </div>
                <div class="pbar-line" style="display:${showBar ? 'block' : 'none'};">
                    <div class="pbar-fill" id="pbar-${j.id}" style="width:${pct}%;"></div>
                </div>
                ${j.error_message ? `<div class="error-box"><strong>Error details:</strong>\n${escapeHtml(j.error_message)}</div>` : ''}
            </div>`;
    }).join('');
}

function updateSingleJob(job) {
    if (!job) return;
    const idx = currentJobs.findIndex(j => j.id === job.id);
    const statusChanged = idx < 0 || currentJobs[idx].status !== job.status;
    if (idx >= 0) currentJobs[idx] = job;
    else currentJobs.push(job);

    if (statusChanged || !$(`card-${job.id}`)) {
        renderJobs();
        return;
    }
    $(`pbar-${job.id}`).style.width = `${(job.progress * 100).toFixed(1)}%`;
    $(`status-${job.id}`).innerText = job.status_text || job.status;
}

async function onJobAction(event) {
    const btn = event.target.closest('button[data-action]');
    if (!btn) return;
    const id = btn.dataset.id;
    const urls = {
        retry: `/api/queue/retry/${id}`,
        up: `/api/queue/move/${id}/up`,
        down: `/api/queue/move/${id}/down`,
        remove: `/api/queue/remove/${id}`
    };
    try {
        const data = await api(urls[btn.dataset.action]);
        if (data.jobs) { currentJobs = data.jobs; renderJobs(); }
        if (data.job) updateSingleJob(data.job);
    } catch (e) {
        alert(e.message);
    }
}

async function toggleQueue() {
    const url = queueState.is_running && !queueState.is_paused ? '/api/queue/pause' : '/api/queue/start';
    const data = await api(url);
    if (data.jobs) { currentJobs = data.jobs; renderJobs(); }
}

async function queueCommand(url) {
    const data = await api(url);
    if (data.jobs) { currentJobs = data.jobs; renderJobs(); }
}

// --- Auto-tune ---------------------------------------------------------------

async function startAutoTune() {
    const files = selectedScannedFiles();
    if (files.length === 0) return alert('Select a file to benchmark.');
    try {
        await api('/api/autotune', {
            path: files[0].path,
            total_files: files.length,
            resolution: $('cfg-res').value,
            bit_depth: $('cfg-depth').value
        });
    } catch (e) {
        alert(e.message);
    }
}

function renderAutoTune(state) {
    if (!state || (!state.running && !state.status)) return;
    $('autotune-card').style.display = 'block';
    $('autotune-file').innerText = state.file || '';
    $('autotune-status').innerText = state.error ? `${state.status}: ${state.error}` : (state.status || '');
    $('autotune-pbar').style.width = `${((state.progress || 0) * 100).toFixed(1)}%`;
    $('btn-autotune-cancel').style.display = state.running ? 'inline-flex' : 'none';
    $('btn-autotune').disabled = !!state.running;

    const results = state.results || [];
    if (results.length === 0) {
        $('autotune-results').innerHTML = '';
        return;
    }
    const bestName = state.best && state.best.name;
    $('autotune-results').innerHTML = `
        <table class="results">
            <tr><th>Profile</th><th>CQ</th><th>Grain</th><th>Sample size</th><th>Projected total</th><th>VMAF</th><th></th></tr>
            ${results.map((r, i) => `
                <tr class="${r.name === bestName ? 'best' : ''}">
                    <td>${escapeHtml(r.name)}${r.name === bestName ? ' (recommended)' : ''}</td>
                    <td>${escapeHtml(r.cq)}</td>
                    <td>${escapeHtml(r.grain)}</td>
                    <td>${r.clip_size_mb.toFixed(1)} MB</td>
                    <td>~${formatBytes(r.proj_gb * 1024 ** 3)}</td>
                    <td>${r.vmaf.toFixed(2)}</td>
                    <td><button class="btn btn-outline btn-sm" data-apply="${i}">Apply</button></td>
                </tr>`).join('')}
        </table>
        ${state.best ? `<p class="muted" style="margin-top:8px;">${escapeHtml(state.best.reason)}</p>` : ''}`;
    $('autotune-results').querySelectorAll('button[data-apply]').forEach(btn => {
        btn.onclick = () => applyProfile(results[parseInt(btn.dataset.apply)]);
    });
}

function applyProfile(profile) {
    $('cfg-rate-mode').value = (options.rate_control || [])[0];
    toggleRateMode();
    $('cfg-cq').value = profile.cq;
    $('cfg-grain').value = profile.grain;
    appendLog(`[Auto-Tune] Applied ${profile.name} (CQ ${profile.cq}, grain ${profile.grain}).\n`);
}

// --- Downloads ---------------------------------------------------------------

async function startDownloads() {
    const urls = $('dl-links').value.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    if (urls.length === 0) return alert('Paste at least one link.');
    try {
        const data = await api('/api/downloads', {
            urls,
            subfolder: $('dl-subfolder').value.trim(),
            add_to_queue: $('dl-add-queue').checked,
            settings: collectEncodeSettings()
        });
        $('dl-links').value = '';
        $('dl-message').innerText = `Started ${data.tasks.length} download(s).`;
    } catch (e) {
        $('dl-message').innerText = e.message;
    }
}

function renderDownloads(downloads) {
    const active = downloads.filter(d => !['completed', 'failed', 'cancelled'].includes(d.status)).length;
    $('nav-dl-count').innerText = active ? `(${active})` : '';
    $('dl-count').innerText = downloads.length;

    const list = $('downloads-list');
    if (downloads.length === 0) {
        list.innerHTML = '<p class="empty">No transfers yet.</p>';
        return;
    }
    list.innerHTML = downloads.map(d => {
        const pct = (d.progress * 100).toFixed(1);
        const done = d.status === 'completed';
        const failed = d.status === 'failed';
        const finished = done || failed || d.status === 'cancelled';
        const size = d.total_bytes > 0
            ? `${formatBytes(d.transferred_bytes)} / ${formatBytes(d.total_bytes)}`
            : formatBytes(d.transferred_bytes);
        const stats = finished ? size : `${size} | ${d.speed_mbs} MB/s | ETA ${formatEta(d.eta_sec)}`;
        const resumed = d.resumed_from > 0 ? ` | resumed at ${formatBytes(d.resumed_from)}` : '';
        return `
            <div class="list-item">
                <div class="spread" style="margin-bottom:6px;">
                    <div class="row" style="min-width:0; flex:1;">
                        <span class="badge ${done ? 'badge-done' : (finished ? 'badge-error' : 'badge-active')}">${escapeHtml(d.status)}</span>
                        <span class="job-title" title="${escapeHtml(d.source)}">${escapeHtml(d.filename)}</span>
                    </div>
                    <div class="row">
                        <span class="muted" style="white-space:nowrap;">${stats}${resumed}</span>
                        ${failed ? `<button class="btn btn-warning btn-sm" data-dl-action="retry" data-id="${d.id}">Resume</button>` : ''}
                        ${!finished ? `<button class="btn btn-danger btn-sm" data-dl-action="cancel" data-id="${d.id}">Cancel</button>` : ''}
                    </div>
                </div>
                <div class="pbar-line"><div class="pbar-fill" style="width:${pct}%; background:${done ? '#28a745' : '#007acc'};"></div></div>
                ${d.error ? `<div class="error-box">${escapeHtml(d.error)}</div>` : ''}
            </div>`;
    }).join('');
}

async function onDownloadAction(event) {
    const btn = event.target.closest('button[data-dl-action]');
    if (!btn) return;
    await api(`/api/downloads/${btn.dataset.id}/${btn.dataset.dlAction}`);
}

// --- Library -----------------------------------------------------------------

function renderFolderSummary(folders) {
    $('raw-folders-summary').innerHTML = folders.map(f => `
        <div class="list-item" style="margin:0;">
            <strong>${escapeHtml(f.name)}</strong>: <span class="muted">${f.file_count} files (${f.size_gb} GB)</span>
        </div>`).join('');
}

async function loadRawFiles() {
    const fData = await getJson('/api/raw/folders');
    if (fData.folders) renderFolderSummary(fData.folders);

    const data = await getJson('/api/raw/list');
    currentRawFiles = data.files || [];
    $('raw-file-count').innerText = currentRawFiles.length;
    $('raw-total-size').innerText = data.total_size_gb;
    const container = $('raw-files-list');
    if (currentRawFiles.length === 0) {
        container.innerHTML = '<p class="empty">No files in the input root.</p>';
        return;
    }
    container.innerHTML = currentRawFiles.map((f, idx) => `
        <div class="list-item spread">
            <label class="check-label">
                <input type="checkbox" class="raw-file-chk" data-idx="${idx}">
                <span>${escapeHtml(f.filename)}</span>
            </label>
            <div class="row">
                <span class="muted">${f.size_mb} MB</span>
                ${f.is_encoded
                    ? `<span class="badge badge-done">Encoded (${f.encoded_size_mb} MB)</span>`
                    : '<span class="badge badge-pending">Not encoded</span>'}
            </div>
        </div>`).join('');
}

function selectedRawPaths() {
    return Array.from(document.querySelectorAll('.raw-file-chk:checked'))
        .map(c => currentRawFiles[parseInt(c.dataset.idx)]?.path)
        .filter(Boolean);
}

async function moveSelectedRaw() {
    const paths = selectedRawPaths();
    if (paths.length === 0) return alert('Select files to move first.');
    const target = prompt('Target folder name (leave blank for the input root):');
    if (target === null) return;
    await api('/api/raw/move', {paths, target_folder: target.trim()});
    loadRawFiles();
}

async function deleteSelectedRaw() {
    const paths = selectedRawPaths();
    if (paths.length === 0) return alert('No files selected.');
    if (!confirm(`Permanently delete ${paths.length} file(s)?`)) return;
    await api('/api/raw/delete', {paths});
    loadRawFiles();
}

async function cleanEncodedRawFiles() {
    if (!confirm('Delete every source file in the input root that already has an encoded output?')) return;
    const data = await api('/api/raw/clean-encoded');
    alert(`Deleted ${data.deleted_count} file(s), freeing ${data.freed_mb} MB.`);
    loadRawFiles();
}

// --- Wiring ------------------------------------------------------------------

window.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav-btn').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

    $('btn-start').onclick = toggleQueue;
    $('btn-clear-done').onclick = () => queueCommand('/api/queue/clear_completed');
    $('btn-cancel-all').onclick = () => queueCommand('/api/queue/cancel_all');
    $('jobs-container').addEventListener('click', onJobAction);

    $('sel-folder').onchange = () => { currentFolder = $('sel-folder').value; scanFolder(currentFolder); };
    $('btn-rescan').onclick = () => scanFolder(currentFolder);
    $('btn-new-folder').onclick = promptNewFolder;
    $('btn-new-folder-2').onclick = promptNewFolder;
    $('btn-toggle-all').onclick = toggleSelectAllScanned;
    $('cfg-rate-mode').onchange = toggleRateMode;
    $('btn-add-queue').onclick = addScannedToQueue;
    $('btn-autotune').onclick = startAutoTune;
    $('btn-autotune-cancel').onclick = () => api('/api/autotune/cancel');
    $('btn-clear-log').onclick = () => { $('log-box').textContent = ''; };

    $('btn-start-downloads').onclick = startDownloads;
    $('btn-clear-downloads').onclick = () => api('/api/downloads/clear');
    $('downloads-list').addEventListener('click', onDownloadAction);

    $('btn-refresh-raw').onclick = loadRawFiles;
    $('btn-move-selected').onclick = moveSelectedRaw;
    $('btn-delete-selected').onclick = deleteSelectedRaw;
    $('btn-clean-encoded').onclick = cleanEncodedRawFiles;

    connectWS();
});
