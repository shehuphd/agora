// debate.js — live debate screen: SSE streaming, token tracking, rerun wiring.

import {
  esc, formatTokens, triggerDownload,
  appendActBubble, appendIntroBubble, appendErrorBubble, appendSystemBubble,
  showThinkingBubble, removeThinkingBubble, markDebateClosed,
  renderTokenStrip, updateTerminationTracker,
} from './render.js';

// Module-level state for the active debate session.
let activeSSE      = null;
let debateTok      = { total: 0, proposition: 0, opposition: 0, moderator: 0 };
let debateCfg      = {};
let _runId     = null;
let _isPaused      = false;
let _overrideCount = 0;
let _effectiveBudget = 0;

// Closure reasons that indicate a natural/intentional end — not resumable.
const _NON_RESUMABLE = [
  "max_turns", "max_time_minutes", "token_budget",
  "user_requested_end", "quota_exhausted",
  "propose met with concede", "repetition detected", "challenge_rate_floor",
];

function _isResumable(status, closureReason) {
  if (status === 'running' || status === 'error') return true;
  if (!closureReason) return true;
  const cr = closureReason.toLowerCase();
  return !_NON_RESUMABLE.some(kw => cr.includes(kw));
}

function _showContinueButton(runId) {
  const btn = document.getElementById('btn-continue');
  if (!btn) return;

  btn.style.display = 'inline-flex';
  btn.disabled      = false;
  btn.innerHTML = '<i class="ti ti-player-play" aria-hidden="true"></i> continue';

  btn.onclick = async () => {
    btn.disabled  = true;
    btn.innerHTML = '<i class="ti ti-loader-2" aria-hidden="true"></i> continuing…';
    try {
      const res = await fetch(`/debates/${runId}/continue`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        btn.disabled  = false;
        btn.innerHTML = '<i class="ti ti-player-play" aria-hidden="true"></i> continue';
        appendSystemBubble('ti-alert-circle', err.detail || 'Could not start the continuation.');
        return;
      }
      const { run_id: newId } = await res.json();
      window.location.hash = `#/debate/${newId}`;
    } catch (_) {
      btn.disabled  = false;
      btn.innerHTML = '<i class="ti ti-player-play" aria-hidden="true"></i> continue';
      appendSystemBubble('ti-wifi-off', 'Network error — could not start continuation.');
    }
  };
}

export async function loadDebate(runId) {
  debateTok = { total: 0, proposition: 0, opposition: 0, moderator: 0 };
  let _tokOffset = { total: 0, proposition: 0, opposition: 0, moderator: 0 };
  debateCfg = {};
  _runId     = runId;
  _isPaused      = false;
  _overrideCount = 0;
  _effectiveBudget = 0;
  document.getElementById('act-feed').innerHTML = '';
  document.getElementById('dh-title').textContent = 'loading...';
  document.getElementById('dh-id').textContent    = runId;
  ['tok-total', 'tok-prop', 'tok-opp', 'tok-mod'].forEach(id => {
    document.getElementById(id).textContent = '0';
  });

  // Reset controls that markDebateClosed() mutates, so a rerun on the same
  // screen doesn't inherit the previous session's closed state.
  const turnBadge = document.getElementById('dh-turn-badge');
  if (turnBadge) {
    turnBadge.textContent    = 'turn 0';
    turnBadge.style.background = '';
    turnBadge.style.color      = '';
  }
  const endBtn = document.getElementById('btn-end');
  if (endBtn) {
    endBtn.disabled  = false;
    endBtn.innerHTML = '<i class="ti ti-x" aria-hidden="true"></i> end';
  }

  const rerunBtn = document.getElementById('btn-rerun');
  if (rerunBtn) { rerunBtn.style.display = 'none'; rerunBtn.onclick = null; }

  const continueBtn = document.getElementById('btn-continue');
  if (continueBtn) { continueBtn.style.display = 'none'; continueBtn.onclick = null; }

  _wirePauseButton(runId);
  _wireEndButton(runId);
  _wireOverridePanel(runId);

  const exportJsonBtn = document.getElementById('btn-export-debate-json');
  if (exportJsonBtn) {
    exportJsonBtn.onclick = async () => {
      const res = await fetch(`/debates/${runId}/export?format=json`);
      triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
    };
  }
  const exportMdBtn = document.getElementById('btn-export-debate-md');
  if (exportMdBtn) {
    exportMdBtn.onclick = async () => {
      const res = await fetch(`/debates/${runId}/export?format=markdown`);
      triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
    };
  }

  try {
    const res = await fetch(`/debates/${runId}`);
    if (res.ok) {
      const data = await res.json();
      document.getElementById('dh-title').textContent = data.debate_title || data.topic || runId;
      debateCfg = data.config || {};
      _effectiveBudget = debateCfg.token_budget || 100_000;
      if (data.token_offset) {
        _tokOffset = { ...data.token_offset };
        debateTok  = { ...data.token_offset };
      }
      _renderDebateChips(debateCfg);
      if (debateCfg.max_turns) {
        const tv = document.getElementById('term-turns-val');
        if (tv) tv.textContent = `0 / ${debateCfg.max_turns}`;
      }
      (data.acts || []).forEach(act => {
        appendActBubble(act);
        _accumulateTokens(act);
      });
      (data.override_log || []).forEach(ov => {
        if (ov.field === 'token_budget') _effectiveBudget = ov.new_value;
      });
      _overrideCount = (data.override_log || []).length;
      const ocEl = document.getElementById('override-count');
      if (ocEl) ocEl.textContent = _overrideCount;
      renderTokenStrip(debateTok, _effectiveBudget);
      _wireRerunButton(debateCfg);
      if (data.status === 'closed' || data.status === 'error') {
        markDebateClosed();
        if (data.is_continuable) _showContinueButton(runId);
        return;
      }
      if (data.status === 'running') {
        // Stuck running means server restarted — server will confirm continuability.
        if (data.is_continuable) _showContinueButton(runId);
      }
    }
  } catch (e) { console.warn('debate state load failed', e); }

  _openSSE(runId);
}

