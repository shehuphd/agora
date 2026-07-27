// experiments.js — Experiment management screen (#/experiments)
import { esc, formatTokens } from './render.js';

let _selectedEid = null;

// At most one batch poll runs at a time; cleared on navigation so a poll
// can't keep firing after the user leaves the experiments screen.
let _batchPollTimer = null;

function _clearBatchPoll() {
  if (_batchPollTimer) {
    clearInterval(_batchPollTimer);
    _batchPollTimer = null;
  }
}

window.addEventListener('hashchange', _clearBatchPoll);

// ============================================================
// Public entry point (called by router)
// ============================================================

export async function loadExperiments() {
  const screen = document.getElementById('screen-experiments');
  screen.innerHTML = `
    <div class="exp-layout">
      <div class="exp-sidebar" id="exp-sidebar">
        <div class="exp-sidebar-head">
          <h2 class="exp-sidebar-title">experiments</h2>
          <div class="exp-sidebar-actions">
            <button class="btn-ghost btn-sm" id="btn-sidebar-import"
                    title="import a CSV of debates as an experiment">
              <i class="ti ti-upload" aria-hidden="true"></i> import
            </button>
            <button class="btn-primary btn-sm" id="btn-new-exp">
              <i class="ti ti-plus" aria-hidden="true"></i> new
            </button>
          </div>
        </div>
        <div id="exp-create-form" class="exp-create-form" style="display:none">
          <input type="text" id="exp-name-inp" class="exp-name-inp" placeholder="experiment name" maxlength="120">
          <textarea id="exp-desc-inp" class="exp-desc-inp" rows="2" placeholder="description (optional)" maxlength="500"></textarea>
          <div class="exp-form-btns">
            <button class="btn-primary btn-sm" id="btn-exp-submit">create</button>
            <button class="btn-ghost btn-sm" id="btn-exp-cancel">cancel</button>
          </div>
        </div>
        <div id="exp-list" class="exp-list">
          <p class="exp-loading">loading…</p>
        </div>
      </div>
      <div class="exp-detail" id="exp-detail">
        <div class="exp-detail-empty">
          <i class="ti ti-flask-2" aria-hidden="true"></i>
          <p>select an experiment to see its runs</p>
        </div>
      </div>
    </div>
  `;

  _wireCreateForm();
  _wireSidebarImport();
  await _refreshList();
}

// ============================================================
// List
// ============================================================

async function _refreshList() {
  const listEl = document.getElementById('exp-list');
  if (!listEl) return;
  try {
    const res = await fetch('/experiments');
    const data = await res.json();
    _renderList(listEl, data);
  } catch (e) {
    listEl.innerHTML = `<p class="exp-error">failed to load experiments</p>`;
  }
}

function _renderList(listEl, experiments) {
  if (!experiments.length) {
    listEl.innerHTML = `<p class="exp-empty-hint">no experiments yet — create one to group related runs</p>`;
    return;
  }
  listEl.innerHTML = '';
  experiments.forEach(exp => {
    const row = document.createElement('div');
    row.className = 'exp-row' + (exp.experiment_id === _selectedEid ? ' exp-row-active' : '');
    row.dataset.eid = exp.experiment_id;
    row.innerHTML = `
      <div class="exp-row-name">${esc(exp.name)}</div>
      <div class="exp-row-meta">
        <span class="exp-run-count">${exp.run_count} run${exp.run_count !== 1 ? 's' : ''}</span>
        <span class="exp-created">${_fmtDate(exp.created_at)}</span>
      </div>
    `;
    row.addEventListener('click', () => _selectExperiment(exp));
    listEl.appendChild(row);
  });
}

// ============================================================
// Detail
// ============================================================

async function _selectExperiment(exp) {
  _selectedEid = exp.experiment_id;
  // Highlight active row
  document.querySelectorAll('.exp-row').forEach(r => {
    r.classList.toggle('exp-row-active', r.dataset.eid === exp.experiment_id);
  });
  await _renderDetail(exp);
}

