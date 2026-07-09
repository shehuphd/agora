// app.js — SPA entry point: routing, nav, new/confirm/settings screens.
// Hash-based routing. ES modules — no bundler required.

import { loadHistory }  from './history.js';
import { loadDebate }   from './debate.js';
import { esc, formatTokens } from './render.js';

// ============================================================
// ROUTING
// ============================================================

const ROUTES = {
  '#/history':  { screen: 'screen-history',  load: loadHistory },
  '#/new':      { screen: 'screen-new',      load: loadNew },
  '#/confirm':  { screen: 'screen-confirm',  load: loadConfirm },
  '#/settings': { screen: 'screen-settings', load: loadSettings },
};

function route() {
  const hash = window.location.hash || '#/history';
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('on'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('on'));

  if (hash.startsWith('#/debate/')) {
    const sessionId = hash.replace('#/debate/', '');
    document.getElementById('screen-debate').classList.add('on');
    loadDebate(sessionId);
    return;
  }

  const r = ROUTES[hash];
  if (r) {
    document.getElementById(r.screen).classList.add('on');
    document.querySelector(`a[href="${hash}"]`)?.classList.add('on');
    r.load();
  } else {
    window.location.hash = '#/history';
  }
}

window.addEventListener('hashchange', route);
document.addEventListener('DOMContentLoaded', () => {
  route();
  loadNavTokenTotal();
});

// ============================================================
// HELPERS
// ============================================================

function showEnvHint(path) {
  const existing = document.getElementById('env-open-hint');
  if (existing) existing.remove();
  const hint = document.createElement('p');
  hint.id = 'env-open-hint';
  hint.style.cssText = 'font-size:11px;color:var(--text-warning);margin-top:6px;line-height:1.5';
  hint.textContent = `couldn't open automatically — navigate to: ${path}`;
  document.getElementById('env-path-display').parentElement.appendChild(hint);
}

// ============================================================
// NAV TOKEN CHIP
// ============================================================

async function loadNavTokenTotal() {
  try {
    const res = await fetch('/settings');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('nav-token-total').textContent =
      formatTokens(data.token_totals?.total || 0);
    const anyMissing = Object.values(data.key_status || {}).some(v => !v);
    const banner = document.getElementById('key-banner');
    if (banner) banner.style.display = anyMissing ? 'flex' : 'none';
  } catch (e) {
    console.warn('nav token load failed', e);
  }
}

// ============================================================
// AVAILABLE MODELS
// ============================================================

const _PROP_NAMES = ['Thesis', 'Advocate', 'Prometheus', 'Affirmo', 'Proponent', 'Vindicator', 'Herald', 'Axiom', 'Credo', 'Euclid'];
const _OPP_NAMES  = ['Antithesis', 'Skeptic', 'Dissenter', 'Refutare', 'Critic', 'Adversario', 'Socrates', 'Diogenes', 'Nullius', 'Rebuttal'];
const _MOD_NAMES  = ['Arbiter', 'Logos', 'Themis', 'Referee', 'Impartial', 'Mentor', 'Nexus', 'Criterion', 'Quorum', 'Verity'];

function _pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

const ALL_MODELS = [
  { value: 'claude-sonnet-4-6', label: 'claude sonnet 4.6', provider: 'anthropic' },
  { value: 'claude-opus-4-8',   label: 'claude opus 4.8',   provider: 'anthropic' },
  { value: 'claude-haiku-4-5',  label: 'claude haiku 4.5',  provider: 'anthropic' },
  { value: 'gpt-4o',            label: 'gpt-4o',            provider: 'openai' },
  { value: 'gpt-4o-mini',       label: 'gpt-4o mini',       provider: 'openai' },
];

// ============================================================
// SCREEN 2: NEW DEBATE
// ============================================================