function _wireRerunButton(cfg) {
  const btn = document.getElementById('btn-rerun');
  if (!btn) return;
  btn.onclick = () => {
    // Build pendingDebate directly from the stored DB config so no form round-trip
    // is needed. This avoids the slider scale mismatches (_prefillNewForm passed raw
    // float temperatures and raw token counts to integer sliders, causing wrong values)
    // and the timing race between loadNew()'s async fetch and the 50ms prefill timeout.
    const pending = {
      topic:                   cfg.topic                  || '',
      debate_title:            '',
      prop_model:              cfg.proposition_model      || 'claude-sonnet-4-6',
      opp_model:               cfg.opposition_model       || 'gpt-4o',
      mod_model:               cfg.moderator_model        || 'claude-opus-4-8',
      prop_nickname:           cfg.proposition_nickname   || 'Thesis',
      opp_nickname:            cfg.opposition_nickname    || 'Antithesis',
      mod_nickname:            'Moderator',
      prop_temperature:        cfg.temperature_proposition ?? 0.7,
      opp_temperature:         cfg.temperature_opposition  ?? 0.4,
      opp_aggression:          cfg.aggression              ?? 0.8,
      max_turns:               cfg.max_turns               ?? 15,
      max_time_minutes:        cfg.max_time_minutes        ?? 30,
      token_budget:            cfg.token_budget            ?? 100_000,
      min_challenges:          cfg.min_challenges          ?? 2,
      min_concessions:         cfg.min_concessions         ?? 1,
      repetition_tolerance:    cfg.repetition_tolerance    ?? 1,
      require_steelman:        cfg.steelman_mode           ?? false,
      require_full_resolution: false,
      auto_generate_title:     true,
    };
    sessionStorage.setItem('pendingDebate', JSON.stringify(pending));
    window.location.hash = '#/confirm';
  };
}