async function _renderDetail(exp) {
  const detailEl = document.getElementById('exp-detail');
  if (!detailEl) return;

  detailEl.innerHTML = `<p class="exp-loading">loading runs…</p>`;

  let runs = [];
  try {
    const res = await fetch(`/experiments/${exp.experiment_id}/runs`);
    runs = await res.json();
  } catch (e) {
    detailEl.innerHTML = `<p class="exp-error">failed to load runs</p>`;
    return;
  }

  let unassigned = [];
  try {
    const res = await fetch('/experiments/unassigned-runs');
    unassigned = await res.json();
  } catch (_) {}

  detailEl.innerHTML = `
    <div class="exp-detail-head">
      <div>
        <h2 class="exp-detail-name">${esc(exp.name)}</h2>
        ${exp.description ? `<p class="exp-detail-desc">${esc(exp.description)}</p>` : ''}
        <p class="exp-detail-meta">created ${_fmtDate(exp.created_at)} · ${runs.length} run${runs.length !== 1 ? 's' : ''}</p>
      </div>
      <button class="btn-ghost btn-sm exp-delete-btn" data-eid="${esc(exp.experiment_id)}" data-name="${esc(exp.name)}">
        <i class="ti ti-trash" aria-hidden="true"></i> delete
      </button>
    </div>

    ${unassigned.length
      ? `<div class="exp-assign-row">
           <select id="exp-assign-select" class="exp-assign-select">
             <option value="">— assign a run to this experiment —</option>
             ${unassigned.map(r => `<option value="${esc(r.run_id)}">${esc(r.debate_title || r.topic || r.run_id)} · ${_fmtDate(r.created_at)}</option>`).join('')}
           </select>
           <button class="btn-solid btn-sm" id="btn-exp-assign">assign</button>
         </div>`
      : `<div class="exp-no-runs-cta">no unassigned runs — <a href="#/new" class="exp-new-run-link">start a new debate</a></div>`
    }

    <div id="exp-runs-list" class="exp-runs-list">
      ${runs.length ? _runsHtml(runs) : '<p class="exp-empty-hint">no runs in this experiment yet</p>'}
    </div>
  `;

  // Wire delete experiment
  detailEl.querySelector('.exp-delete-btn').addEventListener('click', async (e) => {
    const { eid, name } = e.currentTarget.dataset;
    if (!confirm(`Delete experiment "${name}"? Runs will be unassigned but not deleted.`)) return;
    await fetch(`/experiments/${eid}`, { method: 'DELETE' });
    _selectedEid = null;
    document.getElementById('exp-detail').innerHTML = `
      <div class="exp-detail-empty">
        <i class="ti ti-flask-2" aria-hidden="true"></i>
        <p>select an experiment to see its runs</p>
      </div>`;
    await _refreshList();
  });

  // Wire assign (only rendered when unassigned runs exist)
  const assignBtn = detailEl.querySelector('#btn-exp-assign');
  if (assignBtn) assignBtn.addEventListener('click', async () => {
    const sel = detailEl.querySelector('#exp-assign-select');
    const runId = sel.value;
    if (!runId) return;
    await fetch(`/experiments/${exp.experiment_id}/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId }),
    });
    await _renderDetail(exp);
    await _refreshList();
  });

  // Wire unassign buttons
  detailEl.querySelectorAll('.exp-unassign-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const runId = e.currentTarget.dataset.runId;
      await fetch(`/experiments/${exp.experiment_id}/runs/${runId}`, { method: 'DELETE' });
      await _renderDetail(exp);
      await _refreshList();
    });
  });

  // Navigate to run on row click
  detailEl.querySelectorAll('.exp-run-row[data-run-id]').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      window.location.hash = `#/debate/${row.dataset.runId}`;
    });
  });
}

function _runsHtml(runs) {
  return runs.map(r => {
    const title  = esc(r.debate_title || r.topic || r.run_id);
    const status = r.status || 'unknown';
    const cls    = status === 'running' ? 'pill-live' : status === 'paused' ? 'pill-paused' : 'pill-done';
    const orphan = r.orphaned ? `<span class="exp-orphan-badge" title="run folder not found on disk">!</span>` : '';
    return `
      <div class="exp-run-row" data-run-id="${esc(r.run_id)}">
        <div class="exp-run-info">
          ${orphan}
          <span class="exp-run-title">${title}</span>
          <span class="exp-run-sub">${esc(r.proposition_nickname)} vs ${esc(r.opposition_nickname)} · ${r.turn} turns · ${formatTokens(r.total_tokens)}</span>
        </div>
        <div class="exp-run-right">
          <span class="pill ${cls}">${esc(status)}</span>
          <span class="exp-run-date">${_fmtDate(r.created_at)}</span>
          <button class="btn-ghost btn-sm exp-unassign-btn" data-run-id="${esc(r.run_id)}" title="remove from experiment">
            <i class="ti ti-x" aria-hidden="true"></i>
          </button>
        </div>
      </div>`;
  }).join('');
}

