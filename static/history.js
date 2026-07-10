// history.js — history screen: table rendering, selection, export.

import { esc, formatTokens, triggerDownload } from './render.js';

export async function loadHistory() {
  const tbody     = document.getElementById('history-tbody');
  const empty     = document.getElementById('history-empty');
  const tableWrap = document.getElementById('history-table-wrap');
  const subtitle  = document.getElementById('history-subtitle');

  tbody.innerHTML = '';
  subtitle.textContent = 'loading...';

  let sortCol = null;   // 'title' | 'turns' | 'tokens' | 'status' | null
  let sortDir = 'asc';  // 'asc' | 'desc'
  const selected = new Set();

  try {
    const res = await fetch('/debates');
    if (!res.ok) throw new Error(res.statusText);
    const debates = await res.json();

    if (!debates.length) {
      empty.style.display = 'block';
      tableWrap.style.display = 'none';
      subtitle.textContent = 'no runs yet';
      return;
    }

    empty.style.display = 'none';
    tableWrap.style.display = 'block';

    const live = debates.filter(d => d.status === 'running').length;
    subtitle.textContent = `${debates.length} run${debates.length === 1 ? '' : 's'} · ${live} live`;

    function updateExportBar() {
      const bar   = document.getElementById('export-bar');
      const count = document.getElementById('export-sel-count');
      bar.style.display = selected.size > 0 ? 'flex' : 'none';
      count.textContent = `${selected.size} selected`;
    }

    document.getElementById('check-all').onchange = (e) => {
      tbody.querySelectorAll('.row-check').forEach(cb => {
        cb.checked = e.target.checked;
        e.target.checked ? selected.add(cb.dataset.id) : selected.delete(cb.dataset.id);
      });
      updateExportBar();
    };

    document.getElementById('btn-deselect-all').onclick = () => {
      selected.clear();
      tbody.querySelectorAll('.row-check').forEach(cb => { cb.checked = false; });
      document.getElementById('check-all').checked = false;
      updateExportBar();
    };

    const _batchExport = async (fmt) => {
      if (!selected.size) return;
      const res = await fetch('/debates/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [...selected], format: fmt }),
      });
      triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
    };
    document.getElementById('btn-export-selected-json').onclick = () => _batchExport('json');
    document.getElementById('btn-export-selected-md').onclick   = () => _batchExport('markdown');

    // --- Sorting ---

    const SORT_KEY = {
      title:  d => (d.debate_title || d.topic || '').toLowerCase(),
      turns:  d => d.turn || 0,
      tokens: d => d.total_tokens || 0,
      status: d => d.status || '',
    };

    function sortedDebates() {
      if (!sortCol) return debates;
      const key = SORT_KEY[sortCol];
      return [...debates].sort((a, b) => {
        const av = key(a), bv = key(b);
        const cmp = av < bv ? -1 : av > bv ? 1 : 0;
        return sortDir === 'asc' ? cmp : -cmp;
      });
    }

    function updateSortHeaders() {
      document.querySelectorAll('th[data-sort]').forEach(th => {
        const icon = th.querySelector('.sort-icon');
        if (!icon) return;
        if (th.dataset.sort === sortCol) {
          icon.textContent = sortDir === 'asc' ? ' ▲' : ' ▼';
          th.classList.add('sort-active');
        } else {
          icon.textContent = '';
          th.classList.remove('sort-active');
        }
      });
    }

    function renderRows() {
      // Preserve checked state across re-renders.
      tbody.innerHTML = '';
      sortedDebates().forEach(d => {
        const statusCls = d.status === 'running' ? 'pill-live' : d.status === 'paused' ? 'pill-paused' : 'pill-done';
        const modeTag   = d.steelman_mode ? '<span class="pill-rapoport">Rapoport</span>' : '';
        const row = document.createElement('tr');
        row.innerHTML = `
          <td class="col-check" onclick="event.stopPropagation()">
            <input type="checkbox" class="row-check" data-id="${esc(d.session_id)}"${selected.has(d.session_id) ? ' checked' : ''}>
          </td>
          <td class="cell-id">${esc(d.session_id)}</td>
          <td class="cell-title">${esc(d.debate_title || d.topic || '—')}</td>
          <td class="cell-meta">${esc(d.proposition_nickname || 'P')} vs ${esc(d.opposition_nickname || 'O')}</td>
          <td class="cell-meta">${esc(d.turn || 0)}</td>
          <td class="cell-tok">${formatTokens(d.total_tokens || 0)}</td>
          <td>${modeTag}</td>
          <td><span class="pill ${statusCls}">${esc(d.status)}</span></td>
          <td class="col-check" onclick="event.stopPropagation()">
            <button class="btn-ghost btn-sm row-export-btn" data-id="${esc(d.session_id)}" title="export JSON">
              <i class="ti ti-download" aria-hidden="true"></i>
            </button>
            <button class="btn-ghost btn-sm row-export-btn-md" data-id="${esc(d.session_id)}" title="export MD">
              <i class="ti ti-markdown" aria-hidden="true"></i>
            </button>
          </td>
        `;
        row.querySelector('.row-check').onchange = (e) => {
          e.target.checked ? selected.add(d.session_id) : selected.delete(d.session_id);
          updateExportBar();
        };
        row.querySelector('.row-export-btn').onclick = async () => {
          const res = await fetch(`/debates/${d.session_id}/export?format=json`);
          triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
        };
        row.querySelector('.row-export-btn-md').onclick = async () => {
          const res = await fetch(`/debates/${d.session_id}/export?format=markdown`);
          triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
        };
        row.addEventListener('click', () => { window.location.hash = `#/debate/${d.session_id}`; });
        tbody.appendChild(row);
      });
    }

    document.querySelectorAll('th[data-sort]').forEach(th => {
      th.onclick = () => {
        if (sortCol === th.dataset.sort) {
          sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          sortCol = th.dataset.sort;
          sortDir = 'asc';
        }
        updateSortHeaders();
        renderRows();
      };
    });

    renderRows();

  } catch (e) {
    subtitle.textContent = 'error loading history';
    console.error(e);
  }
}
