// app.js — SPA entry point: routing, nav, new/confirm/settings screens.
// Hash-based routing. ES modules — no bundler required.

import { loadHistory, setHistoryPageSize }  from './history.js';
import { loadDebate }   from './debate.js';
import { loadExperiments } from './experiments.js';
import { loadTraces } from './traces.js';
import { esc, formatTokens } from './render.js';
import { launchOnboarding, maybeAutoLaunch } from './onboarding.js';

// ============================================================
// ROUTING
// ============================================================

const ROUTES = {
  '#/history':     { screen: 'screen-history',     load: loadHistory },
  '#/new':         { screen: 'screen-new',         load: loadNew },
  '#/confirm':     { screen: 'screen-confirm',     load: loadConfirm },
  '#/experiments': { screen: 'screen-experiments', load: loadExperiments },
  '#/traces':      { screen: 'screen-traces',      load: loadTraces },
  '#/settings':    { screen: 'screen-settings',    load: loadSettings },
};

function route() {
  const hash = window.location.hash || '#/history';
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('on'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('on'));

  if (hash.startsWith('#/debate/')) {
    const runId = hash.replace('#/debate/', '');
    document.getElementById('screen-debate').classList.add('on');
    loadDebate(runId);
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
  initTheme();
  _fetchAvailableModels().then(() => route());
  loadNavTokenTotal();
  maybeAutoLaunch();   // first-run setup wizard when no LLM key is configured
});

// ============================================================
// THEME TOGGLE
// ============================================================

function _applyThemeIcon(btn, theme) {
  if (btn) btn.innerHTML = theme === 'dark'
    ? '<i class="ti ti-sun" aria-hidden="true"></i>'
    : '<i class="ti ti-moon" aria-hidden="true"></i>';
}

function initTheme() {
  const saved = localStorage.getItem('agora-theme') || 'light';
  document.documentElement.dataset.theme = saved;
  const btn = document.getElementById('btn-theme-toggle');
  _applyThemeIcon(btn, saved);
  if (btn) btn.onclick = _toggleTheme;
}

function _toggleTheme() {
  const isDark = document.documentElement.dataset.theme === 'dark';
  const next = isDark ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('agora-theme', next);
  _applyThemeIcon(document.getElementById('btn-theme-toggle'), next);
}

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
    const keyInfo    = data.key_info || {};
    const agentsCfg  = data.config?.agents || {};
    const hasAnyValid = Object.values(keyInfo).some(i => i.valid);
    const invalidProviders = Object.entries(keyInfo)
      .filter(([, i]) => i.present && !i.valid).map(([p]) => p);
    const mismatchedRoles = [
      ['proposition', agentsCfg.proposition?.model],
      ['opposition',  agentsCfg.opposition?.model],
      ['moderator',   agentsCfg.moderator?.model],
    ].filter(([, m]) => { const p = _providerForModel(m); return p && !keyInfo[p]?.valid; })
     .map(([role]) => role);

    const banner     = document.getElementById('key-banner');
    const bannerMsg  = document.getElementById('key-banner-msg');
    const bannerLink = document.getElementById('key-banner-link');
    if (banner && bannerMsg && bannerLink) {
      if (!hasAnyValid) {
        bannerMsg.textContent  = 'No working API key found — add at least one to start debates.';
        bannerLink.textContent = 'add a key in settings';
        bannerLink.href        = '#/settings';
        banner.style.display   = 'flex';
      } else if (invalidProviders.length > 0) {
        const names  = invalidProviders.map(p => p.toUpperCase() + '_API_KEY');
        const plural = names.length > 1;
        bannerMsg.textContent  = `${names.join(' and ')} ${plural ? 'are' : 'is'} invalid — edit or remove ${plural ? 'them' : 'it'}.`;
        bannerLink.textContent = 'go to settings';
        bannerLink.href        = '#/settings';
        banner.style.display   = 'flex';
      } else if (mismatchedRoles.length > 0) {
        bannerMsg.textContent  = `The model assigned to ${mismatchedRoles.join(', ')} has no valid API key.`;
        bannerLink.textContent = 'change model defaults';
        bannerLink.href        = '#/settings';
        banner.style.display   = 'flex';
      } else {
        banner.style.display   = 'none';
      }
    }
  } catch (e) {
    console.warn('nav token load failed', e);
  }
}