function _openSSE(runId) {
  if (activeSSE) { activeSSE.close(); activeSSE = null; }

  activeSSE = new EventSource(`/debates/${runId}/stream`);

  activeSSE.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === 'intro') {
      removeThinkingBubble();
      appendIntroBubble(msg);
      return;
    }
    if (msg.type === 'thinking') {
      removeThinkingBubble();
      showThinkingBubble(msg.agent, msg.role);
      return;
    }
    if (msg.type === 'close') {
      removeThinkingBubble();
      if (activeSSE) { activeSSE.close(); activeSSE = null; }
      markDebateClosed();
      return;
    }
    if (msg.type === 'error') {
      removeThinkingBubble();
      appendErrorBubble(msg.message);
      return;
    }
    if (msg.type === 'paused') {
      removeThinkingBubble();
      _isPaused = true;
      _setPauseButtonState(true);
      appendSystemBubble('ti-player-pause', 'Debate paused. Press resume to continue.');
      return;
    }
    if (msg.type === 'resumed') {
      _isPaused = false;
      _setPauseButtonState(false);
      appendSystemBubble('ti-player-play', 'Debate resumed.');
      return;
    }
    if (msg.type === 'override') {
      _overrideCount++;
      const el = document.getElementById('override-count');
      if (el) el.textContent = _overrideCount;
      if (msg.field === 'token_budget') {
        _effectiveBudget = msg.new_value;
        renderTokenStrip(debateTok, _effectiveBudget);
        const old = msg.old_value != null ? `${Number(msg.old_value).toLocaleString()} → ` : '';
        appendSystemBubble('ti-adjustments-horizontal',
          `Token budget updated: ${old}${Number(msg.new_value).toLocaleString()}`);
      }
      return;
    }

    // Real act
    const act = msg;
    removeThinkingBubble();
    appendActBubble(act);
    _accumulateTokens(act);
    renderTokenStrip(debateTok, _effectiveBudget || debateCfg.token_budget || 100_000);

    if (act.turn) {
      document.getElementById('dh-turn-badge').textContent = `turn ${act.turn}`;
    }

    if (act.act_type === 'STATUS') {
      try {
        updateTerminationTracker(JSON.parse(act.content), debateCfg);
      } catch (_) {}
    }

    if (act.act_type === 'CLOSE') {
      // Don't close SSE here — ARGUMENT_MAP follows immediately after CLOSE.
      markDebateClosed();
    }
  };

  let _reconnectCount = 0;
  activeSSE.onerror = async () => {
    removeThinkingBubble();
    _reconnectCount++;
    // Short wait for the browser's built-in SSE reconnect to fire.
    await new Promise(r => setTimeout(r, 2000));
    if (!activeSSE) return; // already cleaned up

    try {
      // Check aliveness first — fast, tells us if the server restarted.
      const aliveRes = await fetch(`/debates/${runId}/alive`);
      if (aliveRes.ok) {
        const { alive } = await aliveRes.json();
        if (!alive) {
          // Session is gone from memory — server restarted mid-debate.
          if (activeSSE) { activeSSE.close(); activeSSE = null; }
          appendSystemBubble('ti-server-off',
            'The server restarted while this debate was running. The transcript so far is saved — continue to pick up where it left off, or rerun for a fresh start.');
          markDebateClosed();
          // For server restarts, check continuability via the API before showing the button.
          fetch(`/debates/${runId}`).then(r => r.ok ? r.json() : null).then(d => {
            if (d?.is_continuable) _showContinueButton(runId);
          }).catch(() => {});
          return;
        }
      }
      // Runner still alive — check if it closed cleanly while we were disconnected.
      const check = await fetch(`/debates/${runId}`);
      if (check.ok) {
        const d = await check.json();
        if (d.status === 'closed') {
          if (activeSSE) { activeSSE.close(); activeSSE = null; }
          markDebateClosed();
          return;
        }
      }
    } catch (_) {}

    // Runner alive but SSE dropped — transient network issue, keep retrying.
    if (_reconnectCount <= 3) {
      appendSystemBubble('ti-wifi-off', `Connection interrupted — reconnecting… (attempt ${_reconnectCount})`);
    } else {
      if (activeSSE) { activeSSE.close(); activeSSE = null; }
      appendSystemBubble('ti-wifi-off', 'Connection lost after several attempts. Refresh to check the latest state.');
      markDebateClosed();
    }
  };
}

function _accumulateTokens(act) {
  const delta = (act.input_tokens || 0) + (act.output_tokens || 0);
  debateTok.total += delta;
  if (act.agent_role === 'proposition') debateTok.proposition += delta;
  if (act.agent_role === 'opposition')  debateTok.opposition  += delta;
  if (act.agent_role === 'moderator')   debateTok.moderator   += delta;
}

function _setPauseButtonState(paused) {
  const btn = document.getElementById('btn-pause');
  if (!btn) return;
  const icon = btn.querySelector('i');
  if (paused) {
    btn.setAttribute('aria-label', 'resume debate');
    btn.title = 'resume';
    if (icon) { icon.className = 'ti ti-player-play'; }
    btn.classList.add('btn-paused');
  } else {
    btn.setAttribute('aria-label', 'pause debate');
    btn.title = 'pause';
    if (icon) { icon.className = 'ti ti-player-pause'; }
    btn.classList.remove('btn-paused');
  }
}