async function loadNew() {
  let keyStatus = {};
  try {
    const res = await fetch('/settings');
    if (res.ok) keyStatus = (await res.json()).key_status || {};
  } catch (e) { /* defaults — all disabled */ }

  const DEFAULTS = { 'prop-model': 'claude-sonnet-4-6', 'opp-model': 'gpt-4o', 'mod-model': 'claude-opus-4-8' };

  ['prop-model', 'opp-model', 'mod-model'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '';
    ALL_MODELS.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.value;
      opt.textContent = keyStatus[m.provider] ? m.label : `${m.label} (key missing)`;
      opt.disabled = !keyStatus[m.provider];
      sel.appendChild(opt);
    });
    sel.value = DEFAULTS[id];
  });

  const anyKey = Object.values(keyStatus).some(v => v);
  const randBtn = document.getElementById('btn-random-topic');
  if (randBtn) {
    randBtn.disabled = !anyKey;
    randBtn.title = anyKey ? 'generate a random debate topic' : 'add an API key in settings first';
    randBtn.onclick = async () => {
      randBtn.disabled = true;
      const origHtml = randBtn.innerHTML;
      randBtn.innerHTML = '<i class="ti ti-loader-2" aria-hidden="true"></i> generating…';
      try {
        const r = await fetch('/api/random-topic', { method: 'POST' });
        const result = await r.json();
        if (result.ok) {
          document.getElementById('topic').value = result.topic;
          const propName = _pick(_PROP_NAMES);
          const oppName  = _pick(_OPP_NAMES);
          const modName  = _pick(_MOD_NAMES);
          const propEl = document.getElementById('prop-nickname');
          const oppEl  = document.getElementById('opp-nickname');
          const modEl  = document.getElementById('mod-nickname');
          if (propEl) { propEl.value = propName; document.getElementById('prop-name-preview').textContent = propName; }
          if (oppEl)  { oppEl.value  = oppName;  document.getElementById('opp-name-preview').textContent  = oppName; }
          if (modEl)  { modEl.value  = modName;  document.getElementById('mod-name-preview').textContent  = modName; }
        } else console.warn('random topic failed:', result.error);
      } catch (err) {
        console.warn('random topic error:', err);
      } finally {
        randBtn.innerHTML = origHtml;
        randBtn.disabled = false;
      }
    };
  }

  document.getElementById('new-debate-form').onsubmit = (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const cfg = Object.fromEntries(fd.entries());

    cfg.prop_temperature     = parseFloat(cfg.prop_temperature) / 10;
    cfg.opp_temperature      = parseFloat(cfg.opp_temperature)  / 10;
    cfg.opp_aggression       = parseFloat(cfg.opp_aggression)   / 10;
    cfg.max_turns            = parseInt(cfg.max_turns);
    cfg.max_time_minutes     = parseInt(cfg.max_time);
    cfg.token_budget         = parseInt(cfg.token_budget) * 1000;
    cfg.min_challenges       = parseInt(cfg.min_challenges);
    cfg.min_concessions      = parseInt(cfg.min_concessions);
    cfg.repetition_tolerance = parseInt(cfg.rep_tolerance);

    cfg.require_steelman        = document.getElementById('toggle-steelman').classList.contains('on');
    cfg.require_full_resolution = document.getElementById('toggle-full-resolution').classList.contains('on');
    cfg.auto_generate_title     = document.getElementById('toggle-auto-title').classList.contains('on');

    sessionStorage.setItem('pendingDebate', JSON.stringify(cfg));
    window.location.hash = '#/confirm';
  };
}

// ============================================================
// SCREEN 3: CONFIRM
// ============================================================