// ============================================================
// AVAILABLE MODELS — fetched from /api/models (DB-backed, key is source of truth)
// ============================================================

const _PROP_NAMES = ['Thesis', 'Advocate', 'Prometheus', 'Affirmo', 'Proponent', 'Vindicator', 'Herald', 'Axiom', 'Credo', 'Euclid'];
const _OPP_NAMES  = ['Antithesis', 'Skeptic', 'Dissenter', 'Refutare', 'Critic', 'Adversario', 'Socrates', 'Diogenes', 'Nullius', 'Rebuttal'];
const _MOD_NAMES  = ['Arbiter', 'Logos', 'Themis', 'Referee', 'Impartial', 'Mentor', 'Nexus', 'Criterion', 'Quorum', 'Verity'];

function _pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

// Module-level cache — populated on load and after any key test.
// Separates provider from model id inside a <select> option value. Provider
// names are plain identifiers, so this never appears in one; model ids may
// contain ':' and '/', which is why neither is used here. Split on the first
// occurrence only.
const MODEL_KEY_SEP = '|';

function splitModelKey(key) {
  const i = String(key ?? '').indexOf(MODEL_KEY_SEP);
  return i === -1
    ? { provider: null, model: key || '' }
    : { provider: key.slice(0, i), model: key.slice(i + 1) };
}

let _availableModels = [];

async function _fetchAvailableModels() {
  try {
    const res = await fetch('/api/models');
    if (!res.ok) return;
    const data = await res.json();
    _availableModels = (data.models || []).map(m => ({
      value:    m.model_id,
      // Identifies the registry row, not just the model. Two providers can
      // serve one model id, and a <select> option carrying only the id makes
      // those two choices indistinguishable once the form is submitted.
      // Purely a DOM transport encoding — it is split back into separate
      // model and provider fields before anything is sent or stored.
      key:      `${m.provider}${MODEL_KEY_SEP}${m.model_id}`,
      label:    `${m.provider} ${m.display_name || m.model_id}`,
      provider: m.provider,
    }));
    window._knownModels = new Set(_availableModels.map(m => m.value));
  } catch (e) {
    console.warn('model list fetch failed', e);
  }
}

// Which provider serves this model, per the registry — the only thing that
// knows, since each row was written by the adapter that enumerated it.
// Returns null when unregistered rather than guessing from the name: callers
// use this to name the key at fault, and naming the wrong one is worse than
// staying quiet.
function _providerForModel(model) {
  if (!model) return null;
  return _availableModels.find(m => m.value === model)?.provider ?? null;
}

// Exposed so debate.js can check validity without importing app.js (avoids circular dep).
// Populated once models are fetched; starts empty so no model passes pre-flight before load.
window._knownModels = new Set();

// allowEmpty: if true, prepend a blank option (value="") and don't auto-select.
// blankLabel: text for the blank option (default: "— select a model —").
function _buildModelSelect(sel, keyStatus, selectedValue, allowEmpty = false,
                           blankLabel = '— select a model —', selectedProvider = null) {
  sel.innerHTML = '';
  if (!_availableModels.length) {
    const opt = document.createElement('option');
    opt.disabled = true;
    opt.textContent = 'no models available — test your API keys in Settings';
    sel.appendChild(opt);
    return;
  }
  if (allowEmpty) {
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = blankLabel;
    sel.appendChild(blank);
  }
  const providers = [...new Set(_availableModels.map(m => m.provider))];
  providers.forEach(provider => {
    const models = _availableModels.filter(m => m.provider === provider);
    const grp = document.createElement('optgroup');
    grp.label = provider;
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.key;
      opt.textContent = keyStatus[m.provider] ? m.label : `${m.label} (key missing)`;
      opt.disabled = !keyStatus[m.provider];
      grp.appendChild(opt);
    });
    sel.appendChild(grp);
  });
  if (selectedValue) {
    // selectedValue may be a full "provider|model" key or a bare model id from
    // an older config. Try the exact key, then the provider-qualified key when
    // one was supplied, then any provider serving that model, then a prefix
    // match for short ids against versioned ones in the registry.
    const want = splitModelKey(selectedValue);
    const provider = selectedProvider || want.provider;
    const opts = [...sel.options];
    const byModel = o => splitModelKey(o.value).model;
    const match =
      opts.find(o => o.value === selectedValue) ||
      (provider && opts.find(o => o.value === `${provider}${MODEL_KEY_SEP}${want.model}`)) ||
      opts.find(o => byModel(o) === want.model) ||
      opts.find(o => byModel(o).startsWith(want.model));
    if (match) sel.value = match.value;
  }
  // Without allowEmpty, auto-select the first enabled option so the field is never blank.
  if (!sel.value && !allowEmpty) {
    const first = [...sel.options].find(o => !o.disabled);
    if (first) sel.value = first.value;
  }
}