function _wirePauseButton(runId) {
  const btn = document.getElementById('btn-pause');
  if (!btn) return;
  btn.onclick = async () => {
    const action = _isPaused ? 'resume' : 'pause';
    try {
      await fetch(`/debates/${runId}/${action}`, { method: 'POST' });
      // State update happens when the runner sends the paused/resumed SSE event.
    } catch (e) { console.error('pause/resume failed', e); }
  };
}

function _wireEndButton(runId) {
  const btn = document.getElementById('btn-end');
  if (!btn) return;

  let _pendingConfirm = false;
  let _confirmTimer   = null;

  btn.onclick = async () => {
    if (!_pendingConfirm) {
      // First click — ask for confirmation.
      _pendingConfirm = true;
      btn.innerHTML = '<i class="ti ti-alert-triangle" aria-hidden="true"></i> confirm end?';
      // Auto-cancel after 5 s if no second click.
      _confirmTimer = setTimeout(() => {
        _pendingConfirm = false;
        btn.innerHTML = '<i class="ti ti-x" aria-hidden="true"></i> end';
      }, 5000);
    } else {
      // Second click — proceed.
      clearTimeout(_confirmTimer);
      _pendingConfirm = false;
      btn.disabled = true;
      btn.innerHTML = '<i class="ti ti-loader-2" aria-hidden="true"></i> ending…';
      try {
        await fetch(`/debates/${runId}/end`, { method: 'POST' });
        // The runner will finish the current turn, call the moderator with
        // closure_reason="user_requested_end", run the synthesiser, then close.
        // The SSE stream delivers all of this normally — nothing to do here.
      } catch (e) {
        console.error('end debate failed', e);
        btn.disabled = false;
        btn.innerHTML = '<i class="ti ti-x" aria-hidden="true"></i> end';
      }
    }
  };
}

function _wireOverridePanel(runId) {
  const btn       = document.getElementById('btn-override');
  const panel     = document.getElementById('override-panel');
  const cancelBtn = document.getElementById('btn-override-cancel');
  const applyBtn  = document.getElementById('btn-override-apply');
  const input     = document.getElementById('override-budget-input');
  if (!btn || !panel) return;

  btn.onclick = () => {
    const open = panel.style.display !== 'none';
    panel.style.display = open ? 'none' : 'block';
    if (!open && debateCfg.token_budget) {
      const current = _effectiveBudget || debateCfg.token_budget;
      const el = document.getElementById('override-current-budget');
      if (el) el.textContent = Number(current).toLocaleString();
      if (input) input.value = current;
    }
  };

  if (cancelBtn) cancelBtn.onclick = () => { panel.style.display = 'none'; };

  panel.querySelectorAll('.override-quick-btn').forEach(qb => {
    qb.onclick = () => {
      const delta = parseInt(qb.dataset.delta, 10);
      const base  = parseInt(input?.value || _effectiveBudget || debateCfg.token_budget || 40000, 10);
      if (input) input.value = base + delta;
    };
  });

  if (applyBtn) {
    applyBtn.onclick = async () => {
      const newBudget = parseInt(input?.value, 10);
      if (!newBudget || newBudget < 1000) return;
      try {
        await fetch(`/debates/${runId}/override`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token_budget: newBudget }),
        });
        panel.style.display = 'none';
      } catch (e) { console.error('override failed', e); }
    };
  }
}

function _renderDebateChips(cfg) {
  const container = document.getElementById('dh-chips');
  if (!container) return;
  container.innerHTML = '';

  const chip = (text, highlight = false) => {
    const el = document.createElement('span');
    el.className = 'dh-chip' + (highlight ? ' dh-chip-highlight' : '');
    el.textContent = text;
    return el;
  };

  const steelman = cfg.steelman_mode ?? false;
  container.appendChild(chip(steelman ? 'rapoport / steelman ✓' : 'standard mode', steelman));

  const propModel = cfg.proposition_model || '';
  const oppModel  = cfg.opposition_model  || '';
  const propNick  = cfg.proposition_nickname || 'proposition';
  const oppNick   = cfg.opposition_nickname  || 'opposition';
  if (propModel) container.appendChild(chip(`${propNick}: ${propModel}`));
  if (oppModel)  container.appendChild(chip(`${oppNick}: ${oppModel}`));

  const budget = cfg.token_budget;
  if (budget) container.appendChild(chip(`${Math.round(budget / 1000)}k tokens`));
  if (cfg.min_challenges) container.appendChild(chip(`min ${cfg.min_challenges} challenges`));
}
