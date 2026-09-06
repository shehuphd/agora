// experiments.js — Experiment management screen (#/experiments)
import { esc, formatTokens, formatCost } from './render.js';

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

export async function loadExperiments(selectedEid = null) {
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
  _selectedEid = selectedEid;
  await _refreshList();
  if (selectedEid) await _selectExperiment({ experiment_id: selectedEid });
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
    row.addEventListener('click', () => _selectExperiment(exp, { push: true }));
    listEl.appendChild(row);
  });
}

// ============================================================
// Detail
// ============================================================

async function _selectExperiment(exp, opts = {}) {
  _selectedEid = exp.experiment_id;
  // The detail view has its own URL. A click pushes a history entry so Back
  // returns to the list; programmatic re-renders (post-create, post-save,
  // batch completion) just keep the URL current without adding entries.
  // Neither form fires hashchange, so no double render.
  const target = `#/experiments/${exp.experiment_id}`;
  if (window.location.hash !== target) {
    if (opts.push) history.pushState(null, '', target);
    else history.replaceState(null, '', target);
  }
  // Highlight active row
  document.querySelectorAll('.exp-row').forEach(r => {
    r.classList.toggle('exp-row-active', r.dataset.eid === exp.experiment_id);
  });
  // The sidebar list carries no spec/manifest; the detail view needs both.
  try {
    const res = await fetch(`/experiments/${exp.experiment_id}`);
    if (res.ok) exp = await res.json();
  } catch (_) {}
  if (!exp.name) {
    // A deep link to a deleted or mistyped experiment id: say so instead of
    // rendering a broken header.
    const detailEl = document.getElementById('exp-detail');
    if (detailEl) detailEl.innerHTML = `
      <div class="exp-detail-empty">
        <i class="ti ti-flask-2" aria-hidden="true"></i>
        <p>This experiment doesn't exist any more. Pick one from the list on the left.</p>
      </div>`;
    return;
  }
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
        <p class="exp-detail-meta">created ${_fmtDate(exp.created_at)} · ${runs.length} run${runs.length !== 1 ? 's' : ''}${_spendSummary(runs)}</p>
      </div>
      <button class="btn-ghost btn-sm exp-delete-btn" data-eid="${esc(exp.experiment_id)}" data-name="${esc(exp.name)}">
        <i class="ti ti-trash" aria-hidden="true"></i> delete
      </button>
    </div>

    <div id="exp-design-panel" class="exp-design-panel">${_designPanelHtml(exp)}</div>
    <div id="exp-spec-builder" style="display:none"></div>
    <div id="exp-launch-status" class="exp-batch-status" style="display:none"></div>

    <div id="exp-comparison" class="exp-comparison"></div>

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

  _wireDesignPanel(exp, detailEl);
  _renderComparison(exp, runs, detailEl);

  // Wire delete experiment — two-click confirm (same pattern as History's
  // batch delete), never a browser dialog. Runs are unassigned, not deleted.
  let _delConfirmTimer = null;
  detailEl.querySelector('.exp-delete-btn').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const { eid } = btn.dataset;
    if (btn.dataset.confirming !== 'true') {
      btn.dataset.confirming = 'true';
      btn.innerHTML = '<i class="ti ti-alert-triangle" aria-hidden="true"></i> confirm delete? (runs are kept)';
      _delConfirmTimer = setTimeout(() => {
        btn.dataset.confirming = '';
        btn.innerHTML = '<i class="ti ti-trash" aria-hidden="true"></i> delete';
      }, 5000);
      return;
    }
    clearTimeout(_delConfirmTimer);
    await fetch(`/experiments/${eid}`, { method: 'DELETE' });
    _selectedEid = null;
    // The deleted experiment's URL no longer resolves to anything.
    history.replaceState(null, '', '#/experiments');
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

// Aggregate spend across an experiment's runs, from each run's recorded
// total_cost_usd. No priced run → no summary at all; any run partial or
// unpriced-with-tokens → the total is a minimum, same semantics as History.
function _spendSummary(runs) {
  let total = null, partial = false, priced = 0;
  for (const r of runs) {
    if (r.total_cost_usd != null) {
      total = (total ?? 0) + r.total_cost_usd;
      priced += 1;
      if (r.cost_partial) partial = true;
    } else if ((r.total_tokens || 0) > 0) {
      partial = true;
    }
  }
  if (total == null) return '';
  const avg = total / priced;
  return ` · ${formatCost(total, partial)} total · ${formatCost(avg)} avg/run`;
}

function _runsHtml(runs) {
  return runs.map(r => {
    const title  = esc(r.debate_title || r.topic || r.run_id);
    const status = r.status || 'unknown';
    const cls    = status === 'running' ? 'pill-live' : status === 'paused' ? 'pill-paused' : 'pill-done';
    const orphan = r.orphaned ? `<span class="exp-orphan-badge" title="run folder not found on disk">!</span>` : '';
    const cond = r.condition ? ` · ${_conditionText(r.condition)}` : '';
    const m = r.metrics;
    const metricBits = [];
    if (m && m.citation_coverage != null) metricBits.push(`${Math.round(m.citation_coverage * 100)}% cited`);
    if (m && m.retries != null && m.retries > 0) metricBits.push(`${m.retries} retr${m.retries === 1 ? 'y' : 'ies'}`);
    const metricStr = metricBits.length ? ` · ${metricBits.join(' · ')}` : '';
    return `
      <div class="exp-run-row" data-run-id="${esc(r.run_id)}">
        <div class="exp-run-info">
          ${orphan}
          <span class="exp-run-title">${title}</span>
          <span class="exp-run-sub">${esc(r.proposition_nickname)} vs ${esc(r.opposition_nickname)} · ${r.turn} turns · ${formatTokens(r.total_tokens)} tokens · ${formatCost(r.total_cost_usd, r.cost_partial)}${cond}${metricStr}</span>
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
// Experiment design (spec) panel
// ============================================================

// Mirrors SPEC_FIELDS in core/spec.py (also the CSV template columns).
const _SPEC_FIELDS = [
  'topic', 'debate_title',
  'proposition_model', 'opposition_model', 'moderator_model', 'synth_model',
  'proposition_nickname', 'opposition_nickname',
  'max_turns', 'max_time_minutes', 'token_budget',
  'temperature_proposition', 'temperature_opposition', 'temperature_moderator',
  'aggression', 'min_challenges', 'min_concessions',
  'require_steelman', 'require_full_resolution',
];
const _INT_FIELDS   = new Set(['max_turns', 'max_time_minutes', 'token_budget', 'min_challenges', 'min_concessions']);
const _FLOAT_FIELDS = new Set(['temperature_proposition', 'temperature_opposition', 'temperature_moderator', 'aggression']);
const _BOOL_FIELDS  = new Set(['require_steelman', 'require_full_resolution']);

function _coerceLevel(field, raw) {
  const v = String(raw).trim();
  if (_INT_FIELDS.has(field))   return parseInt(v, 10);
  if (_FLOAT_FIELDS.has(field)) return parseFloat(v);
  if (_BOOL_FIELDS.has(field))  return ['1', 'true', 'yes'].includes(v.toLowerCase());
  return v;
}

function _designPanelHtml(exp) {
  if (!exp.spec) {
    return `
      <div class="exp-design-empty">
        <span>no design yet — a design turns this experiment into a repeatable run matrix (conditions × replicates)</span>
        <button class="btn-solid btn-sm" id="btn-exp-design">
          <i class="ti ti-layout-grid" aria-hidden="true"></i> design
        </button>
      </div>`;
  }
  const s = exp.spec;
  let summary;
  if (s.rows) {
    summary = `${s.rows.length} explicit row${s.rows.length !== 1 ? 's' : ''} (from a CSV import) × ${s.replicates || 1} replicate${(s.replicates || 1) !== 1 ? 's' : ''}`;
  } else {
    const factors = (s.factors || []).map(f => `${esc(f.field)} ∈ {${f.levels.map(esc).join(', ')}}`).join(' × ');
    const cells = (s.factors || []).reduce((n, f) => n * f.levels.length, 1);
    summary = `${factors || 'one condition'} × ${s.replicates || 1} replicate${(s.replicates || 1) !== 1 ? 's' : ''} = ${cells * (s.replicates || 1)} runs per launch`;
  }
  const manifest = exp.manifest
    ? `<p class="exp-manifest">first launched with keycall ${esc(exp.manifest.keycall || '?')}, traceact ${esc(exp.manifest.traceact || '?')}, rates ${esc(exp.manifest.rates || '?')}${exp.manifest.prices_as_of ? `, prices as of ${esc(exp.manifest.prices_as_of)}` : ''}${exp.manifest.git_commit ? `, commit ${esc(exp.manifest.git_commit)}` : ''}</p>`
    : '';
  return `
    <div class="exp-design-summary">
      <div class="exp-design-info">
        <span class="exp-design-label">design:</span> ${summary}
        ${manifest}
      </div>
      <div class="exp-design-actions">
        <label class="exp-budget-label">ceiling $
          <input type="number" id="exp-launch-budget" class="exp-budget-inp" min="0.01" step="0.5"
                 placeholder="none" title="optional spend ceiling for the launch — new rows stop launching once recorded spend reaches it">
        </label>
        <button class="btn-primary btn-sm" id="btn-exp-launch" title="run the full design as a batch">
          <i class="ti ti-player-play" aria-hidden="true"></i> run design
        </button>
        <label class="exp-extend-label">+
          <input type="number" id="exp-extend-n" class="exp-extend-inp" min="1" max="20" value="1">
          <button class="btn-ghost btn-sm" id="btn-exp-extend" title="add more replicates per condition, numbering continued from the last launch">extend</button>
        </label>
        <button class="btn-ghost btn-sm" id="btn-exp-design" title="edit the stored design">edit</button>
        <a class="btn-ghost btn-sm" id="btn-exp-dataset" href="/experiments/${esc(exp.experiment_id)}/dataset.csv" download
           title="one run per row: condition factors, metrics, and manifest columns">
          <i class="ti ti-table-export" aria-hidden="true"></i> dataset
        </a>
      </div>
    </div>`;
}

function _wireDesignPanel(exp, detailEl) {
  const designBtn = detailEl.querySelector('#btn-exp-design');
  if (designBtn) designBtn.addEventListener('click', () => _renderSpecBuilder(exp, detailEl));

  const launchBtn = detailEl.querySelector('#btn-exp-launch');
  if (launchBtn) launchBtn.addEventListener('click', () => _launch(exp, detailEl, 'run'));

  const extendBtn = detailEl.querySelector('#btn-exp-extend');
  if (extendBtn) extendBtn.addEventListener('click', () => _launch(exp, detailEl, 'extend'));
}

async function _launch(exp, detailEl, mode) {
  const statusDiv = detailEl.querySelector('#exp-launch-status');
  const budgetInp = detailEl.querySelector('#exp-launch-budget');
  const body = { mode };
  if (budgetInp && budgetInp.value) body.budget_usd = parseFloat(budgetInp.value);
  if (mode === 'extend') {
    body.replicates = parseInt(detailEl.querySelector('#exp-extend-n').value, 10) || 1;
  }
  const btn = detailEl.querySelector(mode === 'extend' ? '#btn-exp-extend' : '#btn-exp-launch');
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = 'queuing…';
  try {
    const res = await fetch(`/experiments/${exp.experiment_id}/launch`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'launch failed');
    statusDiv.style.display = 'block';
    _pollBatch(data.job_id, exp, statusDiv, detailEl);
  } catch (err) {
    statusDiv.style.display = 'block';
    statusDiv.innerHTML = `<p class="exp-import-error">${esc(String(err))}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function _renderSpecBuilder(exp, detailEl) {
  const holder = detailEl.querySelector('#exp-spec-builder');
  const spec = exp.spec && !exp.spec.rows ? exp.spec : null;
  const base = (spec && spec.base) || {};
  const factors = (spec && spec.factors) || [];

  const fieldOptions = sel =>
    _SPEC_FIELDS.map(f => `<option value="${f}" ${f === sel ? 'selected' : ''}>${f}</option>`).join('');

  const factorRow = (f = { field: 'proposition_model', levels: [] }) => `
    <div class="exp-factor-row">
      <select class="exp-factor-field" title="config field this factor varies">${fieldOptions(f.field)}</select>
      <input type="text" class="exp-factor-levels" placeholder="levels, comma-separated (e.g. kimi-k3, gpt-4.1)"
             value="${esc((f.levels || []).join(', '))}">
      <button class="btn-ghost btn-sm exp-factor-remove" title="remove this factor"><i class="ti ti-x" aria-hidden="true"></i></button>
    </div>`;

  holder.style.display = 'block';
  holder.innerHTML = `
    <div class="exp-spec-form">
      <p class="exp-spec-help">a design is a base config plus factors: every combination of factor levels becomes one condition, run once per replicate. fields left out of the base use the debate defaults.</p>
      <div class="exp-spec-base">
        <label>topic <input type="text" id="spec-topic" value="${esc(base.topic || '')}" placeholder="the motion every run debates (unless topic is a factor)"></label>
        <label>token budget <input type="number" id="spec-budget" value="${base.token_budget || 100000}" min="1000" step="1000"></label>
        <label>replicates <input type="number" id="spec-replicates" value="${(spec && spec.replicates) || 3}" min="1" max="50" title="runs per condition"></label>
      </div>
      <div id="spec-factors">${factors.map(factorRow).join('') || factorRow()}</div>
      <button class="btn-ghost btn-sm" id="spec-add-factor"><i class="ti ti-plus" aria-hidden="true"></i> add factor</button>
      <div class="exp-spec-actions">
        <button class="btn-solid btn-sm" id="spec-preview-btn">preview matrix</button>
        <button class="btn-primary btn-sm" id="spec-save-btn">save design</button>
        <button class="btn-ghost btn-sm" id="spec-cancel-btn">cancel</button>
      </div>
      <div id="spec-preview-out" class="exp-import-preview" style="display:none"></div>
      <div id="spec-error" class="exp-import-error" style="display:none"></div>
    </div>`;

  holder.querySelector('#spec-add-factor').addEventListener('click', () => {
    holder.querySelector('#spec-factors').insertAdjacentHTML('beforeend', factorRow());
    _wireFactorRemoves();
  });
  const _wireFactorRemoves = () => {
    holder.querySelectorAll('.exp-factor-remove').forEach(btn => {
      btn.onclick = () => btn.closest('.exp-factor-row').remove();
    });
  };
  _wireFactorRemoves();

  holder.querySelector('#spec-cancel-btn').addEventListener('click', () => {
    holder.style.display = 'none';
    holder.innerHTML = '';
  });

  const collect = () => {
    const specOut = {
      base: {},
      factors: [],
      replicates: parseInt(holder.querySelector('#spec-replicates').value, 10) || 1,
    };
    const topic = holder.querySelector('#spec-topic').value.trim();
    if (topic) specOut.base.topic = topic;
    const budget = parseInt(holder.querySelector('#spec-budget').value, 10);
    if (budget) specOut.base.token_budget = budget;
    holder.querySelectorAll('.exp-factor-row').forEach(row => {
      const field = row.querySelector('.exp-factor-field').value;
      const levels = row.querySelector('.exp-factor-levels').value
        .split(',').map(s => s.trim()).filter(Boolean)
        .map(v => _coerceLevel(field, v));
      if (levels.length) specOut.factors.push({ field, levels });
    });
    return specOut;
  };

  const showError = msg => {
    const el = holder.querySelector('#spec-error');
    el.style.display = msg ? 'block' : 'none';
    el.textContent = msg || '';
  };

  holder.querySelector('#spec-preview-btn').addEventListener('click', async () => {
    showError('');
    const out = holder.querySelector('#spec-preview-out');
    try {
      const res = await fetch(`/experiments/${exp.experiment_id}/spec/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spec: collect() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'preview failed');
      out.style.display = 'block';
      out.innerHTML = `
        <div class="exp-import-preview-head">
          <span>${data.count} run${data.count !== 1 ? 's' : ''} per launch</span>
          <span class="exp-batch-est" id="spec-est-total"></span>
        </div>
        <div class="exp-import-list-wrap">
          <ul class="exp-import-list">
            ${data.rows.map((r, i) => `
              <li>
                <span>${_conditionText(r.condition)}</span>
                <span class="exp-row-est" data-spec-est="${i}"></span>
              </li>`).join('')}
          </ul>
        </div>`;
      _estimateSpecRows(data.rows, out);
    } catch (err) {
      showError(String(err.message || err));
    }
  });

  holder.querySelector('#spec-save-btn').addEventListener('click', async () => {
    showError('');
    try {
      const res = await fetch(`/experiments/${exp.experiment_id}/spec`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spec: collect() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'save failed');
      _selectExperiment(exp);   // re-fetches and re-renders with the stored spec
    } catch (err) {
      showError(String(err.message || err));
    }
  });
}

// Pre-flight pricing for previewed spec rows, through the same estimate
// endpoint the CSV preview uses; unpriceable rows just show no figure.
function _estimateSpecRows(rows, out) {
  const est = new Array(rows.length).fill(null);
  let unpriced = rows.length;
  const totalEl = out.querySelector('#spec-est-total');
  const sync = () => {
    const priced = est.filter(v => v != null);
    if (!priced.length) return;
    const total = priced.reduce((a, b) => a + b, 0);
    totalEl.textContent =
      `est. ${formatCost(total, unpriced > 0)} per launch${unpriced ? ` (${unpriced} row${unpriced !== 1 ? 's' : ''} unpriced)` : ''}`;
  };
  rows.forEach((r, i) => {
    const c = r.config;
    fetch('/debates/estimate-cost', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prop_model: c.proposition_model || null, opp_model: c.opposition_model || null,
        mod_model: c.moderator_model || null, synth_model: c.synth_model || null,
        token_budget: c.token_budget || 100000,
      }),
    }).then(r2 => r2.json()).then(e => {
      if (e.total_usd == null) return;
      est[i] = e.total_usd;
      unpriced -= 1;
      const cell = out.querySelector(`[data-spec-est="${i}"]`);
      if (cell) cell.textContent = `≈ ${formatCost(e.total_usd)}`;
      sync();
    }).catch(() => {});
  });
}

function _conditionText(cond) {
  if (!cond) return '';
  const parts = Object.entries(cond)
    .filter(([k]) => k !== 'replicate')
    .map(([k, v]) => `${esc(k.replace(/_model$/, ''))}=${esc(String(v))}`);
  if (cond.replicate != null) parts.push(`rep ${cond.replicate}`);
  return parts.join(' · ');
}

// ============================================================
// Condition comparison table
// ============================================================

async function _renderComparison(exp, runs, detailEl) {
  const holder = detailEl.querySelector('#exp-comparison');
  if (!holder) return;
  // Only meaningful once at least one run carries condition labels.
  if (!runs.some(r => r.condition)) { holder.innerHTML = ''; return; }
  let groups;
  try {
    const res = await fetch(`/experiments/${exp.experiment_id}/comparison`);
    groups = (await res.json()).groups;
  } catch (_) { return; }
  if (!groups || !groups.length) return;

  const fmt = (s, dec = 0, prefix = '') => {
    if (!s) return '—';
    const f = v => prefix + v.toFixed(dec);
    return s.min === s.max ? f(s.mean) : `${f(s.mean)} <span class="exp-cmp-range">(${f(s.min)}–${f(s.max)})</span>`;
  };
  holder.innerHTML = `
    <h3 class="exp-cmp-title">conditions</h3>
    <p class="exp-cmp-note">means with ranges across replicates — ranges, not significance tests, at these replicate counts</p>
    <div class="exp-cmp-wrap">
      <table class="exp-cmp-table">
        <thead><tr>
          <th>condition</th><th>runs</th><th>completed</th><th>turns</th>
          <th>tokens</th><th>cost</th><th>citation coverage</th><th>retries</th><th>quote repairs</th>
        </tr></thead>
        <tbody>
          ${groups.map(g => `
            <tr>
              <td>${_conditionText(g.condition) || 'no labels'}</td>
              <td>${g.n}</td>
              <td>${g.completed}/${g.n}</td>
              <td>${fmt(g.turns, 1)}</td>
              <td>${g.tokens ? fmt({ ...g.tokens, mean: g.tokens.mean / 1000, min: g.tokens.min / 1000, max: g.tokens.max / 1000 }, 1) + 'k' : '—'}</td>
              <td>${fmt(g.cost_usd, 2, '$')}</td>
              <td>${g.citation_coverage ? fmt({ ...g.citation_coverage, mean: g.citation_coverage.mean * 100, min: g.citation_coverage.min * 100, max: g.citation_coverage.max * 100 }, 0) + '%' : '—'}</td>
              <td>${fmt(g.retries, 1)}</td>
              <td>${fmt(g.citation_repairs, 1)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
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
    const rows = _previewRows(text);
    if (!rows) {
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
          <span>${rows.length} debate${rows.length !== 1 ? 's' : ''} found</span>
        </label>
        <span class="exp-batch-est" id="exp-batch-est"></span>
        <span class="exp-selected-count" id="exp-selected-count">${rows.length} selected</span>
      </div>
      <div class="exp-import-list-wrap">
        <ul class="exp-import-list">
          ${rows.map((row, i) => `
            <li>
              <label class="exp-check-label">
                <input type="checkbox" class="exp-row-check" data-idx="${i}" checked>
                <span>${esc(row.topic)}</span>
              </label>
              <span class="exp-row-est" data-est-idx="${i}"></span>
            </li>`).join('')}
        </ul>
      </div>
    `;

    const selectAll = preview.querySelector('#exp-select-all');
    const checks    = Array.from(preview.querySelectorAll('.exp-row-check'));
    const countEl   = preview.querySelector('#exp-selected-count');

    // Pre-flight estimates: one estimate call per row, provider resolved
    // server-side (CSV rows carry no provider column). Rows whose models
    // can't be priced simply show no figure; the batch total sums checked,
    // priced rows and says so.
    const rowEst = new Array(rows.length).fill(null);
    const _syncBatchEst = () => {
      const el = preview.querySelector('#exp-batch-est');
      if (!el) return;
      let total = null, unpriced = 0, checkedN = 0;
      checks.forEach((c, i) => {
        if (!c.checked) return;
        checkedN += 1;
        if (rowEst[i] != null) total = (total ?? 0) + rowEst[i];
        else unpriced += 1;
      });
      el.textContent = total == null ? '' :
        `est. ${formatCost(total, unpriced > 0)} for ${checkedN - unpriced} priced row${checkedN - unpriced !== 1 ? 's' : ''}`;
    };
    rows.forEach((row, i) => {
      if (!row.prop_model && !row.opp_model && !row.mod_model && !row.synth_model) return;
      fetch('/debates/estimate-cost', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prop_model: row.prop_model, opp_model: row.opp_model,
          mod_model: row.mod_model, synth_model: row.synth_model,
          token_budget: row.token_budget,
        }),
      }).then(r => r.json()).then(est => {
        if (est.total_usd == null) return;
        rowEst[i] = est.total_usd;
        const cell = preview.querySelector(`[data-est-idx="${i}"]`);
        if (cell) cell.textContent = `≈ ${formatCost(est.total_usd)}`;
        _syncBatchEst();
      }).catch(() => {});
    });

    function _syncCount() {
      const n = checks.filter(c => c.checked).length;
      countEl.textContent = `${n} selected`;
      runBtn.disabled = n === 0;
      selectAll.checked = n === checks.length;
      selectAll.indeterminate = n > 0 && n < checks.length;
      _syncBatchEst();
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
    const budgetInp = detailEl.querySelector('#exp-import-budget');
    if (budgetInp && budgetInp.value) fd.append('budget_usd', budgetInp.value);

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

function _previewRows(csvText) {
  const rows = _parseCsv(csvText).filter(r => r.some(c => c.trim()));
  if (rows.length < 2) return null;
  const headers = rows[0].map(h => h.trim().toLowerCase());
  const topicIdx = headers.indexOf('topic');
  if (topicIdx === -1) return null;
  const col = name => {
    const i = headers.indexOf(name);
    return r => (i === -1 ? '' : (r[i] || '').trim());
  };
  const getProp  = col('proposition_model');
  const getOpp   = col('opposition_model');
  const getMod   = col('moderator_model');
  const getSynth = col('synth_model');
  const getBudget = col('token_budget');
  // Skip blank-topic rows the same way the backend does, keeping indices aligned.
  return rows.slice(1)
    .map(r => ({
      topic: (r[topicIdx] || '').trim(),
      prop_model: getProp(r) || null,
      opp_model: getOpp(r) || null,
      mod_model: getMod(r) || null,
      synth_model: getSynth(r) || null,
      token_budget: parseInt(getBudget(r), 10) || 100_000,
    }))
    .filter(row => row.topic);
}

function _pollBatch(jobId, exp, statusDiv, detailEl) {
  _clearBatchPoll();

  function _renderStatus(job) {
    const settled = job.done + job.failed + (job.interrupted || 0) + (job.skipped || 0);
    const pct = job.total > 0 ? Math.round((settled / job.total) * 100) : 0;
    const done = ['done', 'failed', 'interrupted'].includes(job.status);
    const retryable = (job.failed || 0) + (job.interrupted || 0) + (job.skipped || 0);
    const extras = [
      job.failed ? `${job.failed} failed` : '',
      job.interrupted ? `${job.interrupted} interrupted` : '',
      job.skipped ? `${job.skipped} skipped (budget)` : '',
    ].filter(Boolean).map(s => ` · ${s}`).join('');

    statusDiv.innerHTML = `
      <div class="exp-batch-header">
        <span class="exp-batch-label">batch run · ${job.done}/${job.total} complete${extras}</span>
        <span class="pill ${done ? 'pill-done' : 'pill-live'}">${done ? job.status : 'running'}</span>
        ${done && retryable ? `<button class="btn-ghost btn-sm" id="btn-batch-retry" title="re-run the ${retryable} unfinished row${retryable !== 1 ? 's' : ''} as a new batch">
          <i class="ti ti-refresh" aria-hidden="true"></i> retry ${retryable}</button>` : ''}
      </div>
      <div class="exp-batch-bar-track"><div class="exp-batch-bar-fill" style="width:${pct}%"></div></div>
      <div class="exp-batch-rows-wrap">
        <ol class="exp-batch-rows">
          ${job.rows.map(r => {
            const cls = r.status === 'done' ? 'batch-row-done'
                      : (r.status === 'failed' || r.status === 'interrupted') ? 'batch-row-failed'
                      : r.status === 'skipped' ? 'batch-row-pending'
                      : r.status === 'running' ? 'batch-row-running'
                      : 'batch-row-pending';
            const icon = r.status === 'done'    ? '<i class="ti ti-check"></i>'
                       : r.status === 'failed'  ? '<i class="ti ti-x"></i>'
                       : r.status === 'interrupted' ? '<i class="ti ti-plug-off"></i>'
                       : r.status === 'skipped' ? '<i class="ti ti-cash-off"></i>'
                       : r.status === 'running' ? '<i class="ti ti-loader-2 spin"></i>'
                       : '<i class="ti ti-clock"></i>';
            const sub = (r.status === 'failed' || r.status === 'interrupted' || r.status === 'skipped') && r.error
              ? `<span class="batch-row-error">${esc(r.error)}</span>` : '';
            const cond = r.condition ? ` <span class="exp-cmp-range">${_conditionText(r.condition)}</span>` : '';
            // Once the run exists in the backend, link straight to it.
            const label = r.run_id
              ? `<a class="batch-row-link" href="#/debate/${esc(r.run_id)}">${esc(r.topic)}</a>`
              : `<span>${esc(r.topic)}</span>`;
            return `<li class="exp-batch-row ${cls}">${icon} ${label}${cond}${sub}</li>`;
          }).join('')}
        </ol>
      </div>
    `;

    const retryBtn = statusDiv.querySelector('#btn-batch-retry');
    if (retryBtn) retryBtn.addEventListener('click', async () => {
      retryBtn.disabled = true;
      retryBtn.textContent = 'queuing…';
      try {
        const res = await fetch(`/api/batch/${jobId}/retry`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'retry failed');
        _pollBatch(data.job_id, exp, statusDiv, detailEl);
      } catch (err) {
        retryBtn.disabled = false;
        retryBtn.textContent = 'retry';
      }
    });

    if (done) {
      _clearBatchPoll();
      _refreshList();
      // Switch to the experiment's own view now the runs exist — but only
      // when nothing needs retrying, so the errors and the retry button
      // stay on screen instead of being wiped by the re-render.
      if (exp && exp.experiment_id && retryable === 0) {
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
        <label class="exp-budget-label">ceiling $
          <input type="number" id="exp-import-budget" class="exp-budget-inp" min="0.01" step="0.5"
                 placeholder="none" title="optional spend ceiling for this batch — new rows stop launching once recorded spend reaches it">
        </label>
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
