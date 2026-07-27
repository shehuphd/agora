// traces.js — Trace log viewer (#/traces)
import { esc } from './render.js';

let _traces = [];
let _filterRunId = '';
let _filterAction = '';
let _filterStatus = '';
let _expanded = new Set();

// ============================================================
// Public entry point (called by router)
// ============================================================

export async function loadTraces() {
  const hash = window.location.hash;
  const m = hash.match(/[?&]run_id=([^&]+)/);
  if (m) _filterRunId = decodeURIComponent(m[1]);

  const screen = document.getElementById('screen-traces');
  _renderShell(screen);
  await _load();
}

// ============================================================
// Shell
// ============================================================

function _renderShell(screen) {
  screen.innerHTML = `
    <div class="pg-head">
      <div>
        <p class="pg-eyebrow">Observability</p>
        <h1 class="pg-title">trace log</h1>
        <p class="pg-sub" id="traces-subtitle">loading…</p>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn-ghost btn-sm" id="btn-traces-refresh" title="refresh">
          <i class="ti ti-refresh" aria-hidden="true"></i> refresh
        </button>
        <button class="btn-ghost btn-sm" id="btn-traceact-viewer"
                title="Open the TraceAct viewer (starts it if not already running)">
          <i class="ti ti-external-link" aria-hidden="true"></i> traceact viewer
        </button>
      </div>
    </div>

    <div style="display:flex;gap:8px;margin-bottom:16px;align-items:center;flex-wrap:wrap">
      <input type="text" id="trace-run-input" placeholder="filter by run id…"
        value="${esc(_filterRunId)}"
        style="flex:1;min-width:200px;max-width:340px;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--card-bg);color:var(--text);font-size:13px">
      <select id="trace-action-select"
        style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--card-bg);color:var(--text);font-size:13px">
        <option value="">all actions</option>
        <option value="debate.run" ${_filterAction==='debate.run'?'selected':''}>debate.run</option>
        <option value="agent.generate" ${_filterAction==='agent.generate'?'selected':''}>agent.generate</option>
      </select>
      <select id="trace-status-select"
        style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--card-bg);color:var(--text);font-size:13px">
        <option value="">all statuses</option>
        <option value="completed" ${_filterStatus==='completed'?'selected':''}>completed</option>
        <option value="failed" ${_filterStatus==='failed'?'selected':''}>failed</option>
      </select>
      <button class="btn-ghost btn-sm" id="btn-trace-clear">clear</button>
    </div>

    <div id="traces-body">
      <p style="color:var(--text-muted);font-size:13px">loading traces…</p>
    </div>
  `;
  _wireControls();
}

// ============================================================
// Load + filter
// ============================================================

async function _load() {
  const params = new URLSearchParams();
  if (_filterRunId) params.set('run_id', _filterRunId);
  if (_filterAction) params.set('action', _filterAction);
  if (_filterStatus) params.set('status', _filterStatus);

  const body = document.getElementById('traces-body');
  if (body) body.innerHTML = '<p style="color:var(--text-muted);font-size:13px">loading…</p>';

  try {
    const resp = await fetch(`/api/traces?${params}`);
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    _traces = data.traces || [];
    _renderTable(data.total || _traces.length, data.more === true);
  } catch (e) {
    if (body) body.innerHTML = `<p style="color:var(--danger);font-size:13px">failed to load traces: ${esc(e.message)}</p>`;
  }
}