// ============================================================
// SCREEN 2: NEW DEBATE
// ============================================================

function _prefillFromPending(cfg) {
  // Text inputs / selects — all have id attributes
  const _set = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null) el.value = val;
  };
  _set('topic',           cfg.topic);
  _set('experiment-name', cfg.experiment_name);
  _set('prop-model',      cfg.prop_model);
  _set('opp-model',       cfg.opp_model);
  _set('mod-model',       cfg.mod_model);
  _set('prop-nickname',   cfg.prop_nickname);
  _set('opp-nickname',    cfg.opp_nickname);
  _set('mod-nickname',    cfg.mod_nickname);

  // Update nickname preview labels
  if (cfg.prop_nickname) { const el = document.getElementById('prop-name-preview'); if (el) el.textContent = cfg.prop_nickname; }
  if (cfg.opp_nickname)  { const el = document.getElementById('opp-name-preview');  if (el) el.textContent = cfg.opp_nickname; }
  if (cfg.mod_nickname)  { const el = document.getElementById('mod-name-preview');  if (el) el.textContent = cfg.mod_nickname; }

  // Temperature / aggression sliders — inputs use name= (no id), displays have id
  // pendingDebate stores floats (0.0–1.0); sliders are 0–10 integers
  const _setTempSlider = (name, displayId, floatVal) => {
    if (floatVal == null) return;
    const v = Math.round(floatVal * 10);
    const input = document.querySelector(`input[name="${name}"]`);
    const disp  = document.getElementById(displayId);
    if (input) input.value = v;
    if (disp)  disp.textContent = (v / 10).toFixed(1);
  };
  _setTempSlider('prop_temperature', 'prop-temp-out', cfg.prop_temperature);
  _setTempSlider('opp_temperature',  'opp-temp-out',  cfg.opp_temperature);
  _setTempSlider('opp_aggression',   'opp-agg-out',   cfg.opp_aggression);

  // Threshold sliders — inputs use name=; display is input.nextElementSibling
  const _setThresh = (name, val, fmt) => {
    if (val == null) return;
    const input = document.querySelector(`input[name="${name}"]`);
    if (!input) return;
    input.value = val;
    const disp = input.nextElementSibling;
    if (disp) disp.textContent = fmt(val);
  };
  _setThresh('max_turns',      cfg.max_turns,                          v => v);
  _setThresh('max_time',       cfg.max_time_minutes,                   v => v);
  _setThresh('token_budget',   Math.round((cfg.token_budget || 0) / 1000), v => v + 'k');
  _setThresh('min_challenges', cfg.min_challenges,                     v => v);
  _setThresh('min_concessions',cfg.min_concessions,                    v => v);
  _setThresh('rep_tolerance',  cfg.repetition_tolerance,               v => v);

  // Toggles
  const _setToggle = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle('on', !!value);
    el.setAttribute('aria-checked', String(!!value));
  };
  _setToggle('toggle-steelman',        cfg.require_steelman);
  _setToggle('toggle-full-resolution', cfg.require_full_resolution);
  _setToggle('toggle-auto-title',      cfg.auto_generate_title ?? true);
}