// ============================================================
// CSV import
// ============================================================

function _wireImportCsv(exp, detailEl) {
  const panel      = detailEl.querySelector('#exp-import-panel');
  const fileInput  = detailEl.querySelector('#exp-import-file');
  const fileLabel  = detailEl.querySelector('#exp-import-filename');
  const preview    = detailEl.querySelector('#exp-import-preview');
  const actions    = detailEl.querySelector('#exp-import-actions');
  const runBtn     = detailEl.querySelector('#btn-run-batch');
  const cancelBtn  = detailEl.querySelector('#btn-import-cancel');
  const statusDiv  = detailEl.querySelector('#exp-batch-status');

  const dropZone   = detailEl.querySelector('#exp-drop-zone');
  const PLACEHOLDER = 'drop a CSV here, or click to choose…';

  let _pendingFile = null;

  // No JS click handler: the label wraps the input, so a click already opens
  // the picker natively — adding fileInput.click() opened it twice.

  // Drag-and-drop
  ['dragenter', 'dragover'].forEach(ev => {
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.add('exp-drop-active');
    });
  });
  ['dragleave', 'drop'].forEach(ev => {
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.remove('exp-drop-active');
    });
  });
  dropZone.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) _acceptFile(file);
  });

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file) _acceptFile(file);
  });

  function _acceptFile(file) {
    if (!/\.csv$/i.test(file.name)) {
      preview.style.display = 'block';
      preview.innerHTML = `<p class="exp-import-error">That's not a CSV file.</p>`;
      actions.style.display = 'none';
      return;
    }
    _pendingFile = file;
    fileLabel.textContent = file.name;

    const reader = new FileReader();
    reader.onload = (e) => _renderPreview(e.target.result);
    reader.readAsText(file);
  }

  function _renderPreview(text) {
    const topics = _previewTopics(text);
    if (!topics) {
      preview.style.display = 'block';
      preview.innerHTML = `<p class="exp-import-error">No 'topic' column found in this CSV.</p>`;
      actions.style.display = 'none';
      return;
    }

    preview.style.display = 'block';
    preview.innerHTML = `
      <div class="exp-import-preview-head">
        <label class="exp-check-label">
          <input type="checkbox" id="exp-select-all" checked>
          <span>${topics.length} debate${topics.length !== 1 ? 's' : ''} found</span>
        </label>
        <span class="exp-selected-count" id="exp-selected-count">${topics.length} selected</span>
      </div>
      <div class="exp-import-list-wrap">
        <ul class="exp-import-list">
          ${topics.map((t, i) => `
            <li>
              <label class="exp-check-label">
                <input type="checkbox" class="exp-row-check" data-idx="${i}" checked>
                <span>${esc(t)}</span>
              </label>
            </li>`).join('')}
        </ul>
      </div>
    `;

    const selectAll = preview.querySelector('#exp-select-all');
    const checks    = Array.from(preview.querySelectorAll('.exp-row-check'));
    const countEl   = preview.querySelector('#exp-selected-count');

    function _syncCount() {
      const n = checks.filter(c => c.checked).length;
      countEl.textContent = `${n} selected`;
      runBtn.disabled = n === 0;
      selectAll.checked = n === checks.length;
      selectAll.indeterminate = n > 0 && n < checks.length;
    }

    selectAll.addEventListener('change', () => {
      checks.forEach(c => { c.checked = selectAll.checked; });
      _syncCount();
    });
    checks.forEach(c => c.addEventListener('change', _syncCount));

    actions.style.display = 'flex';
    statusDiv.style.display = 'none';
    _syncCount();
  }

  cancelBtn.addEventListener('click', () => {
    panel.style.display = 'none';
    _pendingFile = null;
    fileInput.value = '';
    fileLabel.textContent = PLACEHOLDER;
    preview.style.display = 'none';
    actions.style.display = 'none';
    statusDiv.style.display = 'none';
  });

  runBtn.addEventListener('click', async () => {
    if (!_pendingFile) return;
    const selected = Array.from(preview.querySelectorAll('.exp-row-check'))
      .filter(c => c.checked)
      .map(c => c.dataset.idx);
    if (!selected.length) return;

    // Standalone import: the typed name finds or creates the experiment.
    const nameInp = detailEl.querySelector('#exp-import-name');
    const expName = nameInp ? nameInp.value.trim() : '';
    if (!exp && !expName) {
      nameInp?.focus();
      statusDiv.style.display = 'block';
      statusDiv.innerHTML = `<p class="exp-import-error">Name the experiment first.</p>`;
      return;
    }

    runBtn.disabled = true;
    runBtn.textContent = 'queuing…';

    const fd = new FormData();
    fd.append('file', _pendingFile);
    fd.append('experiment_id', exp ? exp.experiment_id : '');
    fd.append('experiment_name', expName);
    fd.append('selected_rows', selected.join(','));

    let data;
    try {
      const res = await fetch('/api/batch', { method: 'POST', body: fd });
      data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Upload failed');
    } catch (err) {
      runBtn.disabled = false;
      runBtn.textContent = 'run selected';
      statusDiv.style.display = 'block';
      statusDiv.innerHTML = `<p class="exp-import-error">${esc(String(err))}</p>`;
      return;
    }

    runBtn.disabled = false;
    runBtn.textContent = 'run selected';
    actions.style.display = 'none';
    preview.style.display = 'none';
    statusDiv.style.display = 'block';
    _pollBatch(data.job_id, exp || { experiment_id: data.experiment_id, name: expName },
               statusDiv, detailEl);
    _refreshList();   // the experiment may have just been created
  });
}