function loadConfirm() {
  const raw = sessionStorage.getItem('pendingDebate');
  if (!raw) { window.location.hash = '#/new'; return; }
  const cfg = JSON.parse(raw);

  document.getElementById('confirm-topic').textContent  = cfg.topic || '';
  document.getElementById('confirm-prop').textContent   = `${cfg.prop_nickname || 'Thesis'} · ${cfg.prop_model}`;
  document.getElementById('confirm-opp').textContent    = `${cfg.opp_nickname  || 'Antithesis'} · ${cfg.opp_model}`;
  document.getElementById('confirm-mod').textContent    = `${cfg.mod_nickname  || 'Arbiter'} · ${cfg.mod_model}`;
  document.getElementById('confirm-turns').textContent  = cfg.max_turns;
  document.getElementById('confirm-budget').textContent = `${Math.round(cfg.token_budget / 1000)}k tokens`;
  document.getElementById('confirm-mode').textContent   = cfg.require_steelman ? 'Rapoport (steelman required)' : 'standard';

  document.getElementById('confirm-start-btn').onclick = async () => {
    const btn = document.getElementById('confirm-start-btn');
    btn.disabled = true;
    btn.textContent = 'starting...';
    try {
      const res = await fetch('/debates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      sessionStorage.removeItem('pendingDebate');
      window.location.hash = `#/debate/${data.session_id}`;
    } catch (e) {
      btn.disabled = false;
      btn.innerHTML = '<i class="ti ti-player-play"></i> retry';
      console.error('start debate failed:', e);
    }
  };
}

// ============================================================
// SCREEN 5: SETTINGS
// ============================================================

async function loadSettings() {
  try {
    const res = await fetch('/settings');
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();

    const container = document.getElementById('api-key-status');
    container.innerHTML = '';
    const KEY_MAP = { anthropic: 'ANTHROPIC_API_KEY', openai: 'OPENAI_API_KEY', google: 'GOOGLE_API_KEY' };
    Object.entries(KEY_MAP).forEach(([provider, envName]) => {
      const ok = data.key_status?.[provider];
      const row = document.createElement('div');
      row.className = 'key-row';
      row.innerHTML = `
        <span class="key-name">${esc(envName)}</span>
        <span class="${ok ? 'key-status-ok' : 'key-status-missing'}">
          <i class="ti ${ok ? 'ti-check' : 'ti-x'}" aria-hidden="true"></i>
          ${ok ? 'present' : 'missing'}
        </span>
      `;
      container.appendChild(row);
    });

    document.getElementById('env-path-display').textContent = data.env_path || '';
    document.getElementById('env-open-link').onclick = async (e) => {
      e.preventDefault();
      const btn = document.getElementById('env-open-link');
      btn.textContent = 'opening...';
      try {
        const r = await fetch('/api/open-env', { method: 'POST' });
        const result = await r.json();
        if (result.ok) {
          const label = result.created ? 'created + opened' : 'opened';
          btn.innerHTML = `<i class="ti ti-check" aria-hidden="true"></i> ${label}`;
          setTimeout(() => { btn.innerHTML = '<i class="ti ti-folder-open" aria-hidden="true"></i> open'; }, 2500);
        } else {
          showEnvHint(result.path);
          btn.innerHTML = '<i class="ti ti-folder-open" aria-hidden="true"></i> open';
        }
      } catch (err) {
        showEnvHint(document.getElementById('env-path-display').textContent);
        btn.innerHTML = '<i class="ti ti-folder-open" aria-hidden="true"></i> open';
      }
    };

    const t = data.token_totals || {};
    document.getElementById('settings-tok-total').textContent  = formatTokens(t.total  || 0);
    document.getElementById('settings-tok-input').textContent  = formatTokens(t.input  || 0);
    document.getElementById('settings-tok-output').textContent = formatTokens(t.output || 0);

    if (data.defaults?.protocol?.require_steelman) {
      document.getElementById('s-toggle-steelman').classList.add('on');
      document.getElementById('s-toggle-steelman').setAttribute('aria-checked', 'true');
    }

  } catch (e) { console.error('settings load failed', e); }

  document.getElementById('btn-reset-tokens').onclick = async () => {
    await fetch('/settings/reset-tokens', { method: 'POST' });
    loadSettings();
    loadNavTokenTotal();
  };

  document.getElementById('btn-save-defaults').onclick = async () => {
    const payload = {
      protocol: {
        max_turns:            parseInt(document.getElementById('s-max-turns').value),
        max_time_minutes:     parseInt(document.getElementById('s-max-time').value),
        token_budget:         parseInt(document.getElementById('s-token-budget').value) * 1000,
        min_challenges:       parseInt(document.getElementById('s-min-challenges').value),
        min_concessions:      parseInt(document.getElementById('s-min-concessions').value),
        repetition_tolerance: parseInt(document.getElementById('s-rep-tolerance').value),
        require_steelman:     document.getElementById('s-toggle-steelman').classList.contains('on'),
      }
    };
    await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  };
}