async function loadNew() {
  // Warn when retrieval would fall back to token-billed vendor search.
  fetch('/api/search-status')
    .then(r => r.ok ? r.json() : null)
    .then(s => {
      const el = document.getElementById('search-tier-warning');
      if (el && s) el.style.display = s.neutral ? 'none' : 'flex';
    })
    .catch(() => {});

  let keyStatus = {};
  let agentCfg  = {};
  try {
    const res = await fetch('/settings');
    if (res.ok) {
      const data = await res.json();
      keyStatus = data.key_status || {};
      agentCfg  = data.config?.agents || {};
    }
  } catch (e) { /* defaults — all disabled */ }

  // Populate experiment autocomplete datalist
  try {
    const expRes = await fetch('/experiments');
    if (expRes.ok) {
      const experiments = await expRes.json();
      const dl = document.getElementById('experiment-list');
      if (dl) {
        experiments.forEach(e => {
          const opt = document.createElement('option');
          opt.value = e.name;
          dl.appendChild(opt);
        });
      }
    }
  } catch (_) {}

  const _firstValid = () => _availableModels.find(m => keyStatus[m.provider])?.value || null;

  // If no working key exists at all, block the form with a clear prompt.
  if (!_firstValid()) {
    const form = document.getElementById('new-debate-form');
    if (form) {
      form.style.display = 'none';
      const block = document.createElement('div');
      block.className = 'no-key-block';
      block.innerHTML = '<p>No working API key found. <a href="#/settings">Add at least one key in Settings</a> to start debates.</p>';
      form.parentElement?.insertBefore(block, form);
    }
    return;
  }

  // Resolve a configured model preference to an available+valid option, or cycle to first valid.
  const _resolveModel = (configModel) => {
    if (!configModel) return _firstValid();
    const exact = _availableModels.find(m => m.value === configModel && keyStatus[m.provider]);
    if (exact) return exact.value;
    const prefix = _availableModels.find(m => m.value.startsWith(configModel) && keyStatus[m.provider]);
    if (prefix) return prefix.value;
    return _firstValid();
  };

  // Use allowEmpty=true so no model is silently auto-selected.
  // Saved settings preferences pre-fill the picker; absent preferences show "— use first available —".
  const DEFAULTS = {
    'prop-model': agentCfg.proposition?.model ? _resolveModel(agentCfg.proposition.model) : null,
    'opp-model':  agentCfg.opposition?.model  ? _resolveModel(agentCfg.opposition.model)  : null,
    'mod-model':  agentCfg.moderator?.model   ? _resolveModel(agentCfg.moderator.model)   : null,
  };

  const submitBtn = document.getElementById('btn-new-submit');
  const topicEl   = document.getElementById('topic');
  const _updateSubmitBtn = () => {
    const topicOk  = !!topicEl?.value.trim();
    const modelsOk = ['prop-model', 'opp-model', 'mod-model'].every(
      id => document.getElementById(id)?.value
    );
    submitBtn?.classList.toggle('incomplete', !(topicOk && modelsOk));
  };

  topicEl?.addEventListener('input', _updateSubmitBtn);

  ['prop-model', 'opp-model', 'mod-model'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    _buildModelSelect(sel, keyStatus, DEFAULTS[id], true);
    sel.addEventListener('change', _updateSubmitBtn);
  });
  _updateSubmitBtn();
  // Re-check after a tick to catch browser-restored textarea values (Chrome restores
  // form values after JS runs, with no input event).
  setTimeout(_updateSubmitBtn, 150);

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
          document.getElementById('topic').dispatchEvent(new Event('input'));
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

    // The selects carry "provider|model" so the choice of vendor survives the
    // form. Split it back apart here: the wire format stays structured, and
    // nothing downstream ever parses a model id.
    for (const field of ['prop_model', 'opp_model', 'mod_model', 'synth_model']) {
      if (!cfg[field]) continue;
      const { provider, model } = splitModelKey(cfg[field]);
      cfg[field] = model;
      if (provider) cfg[field.replace('_model', '_provider')] = provider;
    }

    // Flash all missing required fields and abort. This runs even when the button looks incomplete
    // so clicking always gives the user visual feedback about exactly what's missing.
    const _flashEl = (el, eventName = 'input') => {
      el.classList.remove('field-error');
      void el.offsetWidth; // force reflow so animation restarts each click
      el.classList.add('field-error');
      el.addEventListener(eventName, () => el.classList.remove('field-error'), { once: true });
    };
    const missingEls = [];
    if (!cfg.topic?.trim()) {
      const el = document.getElementById('topic');
      if (el) { _flashEl(el, 'input'); missingEls.push(el); }
    }
    for (const [field] of [['prop_model'], ['opp_model'], ['mod_model']]) {
      if (!cfg[field]) {
        const sel = document.querySelector(`select[name="${field}"]`);
        if (sel) { _flashEl(sel, 'change'); missingEls.push(sel); }
      }
    }
    if (missingEls.length) {
      missingEls[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }

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

  // Prefill from pendingDebate when navigating back from the confirm screen.
  // The rerun button stores config directly in sessionStorage and skips this form,
  // but if the user clicks "go back" from confirm we restore their settings here.
  const _raw = sessionStorage.getItem('pendingDebate');
  if (_raw) {
    try { _prefillFromPending(JSON.parse(_raw)); _updateSubmitBtn(); } catch (_) {}
  }
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
  const expRow = document.getElementById('confirm-experiment-row');
  if (expRow) {
    const name = (cfg.experiment_name || '').trim();
    expRow.style.display = name ? '' : 'none';
    if (name) document.getElementById('confirm-experiment').textContent = name;
  }

  // Pre-flight: check that all selected models are still in the known-working list.
  const _badConfirmModels = [
    { role: 'proposition', model: cfg.prop_model },
    { role: 'opposition',  model: cfg.opp_model  },
    { role: 'moderator',   model: cfg.mod_model  },
  ].filter(r => r.model && !window._knownModels.has(r.model));

  const startBtn = document.getElementById('confirm-start-btn');
  if (_badConfirmModels.length) {
    const names = _badConfirmModels.map(b => `${b.role} (${b.model})`).join(', ');
    const warn = document.createElement('p');
    warn.className = 'confirm-model-warn';
    warn.innerHTML = `Model no longer available: ${esc(names)}. <a href="#/settings">Go to Settings →</a>`;
    startBtn.parentElement.insertBefore(warn, startBtn);
    startBtn.disabled = true;
    return;
  }

  startBtn.onclick = async () => {
    startBtn.disabled = true;
    startBtn.textContent = 'starting...';
    try {
      const res = await fetch('/debates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      sessionStorage.removeItem('pendingDebate');
      window.location.hash = `#/debate/${data.run_id}`;
    } catch (e) {
      startBtn.disabled = false;
      startBtn.innerHTML = '<i class="ti ti-player-play"></i> retry';
      console.error('start debate failed:', e);
    }
  };
}

// ============================================================
// SCREEN 5: SETTINGS
// ============================================================

async function loadSettings() {
  const container = document.getElementById('api-key-status');
  container.innerHTML = '<p class="keys-verifying"><i class="ti ti-loader-2 spin" aria-hidden="true"></i> verifying API keys…</p>';
  try {
    const res = await fetch('/settings');
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();

    container.innerHTML = '';
    // Sent by /settings, derived from the registered adapters — a new provider
    // appears here without the frontend being edited.
    const KEY_MAP = data.key_envs || {};
    const warnings = data.key_warnings || {};

    Object.entries(KEY_MAP).forEach(([provider, envName]) => {
      const info = data.key_info?.[provider] || { present: false, valid: false, error: null };
      const warn = warnings[provider];

      const row = document.createElement('div');
      row.className = 'key-row';
      row.dataset.provider = provider;

      let statusHtml;
      if (info.valid) {
        statusHtml = `<span class="key-status-ok"><i class="ti ti-check" aria-hidden="true"></i> valid</span>`;
      } else if (info.present) {
        const fullErr = info.error || 'check your key';
        const shortErr = fullErr.length > 50 ? fullErr.slice(0, 50) + '…' : fullErr;
        statusHtml = `<span class="key-status-invalid"><i class="ti ti-x" aria-hidden="true"></i> invalid</span>`
                   + `<span class="key-status-error" title="${esc(fullErr)}">${esc(shortErr)}</span>`;
      } else {
        statusHtml = `<span class="key-status-missing"><i class="ti ti-minus" aria-hidden="true"></i> missing</span>`;
      }

      const warnHtml = warn
        ? `<span class="key-warn" title="Quota or credit exhausted on ${new Date(warn).toLocaleString()}">
             <i class="ti ti-alert-triangle" aria-hidden="true"></i> quota exceeded
           </span>`
        : '';

      const modelCount = _availableModels.filter(m => m.provider === provider).length;
      // Serper is a search key, not an LLM key — no model count to report.
      const usedByHtml = provider === 'serper'
        ? (info.valid ? `<span class="key-used-by">web search now enabled</span>` : '')
        : (info.valid && modelCount > 0)
          ? `<span class="key-used-by">${modelCount} model${modelCount !== 1 ? 's' : ''} available</span>`
          : '';

      row.innerHTML = `
        <span class="key-name">${esc(envName)}</span>
        <span class="key-status-group">${statusHtml}${warnHtml}</span>
        ${usedByHtml}
        <div class="key-actions">
          <button class="btn-ghost key-test-btn" style="font-size:11px">
            <i class="ti ti-refresh" aria-hidden="true"></i> test
          </button>
          <button class="btn-ghost key-edit-btn" data-provider="${esc(provider)}" style="font-size:11px">
            <i class="ti ti-pencil" aria-hidden="true"></i> edit
          </button>
        </div>
      `;

      // Inline edit form, hidden by default.
      const editForm = document.createElement('div');
      editForm.className = 'key-edit-form';
      editForm.style.display = 'none';
      editForm.innerHTML = `
        <input type="password" class="key-edit-input" placeholder="paste new key…" autocomplete="off" style="flex:1;font-size:12px;font-family:monospace">
        <button class="btn-primary key-save-btn" style="font-size:11px">
          <i class="ti ti-device-floppy" aria-hidden="true"></i> save
        </button>
        <button class="btn-ghost key-cancel-btn" style="font-size:11px">cancel</button>
      `;
      container.appendChild(row);
      container.appendChild(editForm);

      // Wire test button
      row.querySelector('.key-test-btn').onclick = async () => {
        const testBtn = row.querySelector('.key-test-btn');
        const statusGroup = row.querySelector('.key-status-group');
        testBtn.disabled = true;
        testBtn.innerHTML = '<i class="ti ti-loader-2 spin" aria-hidden="true"></i>';
        let validResult = false;
        try {
          const r = await fetch(`/settings/keys/${provider}/test`, { method: 'POST' });
          if (!r.ok) throw new Error(await r.text());
          const info = await r.json();
          let newStatusHtml;
          if (info.valid) {
            validResult = true;
            newStatusHtml = `<span class="key-status-ok"><i class="ti ti-check" aria-hidden="true"></i> valid</span>`;
          } else if (info.present) {
            const fullErr = info.error || 'check your key';
            const shortErr = fullErr.length > 50 ? fullErr.slice(0, 50) + '…' : fullErr;
            newStatusHtml = `<span class="key-status-invalid"><i class="ti ti-x" aria-hidden="true"></i> invalid</span>`
                         + `<span class="key-status-error" title="${esc(fullErr)}">${esc(shortErr)}</span>`;
          } else {
            newStatusHtml = `<span class="key-status-missing"><i class="ti ti-minus" aria-hidden="true"></i> missing</span>`;
          }
          statusGroup.innerHTML = newStatusHtml;
          // Refresh the model list so the picker reflects the newly-tested key.
          if (validResult) await _fetchAvailableModels();
        } catch (err) {
          console.error('key test failed:', err);
        } finally {
          if (validResult) {
            testBtn.innerHTML = '<i class="ti ti-check" style="color:var(--text-success)" aria-hidden="true"></i>';
            setTimeout(() => {
              testBtn.disabled = false;
              testBtn.innerHTML = '<i class="ti ti-refresh" aria-hidden="true"></i> test';
            }, 1200);
          } else {
            testBtn.disabled = false;
            testBtn.innerHTML = '<i class="ti ti-refresh" aria-hidden="true"></i> test';
          }
        }
      };

      // Wire toggle
      row.querySelector('.key-edit-btn').onclick = () => {
        editForm.style.display = editForm.style.display === 'none' ? 'flex' : 'none';
        if (editForm.style.display === 'flex') editForm.querySelector('.key-edit-input').focus();
      };
      editForm.querySelector('.key-cancel-btn').onclick = () => { editForm.style.display = 'none'; };
      editForm.querySelector('.key-save-btn').onclick = async () => {
        const val  = editForm.querySelector('.key-edit-input').value.trim();
        const btn  = editForm.querySelector('.key-save-btn');
        btn.disabled = true;
        btn.textContent = 'saving…';
        try {
          const r = await fetch('/settings/keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, value: val }),
          });
          if (!r.ok) throw new Error(await r.text());
          editForm.style.display = 'none';
          loadSettings();   // refresh key status + warnings
        } catch (err) {
          btn.textContent = 'error — retry';
          console.error('key save failed:', err);
        } finally {
          btn.disabled = false;
        }
      };
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

    if (data.config?.protocol?.require_steelman) {
      document.getElementById('s-toggle-steelman').classList.add('on');
      document.getElementById('s-toggle-steelman').setAttribute('aria-checked', 'true');
    }

    // Populate agent model selects.
    const agentCfg   = data.config?.agents || {};
    const keyStatus2 = data.key_status || {};
    const MODEL_ROLE_MAP = {
      's-prop-model':  agentCfg.proposition?.model  || null,
      's-opp-model':   agentCfg.opposition?.model   || null,
      's-mod-model':   agentCfg.moderator?.model    || null,
      's-synth-model': agentCfg.synthesiser?.model  || null,
    };
    Object.entries(MODEL_ROLE_MAP).forEach(([selId, defaultModel]) => {
      const sel = document.getElementById(selId);
      if (!sel) return;
      _buildModelSelect(sel, keyStatus2, defaultModel, true, '— no preference —');
    });

    // Responses API mode selector.
    const responsesModeSel = document.getElementById('s-openai-responses-mode');
    if (responsesModeSel) responsesModeSel.value = data.config?.openai?.responses_mode || 'auto';

    const hw = data.config?.agent_settings?.history_window;
    if (hw != null) {
      const hwEl = document.getElementById('s-history-window');
      if (hwEl) {
        hwEl.value = hw;
        const hwVal = hwEl.nextElementSibling;
        if (hwVal) hwVal.textContent = hw;
      }
    }

    const ce = data.config?.agent_settings?.chapter_every;
    if (ce != null) {
      const ceEl = document.getElementById('s-chapter-every');
      if (ceEl) {
        ceEl.value = ce;
        const ceVal = ceEl.nextElementSibling;
        if (ceVal) ceVal.textContent = ce == 0 ? 'off' : ce;
      }
    }

    const ps = data.config?.ui?.history_page_size;
    if (ps != null) {
      const psEl = document.getElementById('s-history-page-size');
      if (psEl) psEl.value = String(ps);
      setHistoryPageSize(ps);
    }

  } catch (e) { console.error('settings load failed', e); }

  const obBtn = document.getElementById('btn-launch-onboarding');
  if (obBtn) obBtn.onclick = () => launchOnboarding();

  document.getElementById('btn-reset-tokens').onclick = async () => {
    await fetch('/settings/reset-tokens', { method: 'POST' });
    loadSettings();
    loadNavTokenTotal();
  };

  document.getElementById('btn-save-settings').onclick = async () => {
    const btn = document.getElementById('btn-save-settings');
    btn.disabled = true;
    btn.innerHTML = '<i class="ti ti-loader-2" aria-hidden="true"></i> saving…';
    const payload = {
      protocol: {
        max_turns:            parseInt(document.getElementById('s-max-turns').value),
        max_time_minutes:     parseInt(document.getElementById('s-max-time').value),
        token_budget:         parseInt(document.getElementById('s-token-budget').value) * 1000,
        min_challenges:       parseInt(document.getElementById('s-min-challenges').value),
        min_concessions:      parseInt(document.getElementById('s-min-concessions').value),
        repetition_tolerance: parseInt(document.getElementById('s-rep-tolerance').value),
        require_steelman:     document.getElementById('s-toggle-steelman').classList.contains('on'),
      },
      agent_settings: {
        history_window: parseInt(document.getElementById('s-history-window').value),
        chapter_every:  parseInt(document.getElementById('s-chapter-every')?.value || '10'),
      },
      ui: {
        history_page_size: parseInt(document.getElementById('s-history-page-size')?.value || '50'),
      },
      agents: {
        proposition: { model: document.getElementById('s-prop-model')?.value || null },
        opposition:  { model: document.getElementById('s-opp-model')?.value  || null },
        moderator:   { model: document.getElementById('s-mod-model')?.value  || null },
        synthesiser: { model: document.getElementById('s-synth-model')?.value || null },
      },
      openai: {
        responses_mode: document.getElementById('s-openai-responses-mode')?.value || 'auto',
      },
    };
    await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    window.location.reload();
  };

  document.getElementById('btn-reset-defaults').onclick = async () => {
    const btn = document.getElementById('btn-reset-defaults');
    btn.disabled = true;
    btn.innerHTML = '<i class="ti ti-loader-2" aria-hidden="true"></i> resetting…';
    await fetch('/settings/reset-defaults', { method: 'POST' });
    window.location.reload();
  };
}
