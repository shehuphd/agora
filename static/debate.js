// debate.js — live debate screen: SSE streaming, token tracking, rerun wiring.

import {
  esc, formatTokens, triggerDownload,
  appendActBubble, appendIntroBubble, appendErrorBubble,
  showThinkingBubble, removeThinkingBubble, markDebateClosed,
  renderTokenStrip, updateTerminationTracker,
} from './render.js';

// Module-level state for the active debate session.
let activeSSE  = null;
let debateTok  = { total: 0, proposition: 0, opposition: 0, moderator: 0 };
let debateCfg  = {};

export async function loadDebate(sessionId) {
  debateTok = { total: 0, proposition: 0, opposition: 0, moderator: 0 };
  debateCfg = {};
  document.getElementById('act-feed').innerHTML = '';
  document.getElementById('dh-title').textContent = 'loading...';
  document.getElementById('dh-id').textContent    = sessionId;
  ['tok-total', 'tok-prop', 'tok-opp', 'tok-mod'].forEach(id => {
    document.getElementById(id).textContent = '0';
  });

  const rerunBtn = document.getElementById('btn-rerun');
  if (rerunBtn) { rerunBtn.style.display = 'none'; rerunBtn.onclick = null; }

  const exportBtn = document.getElementById('btn-export-debate');
  if (exportBtn) {
    exportBtn.onclick = async () => {
      const res = await fetch(`/debates/${sessionId}/export`);
      triggerDownload(await res.blob(), res.headers.get('Content-Disposition'));
    };
  }

  try {
    const res = await fetch(`/debates/${sessionId}`);
    if (res.ok) {
      const data = await res.json();
      document.getElementById('dh-title').textContent = data.debate_title || data.topic || sessionId;
      debateCfg = data.config || {};
      (data.acts || []).forEach(act => {
        appendActBubble(act);
        _accumulateTokens(act);
      });
      renderTokenStrip(debateTok, debateCfg.token_budget || 100_000);
      _wireRerunButton(debateCfg);
      if (data.status === 'closed') {
        markDebateClosed();
        return;
      }
    }
  } catch (e) { console.warn('debate state load failed', e); }

  _openSSE(sessionId);
}

function _wireRerunButton(cfg) {
  const btn = document.getElementById('btn-rerun');
  if (!btn) return;
  btn.onclick = () => {
    window.location.hash = '#/new';
    setTimeout(() => _prefillNewForm(cfg), 50);
  };
}

function _prefillNewForm(cfg) {
  const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
  set('topic',         cfg.topic);
  set('debate-title',  '');
  set('prop-model',    cfg.proposition_model);
  set('opp-model',     cfg.opposition_model);
  set('mod-model',     cfg.moderator_model);
  set('prop-nickname', cfg.proposition_nickname);
  set('opp-nickname',  cfg.opposition_nickname);
  const setSlider = (id, displayId, val) => {
    const el = document.getElementById(id);
    const disp = document.getElementById(displayId);
    if (el && val != null) { el.value = val; if (disp) disp.textContent = val; }
  };
  setSlider('temp-prop',      'temp-prop-val',      cfg.temperature_proposition);
  setSlider('temp-opp',       'temp-opp-val',       cfg.temperature_opposition);
  setSlider('temp-mod',       'temp-mod-val',       cfg.temperature_moderator);
  setSlider('aggression',     'aggression-val',     cfg.aggression);
  setSlider('max-turns',      'max-turns-val',      cfg.max_turns);
  setSlider('token-budget',   'token-budget-val',   cfg.token_budget);
  setSlider('min-challenges', 'min-challenges-val', cfg.min_challenges);
}

function _openSSE(sessionId) {
  if (activeSSE) { activeSSE.close(); activeSSE = null; }

  activeSSE = new EventSource(`/debates/${sessionId}/stream`);

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

    // Real act
    const act = msg;
    removeThinkingBubble();
    appendActBubble(act);
    _accumulateTokens(act);
    renderTokenStrip(debateTok, debateCfg.token_budget || 100_000);

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

  activeSSE.onerror = () => {
    removeThinkingBubble();
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
    markDebateClosed();
  };
}

function _accumulateTokens(act) {
  const delta = (act.input_tokens || 0) + (act.output_tokens || 0);
  debateTok.total += delta;
  if (act.agent_role === 'proposition') debateTok.proposition += delta;
  if (act.agent_role === 'opposition')  debateTok.opposition  += delta;
  if (act.agent_role === 'moderator')   debateTok.moderator   += delta;
}
