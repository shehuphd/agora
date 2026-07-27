// onboarding.js — first-launch setup wizard (also launchable from Settings)
import { esc } from './render.js';

const _STEPS = ['model keys', 'web search', 'done'];
let _step = 0;

const _LLM_PROVIDERS = [
  ['anthropic',  'ANTHROPIC_API_KEY',  'console.anthropic.com'],
  ['openai',     'OPENAI_API_KEY',     'platform.openai.com'],
  ['google',     'GOOGLE_API_KEY',     'aistudio.google.com'],
  ['perplexity', 'PERPLEXITY_API_KEY', 'perplexity.ai/settings/api'],
];

const _SEARXNG_CMDS = [
  'brew install --cask docker   # or: https://docs.docker.com/get-docker/',
  'docker run -d --name searxng -p 8888:8080 -v searxng-config:/etc/searxng searxng/searxng',
  'docker exec searxng sed -i \'s/formats: \\[]/formats: [json]/\' /etc/searxng/settings.yml && docker restart searxng',
].join('\n');

/**
 * Open the wizard when the app looks unconfigured (no valid LLM key).
 * Called once at startup; cheap no-op otherwise.
 */
export async function maybeAutoLaunch() {
  if (localStorage.getItem('agora-onboarded')) return;
  try {
    const data = await (await fetch('/settings')).json();
    const anyValid = _LLM_PROVIDERS.some(([p]) => data.key_status?.[p]);
    if (!anyValid) launchOnboarding();
    else localStorage.setItem('agora-onboarded', '1');
  } catch (_) {}
}

export function launchOnboarding() {
  _step = 0;
  let overlay = document.getElementById('onboarding-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'onboarding-overlay';
    overlay.className = 'onboarding-overlay';
    document.body.appendChild(overlay);
  }
  overlay.style.display = 'flex';
  _render(overlay);
}

function _close(overlay) {
  localStorage.setItem('agora-onboarded', '1');
  overlay.style.display = 'none';
}

async function _render(overlay) {
  const dots = _STEPS.map((s, i) =>
    `<span class="ob-dot ${i === _step ? 'ob-dot-active' : ''}" title="${esc(s)}"></span>`).join('');

  overlay.innerHTML = `
    <div class="onboarding-modal">
      <div class="ob-head">
        <span class="ob-title">welcome to agora</span>
        <div class="ob-dots">${dots}</div>
        <button class="btn-ghost btn-sm" id="ob-close"><i class="ti ti-x" aria-hidden="true"></i></button>
      </div>
      <div class="ob-body" id="ob-body"><p class="ob-loading">loading…</p></div>
      <div class="ob-foot" id="ob-foot"></div>
    </div>`;

  overlay.querySelector('#ob-close').onclick = () => _close(overlay);
  const body = overlay.querySelector('#ob-body');
  const foot = overlay.querySelector('#ob-foot');

  if (_step === 0) await _renderKeysStep(overlay, body, foot);
  else if (_step === 1) await _renderSearchStep(overlay, body, foot);
  else _renderDoneStep(overlay, body, foot);
}

// ---------------------------------------------------------------- step 1

async function _renderKeysStep(overlay, body, foot) {
  let keyInfo = {};
  try {
    keyInfo = (await (await fetch('/settings')).json()).key_info || {};
  } catch (_) {}

  body.innerHTML = `
    <p class="ob-lead">Agora debates run on the models you bring. Paste at least one
    API key — you can add the rest later in Settings.</p>
    ${_LLM_PROVIDERS.map(([p, env, site]) => _keyRowHtml(p, env, site, keyInfo[p])).join('')}
  `;
  _wireKeyRows(body, () => _renderKeysStep(overlay, body, foot));

  const anyValid = _LLM_PROVIDERS.some(([p]) => keyInfo[p]?.valid);
  foot.innerHTML = `
    <button class="btn-ghost btn-sm" id="ob-skip">skip for now</button>
    <button class="btn-primary btn-sm" id="ob-next" ${anyValid ? '' : 'disabled'}>
      next: web search <i class="ti ti-arrow-right" aria-hidden="true"></i>
    </button>`;
  foot.querySelector('#ob-skip').onclick = () => { _step = 1; _render(overlay); };
  foot.querySelector('#ob-next').onclick = () => { _step = 1; _render(overlay); };
}

function _keyRowHtml(provider, env, site, info) {
  const status = info?.valid
    ? `<span class="key-status-ok"><i class="ti ti-check" aria-hidden="true"></i> valid</span>`
    : info?.present
      ? `<span class="key-status-invalid"><i class="ti ti-x" aria-hidden="true"></i> invalid</span>`
      : `<span class="key-status-missing"><i class="ti ti-minus" aria-hidden="true"></i> missing</span>`;
  return `
    <div class="ob-key-row" data-provider="${esc(provider)}">
      <div class="ob-key-meta">
        <span class="key-name">${esc(env)}</span>
        <a class="ob-key-site" href="https://${esc(site)}" target="_blank" rel="noopener">${esc(site)}</a>
      </div>
      ${status}
      <input type="password" class="ob-key-input" placeholder="paste key…" autocomplete="off">
      <button class="btn-solid btn-sm ob-key-save">save</button>
    </div>`;
}