function _wireControls() {
  document.getElementById('trace-run-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { _filterRunId = e.target.value.trim(); _expanded.clear(); _load(); }
  });
  document.getElementById('trace-run-input').addEventListener('blur', e => {
    _filterRunId = e.target.value.trim();
  });
  document.getElementById('trace-action-select').addEventListener('change', e => {
    _filterAction = e.target.value; _expanded.clear(); _load();
  });
  document.getElementById('trace-status-select').addEventListener('change', e => {
    _filterStatus = e.target.value; _expanded.clear(); _load();
  });
  document.getElementById('btn-traces-refresh').addEventListener('click', () => {
    _expanded.clear(); _load();
  });
  document.getElementById('btn-traceact-viewer').addEventListener('click', async () => {
    const btn = document.getElementById('btn-traceact-viewer');
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="ti ti-loader-2 ti-spin" aria-hidden="true"></i> starting…';
    const errEl = document.getElementById('viewer-error');
    if (errEl) errEl.remove();
    try {
      // Carry the tab's active filters into the viewer as pre-filters.
      const vp = new URLSearchParams();
      if (_filterRunId) vp.set('run_id', _filterRunId);
      if (_filterAction) vp.set('action', _filterAction);
      if (_filterStatus) vp.set('status', _filterStatus);
      const qs = vp.toString();
      const data = await fetch('/api/launch-viewer' + (qs ? `?${qs}` : '')).then(r => r.json());
      if (data.ready && data.url) {
        btn.title = data.url;
        window.open(data.url, '_blank', 'noopener');
        _load(); // refresh trace list after opening viewer
      } else {
        _showViewerError(data.error || 'Could not start viewer.');
      }
    } catch (e) {
      _showViewerError('Request failed: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  });
  document.getElementById('btn-trace-clear').addEventListener('click', () => {
    _filterRunId = ''; _filterAction = ''; _filterStatus = '';
    document.getElementById('trace-run-input').value = '';
    document.getElementById('trace-action-select').value = '';
    document.getElementById('trace-status-select').value = '';
    _expanded.clear(); _load();
  });
}

// ============================================================
// Table
// ============================================================

function _renderTable(total, more = false) {
  const shown = _traces.length;
  const subtitle = document.getElementById('traces-subtitle');
  if (subtitle) {
    // `more` means the server hit its result limit — older matches exist
    // beyond what was returned, but the exact count is unknown.
    subtitle.textContent = more
      ? `showing the ${shown} most recent traces (more exist)`
      : `${total} trace${total !== 1 ? 's' : ''}`;
    if (_filterRunId) subtitle.textContent += ` · run ${_filterRunId}`;
  }

  const body = document.getElementById('traces-body');
  if (!body) return;

  if (!_traces.length) {
    body.innerHTML = `<div class="empty-state" style="margin-top:40px">
      <div class="empty-icon"><i class="ti ti-file-analytics" aria-hidden="true"></i></div>
      <p class="empty-title">no traces yet</p>
      <p class="empty-body">Traces appear here after a debate runs. Run the traceact viewer for a richer experience.</p>
      <code style="font-size:12px;color:var(--text-muted)">traceact view data/traces/traces.jsonl</code>
    </div>`;
    return;
  }

  body.innerHTML = `
    <table class="run-table" id="traces-table">
      <thead>
        <tr>
          <th>time</th>
          <th>action</th>
          <th>actor</th>
          <th>status</th>
          <th>duration</th>
          <th>run id</th>
          <th style="text-align:right">events</th>
          <th style="text-align:right">errors</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="traces-tbody"></tbody>
    </table>
  `;

  const tbody = document.getElementById('traces-tbody');
  for (const t of _traces) _appendRow(tbody, t);
}

function _appendRow(tbody, t) {
  const id = t.trace_id || '';
  const expanded = _expanded.has(id);

  const statusClass = t.status === 'completed' ? 'status-done'
    : t.status === 'failed' ? 'status-error' : 'status-live';
  const dur = t.duration_ms != null ? `${Math.round(t.duration_ms)} ms` : '—';
  const evCount = (t.events || []).length;
  const errCount = (t.errors || []).length;
  const runId = t.correlation_id || '—';
  const runShort = runId.length > 20 ? runId.slice(0, 20) + '…' : runId;
  const time = t.started_at
    ? new Date(t.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '—';

  const tr = document.createElement('tr');
  tr.style.cursor = 'pointer';
  tr.innerHTML = `
    <td style="font-size:12px;color:var(--text-muted);white-space:nowrap">${esc(time)}</td>
    <td><code style="font-size:12px">${esc(t.action || '—')}</code></td>
    <td style="font-size:12px">${esc(t.actor || '—')}</td>
    <td><span class="status-badge ${statusClass}" style="font-size:11px">${esc(t.status || '—')}</span></td>
    <td style="font-size:12px;color:var(--text-muted)">${esc(dur)}</td>
    <td style="font-size:11px;color:var(--text-muted);font-family:monospace" title="${esc(runId)}">${esc(runShort)}</td>
    <td style="font-size:12px;text-align:right">${evCount || '—'}</td>
    <td style="font-size:12px;text-align:right;${errCount ? 'color:var(--danger)' : ''}">${errCount || '—'}</td>
    <td style="font-size:11px;color:var(--text-muted);width:20px">
      <i class="ti ti-chevron-${expanded ? 'up' : 'down'}" aria-hidden="true"></i>
    </td>
  `;
  tr.addEventListener('click', () => {
    if (_expanded.has(id)) _expanded.delete(id); else _expanded.add(id);
    _rerenderRow(tbody, t, id);
  });
  tbody.appendChild(tr);

  if (expanded) {
    const dr = document.createElement('tr');
    dr.dataset.detail = id;
    const td = document.createElement('td');
    td.colSpan = 9;
    td.style.cssText = 'padding:0;border-bottom:1px solid var(--border)';
    td.innerHTML = _detailHtml(t);
    dr.appendChild(td);
    tbody.appendChild(dr);
  }
}

function _rerenderRow(tbody, t, id) {
  // Remove existing row + detail row then re-append
  for (const el of [...tbody.children]) {
    if (el.dataset && el.dataset.detail === id) { el.remove(); continue; }
    // Find the row by checking if its first click handler matches — simpler: just re-render the whole table
  }
  // Simplest: full re-render (traces list is small)
  _renderTable(_traces.length);
}

// ============================================================
// Detail panel
// ============================================================

function _detailHtml(t) {
  const parts = [];

  if (t.inputs && Object.keys(t.inputs).length) {
    parts.push(_section('inputs', `<pre style="margin:0;font-size:11px;overflow-x:auto;white-space:pre-wrap">${esc(JSON.stringify(t.inputs, null, 2))}</pre>`));
  }
  if (t.outputs && Object.keys(t.outputs).length) {
    parts.push(_section('outputs', `<pre style="margin:0;font-size:11px;overflow-x:auto;white-space:pre-wrap">${esc(JSON.stringify(t.outputs, null, 2))}</pre>`));
  }
  if (t.steps && t.steps.length) {
    const items = t.steps.map((s, i) => `<li style="font-size:12px;padding:2px 0">${esc(s.label || '')}</li>`).join('');
    parts.push(_section(`steps (${t.steps.length})`, `<ol style="margin:0;padding-left:18px">${items}</ol>`));
  }
  if (t.events && t.events.length) {
    const rows = t.events.map(e => {
      const sc = e.status === 'completed' ? 'status-done' : e.status === 'failed' ? 'status-error' : 'status-live';
      const tokens = (e.tokens_in != null)
        ? `<span style="color:var(--text-muted)">in ${e.tokens_in} / out ${e.tokens_out}</span>` : '';
      return `<tr>
        <td><code style="font-size:11px">${esc(e.kind || '')}</code></td>
        <td style="font-size:12px">${esc(e.operation || '—')}</td>
        <td style="font-size:12px;color:var(--text-muted)">${esc(e.target || '—')}</td>
        <td><span class="status-badge ${sc}" style="font-size:10px">${esc(e.status || '')}</span></td>
        <td style="font-size:11px;color:var(--text-muted)">${e.duration_ms != null ? Math.round(e.duration_ms) + ' ms' : '—'}</td>
        <td style="font-size:11px">${tokens}</td>
      </tr>`;
    }).join('');
    parts.push(_section(`events (${t.events.length})`, `
      <table class="run-table" style="font-size:12px">
        <thead><tr><th>kind</th><th>operation</th><th>target</th><th>status</th><th>dur</th><th>tokens</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`));
  }
  if (t.errors && t.errors.length) {
    parts.push(_section(`errors (${t.errors.length})`,
      `<pre style="margin:0;font-size:11px;color:var(--danger);overflow-x:auto;white-space:pre-wrap">${esc(JSON.stringify(t.errors, null, 2))}</pre>`));
  }
  if (!parts.length) {
    parts.push('<p style="font-size:12px;color:var(--text-muted);margin:0">no detail recorded</p>');
  }

  const meta = `<p style="font-size:11px;color:var(--text-muted);margin:12px 0 0">
    trace_id: <code>${esc(t.trace_id || '—')}</code>
    ${t.parent_trace_id ? ` · parent: <code>${esc(t.parent_trace_id)}</code>` : ''}
    ${t.budget_hit ? ' · <span style="color:var(--danger)">budget hit</span>' : ''}
  </p>`;

  return `<div style="padding:12px 16px;background:var(--card-bg)">
    <div style="display:flex;gap:16px;flex-wrap:wrap">${parts.join('')}</div>
    ${meta}
  </div>`;
}

function _showViewerError(msg) {
  const existing = document.getElementById('viewer-error');
  if (existing) existing.remove();
  const el = document.createElement('p');
  el.id = 'viewer-error';
  el.style.cssText = 'color:var(--danger);font-size:13px;margin:0 0 12px';
  el.textContent = msg;
  const body = document.getElementById('traces-body');
  if (body) body.insertAdjacentElement('beforebegin', el);
}

function _section(title, content) {
  return `<div style="min-width:180px;flex:1">
    <p style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin:0 0 6px">${esc(title)}</p>
    ${content}
  </div>`;
}
