// experiments.js — Experiment management screen (#/experiments)
import { esc, formatTokens } from './render.js';

let _selectedEid = null;

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
          <button class="btn-primary btn-sm" id="btn-new-exp">
            <i class="ti ti-plus" aria-hidden="true"></i> new
          </button>
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