function _wireKeyRows(body, refresh) {
  body.querySelectorAll('.ob-key-row').forEach(row => {
    const btn = row.querySelector('.ob-key-save');
    btn.onclick = async () => {
      const value = row.querySelector('.ob-key-input').value.trim();
      if (!value) return;
      btn.disabled = true;
      btn.textContent = 'testing…';
      try {
        await fetch('/settings/keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: row.dataset.provider, value }),
        });
      } catch (_) {}
      refresh();
    };
  });
}

// ---------------------------------------------------------------- step 2

async function _renderSearchStep(overlay, body, foot) {
  let status = { tier: 'provider', neutral: false };
  let serperInfo = null;
  try {
    status = await (await fetch('/api/search-status')).json();
    serperInfo = ((await (await fetch('/settings')).json()).key_info || {}).serper;
  } catch (_) {}

  const tierHtml = status.neutral
    ? `<div class="ob-search-ok"><i class="ti ti-circle-check" aria-hidden="true"></i>
         web search now enabled — via ${esc(status.tier)}, at no token cost</div>`
    : `<div class="ob-search-warn"><i class="ti ti-alert-triangle" aria-hidden="true"></i>
         no flat-cost search backend found — debates would fall back to vendor search,
         billed through your token budget</div>`;

  body.innerHTML = `
    <p class="ob-lead">Debaters ground their claims through web search. A flat-cost
    backend keeps your whole token budget for arguing.</p>
    ${tierHtml}

    <div class="ob-search-option">
      <span class="ob-search-label">option 1 — serper <span class="field-hint">(~$1 per 1k searches)</span></span>
      <div class="ob-key-row" data-provider="serper">
        <div class="ob-key-meta">
          <span class="key-name">SERPER_API_KEY</span>
          <a class="ob-key-site" href="https://serper.dev" target="_blank" rel="noopener">serper.dev</a>
        </div>
        ${serperInfo?.valid
          ? '<span class="key-status-ok"><i class="ti ti-check" aria-hidden="true"></i> valid</span>'
          : '<span class="key-status-missing"><i class="ti ti-minus" aria-hidden="true"></i> missing</span>'}
        <input type="password" class="ob-key-input" placeholder="paste key…" autocomplete="off">
        <button class="btn-solid btn-sm ob-key-save">save</button>
      </div>
    </div>

    <div class="ob-search-option">
      <span class="ob-search-label">option 2 — SearXNG <span class="field-hint">(free, self-hosted)</span></span>
      <div class="ob-cmd-wrap">
        <pre class="ob-cmd"><code>${esc(_SEARXNG_CMDS)}</code></pre>
        <button class="btn-ghost btn-sm ob-copy" title="copy all"><i class="ti ti-copy" aria-hidden="true"></i></button>
      </div>
      <p class="field-hint" style="margin:6px 0 0">Installs Docker, starts SearXNG, and enables the JSON API in one go.</p>
    </div>
  `;
  _wireKeyRows(body, () => _renderSearchStep(overlay, body, foot));
  const copyBtn = body.querySelector('.ob-copy');
  if (copyBtn) copyBtn.onclick = () => {
    navigator.clipboard.writeText(_SEARXNG_CMDS).then(() => {
      copyBtn.innerHTML = '<i class="ti ti-check" aria-hidden="true"></i>';
      setTimeout(() => { copyBtn.innerHTML = '<i class="ti ti-copy" aria-hidden="true"></i>'; }, 1500);
    }).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = _SEARXNG_CMDS; document.body.appendChild(ta);
      ta.select(); document.execCommand('copy'); ta.remove();
    });
  };

  foot.innerHTML = `
    <button class="btn-ghost btn-sm" id="ob-back"><i class="ti ti-arrow-left" aria-hidden="true"></i> back</button>
    <button class="btn-ghost btn-sm" id="ob-recheck"><i class="ti ti-refresh" aria-hidden="true"></i> re-check</button>
    <button class="btn-primary btn-sm" id="ob-next">
      ${status.neutral ? 'next' : 'continue anyway'} <i class="ti ti-arrow-right" aria-hidden="true"></i>
    </button>`;
  foot.querySelector('#ob-back').onclick    = () => { _step = 0; _render(overlay); };
  foot.querySelector('#ob-recheck').onclick = () => _renderSearchStep(overlay, body, foot);
  foot.querySelector('#ob-next').onclick    = () => { _step = 2; _render(overlay); };
}

// ---------------------------------------------------------------- step 3

function _renderDoneStep(overlay, body, foot) {
  body.innerHTML = `
    <p class="ob-lead">You're set. A debate needs a motion, two debater models, and a
    token budget — everything else has sensible defaults.</p>
    <ul class="ob-done-list">
      <li><i class="ti ti-gavel" aria-hidden="true"></i> <b>new debate</b> — run a single motion</li>
      <li><i class="ti ti-flask-2" aria-hidden="true"></i> <b>experiments</b> — import a CSV of debates and run them as a batch</li>
      <li><i class="ti ti-file-analytics" aria-hidden="true"></i> <b>traces</b> — inspect every agent action and token spend</li>
    </ul>`;
  foot.innerHTML = `
    <button class="btn-ghost btn-sm" id="ob-back"><i class="ti ti-arrow-left" aria-hidden="true"></i> back</button>
    <button class="btn-primary btn-sm" id="ob-start">start your first debate</button>`;
  foot.querySelector('#ob-back').onclick  = () => { _step = 1; _render(overlay); };
  foot.querySelector('#ob-start').onclick = () => {
    _close(overlay);
    window.location.hash = '#/new';
  };
}