/**
 * RFC-4180-style CSV parse (quoted fields, escaped quotes, CRLF).
 * Must mirror Python's csv module on the backend: a naive split(',') breaks on
 * quoted topics containing commas AND desynchronises the checkbox indices from
 * the backend's selected_rows interpretation, silently running the wrong rows.
 */
function _parseCsv(text) {
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(field); field = '';
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      rows.push(row); row = [];
    } else {
      field += ch;
    }
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function _previewTopics(csvText) {
  const rows = _parseCsv(csvText).filter(r => r.some(c => c.trim()));
  if (rows.length < 2) return null;
  const headers = rows[0].map(h => h.trim().toLowerCase());
  const topicIdx = headers.indexOf('topic');
  if (topicIdx === -1) return null;
  // Skip blank-topic rows exactly like the backend does, keeping indices aligned.
  return rows.slice(1)
    .map(r => (r[topicIdx] || '').trim())
    .filter(Boolean);
}

function _pollBatch(jobId, exp, statusDiv, detailEl) {
  _clearBatchPoll();

  function _renderStatus(job) {
    const pct = job.total > 0 ? Math.round(((job.done + job.failed) / job.total) * 100) : 0;
    const done = job.status === 'done' || job.status === 'failed';

    statusDiv.innerHTML = `
      <div class="exp-batch-header">
        <span class="exp-batch-label">batch run · ${job.done}/${job.total} complete${job.failed ? ` · ${job.failed} failed` : ''}</span>
        <span class="pill ${done ? 'pill-done' : 'pill-live'}">${done ? (job.failed === job.total ? 'failed' : 'done') : 'running'}</span>
      </div>
      <div class="exp-batch-bar-track"><div class="exp-batch-bar-fill" style="width:${pct}%"></div></div>
      <div class="exp-batch-rows-wrap">
        <ol class="exp-batch-rows">
          ${job.rows.map(r => {
            const cls = r.status === 'done' ? 'batch-row-done'
                      : r.status === 'failed' ? 'batch-row-failed'
                      : r.status === 'running' ? 'batch-row-running'
                      : 'batch-row-pending';
            const icon = r.status === 'done'    ? '<i class="ti ti-check"></i>'
                       : r.status === 'failed'  ? '<i class="ti ti-x"></i>'
                       : r.status === 'running' ? '<i class="ti ti-loader-2 spin"></i>'
                       : '<i class="ti ti-clock"></i>';
            const sub = r.status === 'failed' && r.error
              ? `<span class="batch-row-error">${esc(r.error)}</span>` : '';
            // Once the run exists in the backend, link straight to it.
            const label = r.run_id
              ? `<a class="batch-row-link" href="#/debate/${esc(r.run_id)}">${esc(r.topic)}</a>`
              : `<span>${esc(r.topic)}</span>`;
            return `<li class="exp-batch-row ${cls}">${icon} ${label}${sub}</li>`;
          }).join('')}
        </ol>
      </div>
    `;

    if (done) {
      _clearBatchPoll();
      _refreshList();
      // Switch to the experiment's own view now the runs exist.
      if (exp && exp.experiment_id) {
        fetch(`/experiments/${exp.experiment_id}`)
          .then(r => r.ok ? r.json() : null)
          .then(full => { if (full) _selectExperiment(full); })
          .catch(() => {});
      }
    }
  }

  async function _tick() {
    try {
      const res = await fetch(`/api/batch/${jobId}`);
      const job = await res.json();
      _renderStatus(job);
    } catch (_) {}
  }

  _tick();
  _batchPollTimer = setInterval(_tick, 3000);
}

