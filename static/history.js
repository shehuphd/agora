// history.js — history screen: table rendering, selection, export.

import { esc, formatTokens, triggerDownload } from './render.js';

export async function loadHistory() {
  const tbody     = document.getElementById('history-tbody');
  const empty     = document.getElementById('history-empty');
  const tableWrap = document.getElementById('history-table-wrap');
  const subtitle  = document.getElementById('history-subtitle');

  tbody.innerHTML = '';
  subtitle.textContent = 'loading...';

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

    const selected = new Set();

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

    document.getElementById('btn-export-selected').onclick = async () => {
      if (!selected.size) return;
      const res = await fetch('/debates/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [...selected] }),
      });
      triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
    };

    debates.forEach(d => {
      const statusCls = d.status === 'running' ? 'pill-live' : d.status === 'paused' ? 'pill-paused' : 'pill-done';
      const modeTag   = d.steelman_mode ? '<span class="pill-rapoport">Rapoport</span>' : '';
      const row = document.createElement('tr');
      row.innerHTML = `
        <td class="col-check" onclick="event.stopPropagation()">
          <input type="checkbox" class="row-check" data-id="${esc(d.session_id)}">
        </td>
        <td class="cell-id">${esc(d.session_id)}</td>
        <td class="cell-title">${esc(d.debate_title || d.topic || '—')}</td>
        <td class="cell-meta">${esc(d.proposition_nickname || 'P')} vs ${esc(d.opposition_nickname || 'O')}</td>
        <td class="cell-meta">${esc(d.turn || 0)}</td>
        <td class="cell-tok">${formatTokens(d.total_tokens || 0)}</td>
        <td>${modeTag}</td>
        <td><span class="pill ${statusCls}">${esc(d.status)}</span></td>
        <td class="col-check" onclick="event.stopPropagation()">
          <button class="btn-ghost btn-sm row-export-btn" data-id="${esc(d.session_id)}" title="export">
            <i class="ti ti-download" aria-hidden="true"></i>
          </button>
        </td>
      `;
      row.querySelector('.row-check').onchange = (e) => {
        e.target.checked ? selected.add(d.session_id) : selected.delete(d.session_id);
        updateExportBar();
      };
      row.querySelector('.row-export-btn').onclick = async () => {
        const res = await fetch(`/debates/${d.session_id}/export`);
        triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
      };
      row.addEventListener('click', () => { window.location.hash = `#/debate/${d.session_id}`; });
      tbody.appendChild(row);
    });
  } catch (e) {
    subtitle.textContent = 'error loading history';
    console.error(e);
  }
}