// ============================================================
// Sidebar import button
// ============================================================

function _wireSidebarImport() {
  const btn = document.getElementById('btn-sidebar-import');
  if (!btn) return;
  btn.addEventListener('click', () => _renderImportView());
}

/**
 * Standalone import screen: pick a CSV and name the experiment in one go.
 * Deliberately does not require an experiment to exist first — the name field
 * finds an existing experiment or creates one server-side.
 */
async function _renderImportView(presetName = '') {
  const detailEl = document.getElementById('exp-detail');
  if (!detailEl) return;

  let existing = [];
  try {
    existing = await (await fetch('/experiments')).json();
  } catch (_) {}

  detailEl.innerHTML = `
    <div class="exp-detail-head">
      <div>
        <h2 class="exp-detail-name">import debates</h2>
        <p class="exp-detail-meta">upload a CSV and name the experiment to run it under</p>
      </div>
    </div>

    <div id="exp-import-panel" class="exp-import-panel">
      <div class="exp-import-name-row">
        <input type="text" id="exp-import-name" class="exp-name-inp"
               list="exp-name-options" maxlength="120"
               placeholder="experiment name (new or existing)"
               value="${esc(presetName)}">
        <datalist id="exp-name-options">
          ${existing.map(e => `<option value="${esc(e.name)}"></option>`).join('')}
        </datalist>
      </div>

      <div class="exp-import-inner">
        <label class="exp-import-label" id="exp-drop-zone">
          <i class="ti ti-file-text" aria-hidden="true"></i>
          <span id="exp-import-filename">drop a CSV here, or click to choose…</span>
          <input type="file" id="exp-import-file" accept=".csv,text/csv" style="display:none">
        </label>
        <a href="/api/batch/template" class="btn-ghost btn-sm" download>
          <i class="ti ti-download" aria-hidden="true"></i> template
        </a>
      </div>

      <div id="exp-import-preview" class="exp-import-preview" style="display:none"></div>
      <div id="exp-import-actions" class="exp-import-actions" style="display:none">
        <button class="btn-primary btn-sm" id="btn-run-batch">run selected</button>
        <button class="btn-ghost btn-sm" id="btn-import-cancel">cancel</button>
      </div>
      <div id="exp-batch-status" class="exp-batch-status" style="display:none"></div>
    </div>
  `;

  _wireImportCsv(null, detailEl);
}

// ============================================================
// Create form
// ============================================================

function _wireCreateForm() {
  const btn    = document.getElementById('btn-new-exp');
  const form   = document.getElementById('exp-create-form');
  const inp    = document.getElementById('exp-name-inp');
  const submit = document.getElementById('btn-exp-submit');
  const cancel = document.getElementById('btn-exp-cancel');

  btn.addEventListener('click', () => {
    form.style.display = form.style.display === 'none' ? 'flex' : 'none';
    if (form.style.display === 'flex') inp.focus();
  });

  cancel.addEventListener('click', () => {
    form.style.display = 'none';
    inp.value = '';
    document.getElementById('exp-desc-inp').value = '';
  });

  submit.addEventListener('click', () => _doCreate());
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') _doCreate(); });
}

async function _doCreate() {
  const name = (document.getElementById('exp-name-inp').value || '').trim();
  if (!name) { document.getElementById('exp-name-inp').focus(); return; }
  const desc = (document.getElementById('exp-desc-inp').value || '').trim();

  const res = await fetch('/experiments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description: desc || null }),
  });
  if (!res.ok) return;
  const exp = await res.json();

  document.getElementById('exp-create-form').style.display = 'none';
  document.getElementById('exp-name-inp').value = '';
  document.getElementById('exp-desc-inp').value = '';

  await _refreshList();
  _selectExperiment(exp);
}

// ============================================================
// Util
// ============================================================

function _fmtDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso + 'Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch (_) { return iso.slice(0, 10); }
}
