// render.js — pure DOM rendering utilities, no external state dependencies.
// Imported as an ES module by app.js, debate.js, and history.js.

export function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Apply inline markdown (bold, italic, strikethrough) to already-escaped HTML text.
function _mdInline(rawText) {
  const result = [];
  // Order matters: ~~ first, then ** (before *), then *
  const re = /~~(.+?)~~|\*\*(.+?)\*\*|\*([^*\n]+?)\*/g;
  let last = 0, m;
  while ((m = re.exec(rawText)) !== null) {
    result.push(esc(rawText.slice(last, m.index)).replace(/\n/g, '<br>'));
    if (m[1] !== undefined)      result.push(`<del>${esc(m[1]).replace(/\n/g, '<br>')}</del>`);
    else if (m[2] !== undefined) result.push(`<strong>${esc(m[2]).replace(/\n/g, '<br>')}</strong>`);
    else if (m[3] !== undefined) result.push(`<em>${esc(m[3]).replace(/\n/g, '<br>')}</em>`);
    last = m.index + m[0].length;
  }
  result.push(esc(rawText.slice(last)).replace(/\n/g, '<br>'));
  return result.join('');
}

// Convert markdown links [text](https://url), bold, italic, strikethrough, and newlines to HTML.
export function renderContent(text) {
  const s = String(text ?? '');
  const parts = [];
  const linkRe = /\[([^\]]+)\]\((https?:\/\/[^)]{1,512})\)/g;
  let last = 0, match;
  while ((match = linkRe.exec(s)) !== null) {
    parts.push(_mdInline(s.slice(last, match.index)));
    parts.push(`<a class="act-link" href="${esc(match[2])}" target="_blank" rel="noopener noreferrer">${esc(match[1])}</a>`);
    last = match.index + match[0].length;
  }
  parts.push(_mdInline(s.slice(last)));
  return parts.join('');
}

export function formatTokens(n) {
  return Math.round(n || 0).toLocaleString();
}

export function triggerDownload(blob, contentDisposition) {
  const filename = (contentDisposition || '').match(/filename="([^"]+)"/)?.[1] || 'agora-export.json';
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export const ACT_BUBBLE_CLASS = {
  STEELMAN:               'act-steelman',
  ACCEPT_STEELMAN:        'act-accept',
  REJECT_STEELMAN:        'act-reject',
  STATUS:                 'act-mod',
  CLOSE:                  'act-mod',
  MODERATOR_INTERVENTION: 'act-mod',
};

export const PILL_CLASS = {
  proposition: 'pill-prop',
  opposition:  'pill-opp',
  moderator:   'pill-mod',
  synthesiser: 'pill-synth',
};

export const ACT_LABEL = {
  STEELMAN:               "restating proposition's claim",
  ACCEPT_STEELMAN:        'steelman accepted · challenge may proceed',
  REJECT_STEELMAN:        'steelman rejected · Opposition must restate',
  MODERATOR_INTERVENTION: 'moderator intervention',
};

export function appendSystemBubble(iconClass, text) {
  const feed = document.getElementById('act-feed');
  if (!feed) return;
  const div = document.createElement('div');
  div.className = 'act-bubble act-system';
  div.innerHTML = `<div class="act-text"><i class="ti ${esc(iconClass)}" aria-hidden="true"></i> ${esc(text)}</div>`;
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function appendActBubble(act) {
  if (act.act_type === 'STATUS')       { appendStatusBubble(act); return; }
  if (act.act_type === 'ARGUMENT_MAP') { appendArgumentMapBubble(act); return; }

  const feed      = document.getElementById('act-feed');
  const bubbleCls = ACT_BUBBLE_CLASS[act.act_type] || '';
  const pillCls   = PILL_CLASS[act.agent_role] || 'pill-mod';
  const label     = ACT_LABEL[act.act_type]
    ? `<span class="act-label">· ${esc(ACT_LABEL[act.act_type])}</span>` : '';

  const div = document.createElement('div');
  div.className = `act-bubble ${bubbleCls}`.trim();
  div.innerHTML = `
    <div class="act-head">
      <span class="agent-pill ${pillCls}">${esc(act.agent || act.agent_role)}</span>
      <span class="act-type">${esc(act.act_type)}</span>
      ${label}
      <span class="act-turn">turn ${esc(act.turn)}</span>
    </div>
    <div class="act-text">${renderContent(act.content)}</div>
    ${act.reason        ? `<div class="act-target">reason: ${renderContent(act.reason)}</div>` : ''}
    ${act.target_act_id ? `<div class="act-target">→ ${esc(act.target_act_id)}</div>` : ''}
    ${act.claim_id      ? `<span class="claim-tag tag-open">${esc(act.claim_id)}</span>` : ''}
  `;
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function appendIntroBubble(msg) {
  const feed = document.getElementById('act-feed');
  const div = document.createElement('div');
  div.className = 'act-bubble act-intro';

  if (msg.is_continuation) {
    const shortId = esc((msg.continued_from || '').slice(0, 8));
    div.innerHTML = `
      <div class="act-head">
        <span class="agent-pill pill-mod">${esc(msg.moderator_nickname || 'Moderator')}</span>
        <span class="act-type">RESUMING</span>
      </div>
      <div class="act-text">
        Continuing from turn ${esc(String(msg.turn_start ?? '?'))}.<br>
        The thesis: <strong>${esc(msg.topic)}</strong>.
        <br><small style="color:var(--text-muted)">Continued from run ${shortId}&hellip;</small>
      </div>
    `;
  } else {
    const steelmanLine = msg.steelman_mode
      ? `<br><span class="act-label">· Rapoport mode active — Opposition must steelman each claim before challenging.</span>`
      : '';
    div.innerHTML = `
      <div class="act-head">
        <span class="agent-pill pill-mod">${esc(msg.moderator_nickname || 'Moderator')}</span>
        <span class="act-type">INTRO</span>
      </div>
      <div class="act-text">
        The thesis before us: <strong>${esc(msg.topic)}</strong>.<br>
        Arguing in favour: <strong>${esc(msg.proposition_nickname)}</strong>.
        Arguing against: <strong>${esc(msg.opposition_nickname)}</strong>.
        Let's begin.${steelmanLine}
      </div>
    `;
  }
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function appendStatusBubble(act) {
  const feed = document.getElementById('act-feed');
  const div = document.createElement('div');
  div.className = 'act-bubble act-mod';
  let chips = '';
  try {
    const c = JSON.parse(act.content);
    const turns = c.turns_used != null
      ? `<span class="status-chip">turn ${c.turns_used}/${c.max_turns ?? '?'}</span>` : '';
    const chal = c.outstanding_challenge_count != null
      ? `<span class="status-chip">challenges open: ${c.outstanding_challenge_count}</span>` : '';
    const rep = c.repetition_count > 0
      ? `<span class="status-chip status-chip-warn">repetitions: ${c.repetition_count}</span>` : '';
    if (turns || chal || rep) chips = `<div class="status-chips">${turns}${chal}${rep}</div>`;
  } catch (_) {}
  div.innerHTML = `
    <div class="act-head">
      <span class="agent-pill pill-mod">${esc(act.agent || 'Moderator')}</span>
      <span class="act-type">STATUS</span>
      <span class="act-turn">turn ${esc(act.turn)}</span>
    </div>
    ${act.reason ? `<div class="act-status-summary">${esc(act.reason)}</div>` : ''}
    ${chips}
  `;
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function appendArgumentMapBubble(act) {
  const feed = document.getElementById('act-feed');
  const div = document.createElement('div');
  div.className = 'act-bubble act-mod';
  let inner = '';
  try {
    inner = renderArgumentMap(JSON.parse(act.content));
  } catch (_) {
    inner = `<div class="act-text">${esc(act.content)}</div>`;
  }
  div.innerHTML = `
    <div class="act-head">
      <span class="agent-pill pill-synth">${esc(act.agent || 'Synthesis')}</span>
      <span class="act-type">ARGUMENT MAP</span>
    </div>
    ${inner}
  `;
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  markDebateClosed();
}

export function appendErrorBubble(message) {
  const feed = document.getElementById('act-feed');
  const div = document.createElement('div');
  div.className = 'act-bubble act-error';
  div.innerHTML = `
    <div class="act-head">
      <span class="agent-pill pill-mod">system</span>
      <span class="act-type">ERROR</span>
    </div>
    <div class="act-text">${esc(message)}</div>
  `;
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function showThinkingBubble(agentName, role) {
  const feed = document.getElementById('act-feed');
  const pillCls = PILL_CLASS[role] || 'pill-mod';
  const div = document.createElement('div');
  div.id = 'thinking-bubble';
  div.className = 'act-thinking';
  div.innerHTML = `
    <span class="agent-pill ${pillCls}">${esc(agentName || role)}</span>
    <span style="font-size:11px;color:var(--text-muted)">thinking</span>
    <div class="thinking-dots"><span></span><span></span><span></span></div>
  `;
  feed.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export function removeThinkingBubble() {
  document.getElementById('thinking-bubble')?.remove();
}

export function markDebateClosed() {
  const badge = document.getElementById('dh-turn-badge');
  if (badge) {
    badge.textContent = 'closed';
    badge.style.background = 'var(--surface-1)';
    badge.style.color      = 'var(--text-muted)';
  }
  const endBtn = document.getElementById('btn-end');
  if (endBtn) endBtn.disabled = true;

  const rerunBtn = document.getElementById('btn-rerun');
  if (rerunBtn) rerunBtn.style.display = 'inline-flex';

  const feed = document.getElementById('act-feed');
  if (feed && !feed.querySelector('.debate-end-cta')) {
    const cta = document.createElement('div');
    cta.className = 'debate-end-cta';
    cta.innerHTML = `
      <span class="debate-end-label">Debate closed</span>
      <a class="btn-ghost" href="#/history">← history</a>
      <a class="btn-primary" href="#/new">+ new debate</a>
    `;
    feed.appendChild(cta);
    cta.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

export function renderArgumentMap(data) {
  const parts = [];

  if (data.surviving_claims?.length) {
    parts.push(`<div class="am-section">
      <div class="am-head am-survived">Surviving claims</div>
      ${data.surviving_claims.map(c => `
        <div class="am-claim">
          <div class="am-claim-text">${renderContent(c.final_text)}</div>
          ${c.survived_because ? `<div class="am-claim-note">${renderContent(c.survived_because)}</div>` : ''}
        </div>`).join('')}
    </div>`);
  }

  if (data.revised_claims?.length) {
    parts.push(`<div class="am-section">
      <div class="am-head am-revised">Revised claims</div>
      ${data.revised_claims.map(c => `
        <div class="am-claim">
          <div class="am-claim-text">${renderContent(c.final_text)}</div>
          ${c.original_text   ? `<div class="am-claim-note">Originally: ${esc(c.original_text)}</div>` : ''}
          ${c.revised_because ? `<div class="am-claim-note">${renderContent(c.revised_because)}</div>` : ''}
        </div>`).join('')}
    </div>`);
  }

  if (data.contested_claims?.length) {
    parts.push(`<div class="am-section">
      <div class="am-head am-contested">Contested claims</div>
      ${data.contested_claims.map(c => `
        <div class="am-claim">
          <div class="am-claim-text">${renderContent(c.final_text)}</div>
          ${c.contested_because ? `<div class="am-claim-note">${renderContent(c.contested_because)}</div>` : ''}
          ${c.evidence_needed   ? `<div class="am-claim-evidence">Evidence needed: ${esc(c.evidence_needed)}</div>` : ''}
        </div>`).join('')}
    </div>`);
  }

  if (data.arbiter_summary) {
    parts.push(`<div class="am-section am-arbiter">
      <div class="am-head">Arbiter summary</div>
      <div class="am-arbiter-text">${renderContent(data.arbiter_summary)}</div>
    </div>`);
  }

  return parts.join('') || `<div class="act-text">${esc(JSON.stringify(data, null, 2))}</div>`;
}

export function renderTokenStrip(tok, budget) {
  document.getElementById('tok-total').textContent = formatTokens(tok.total);
  document.getElementById('tok-prop').textContent  = formatTokens(tok.proposition);
  document.getElementById('tok-opp').textContent   = formatTokens(tok.opposition);
  document.getElementById('tok-mod').textContent   = formatTokens(tok.moderator);

  const pct = Math.min(100, Math.round((tok.total / budget) * 100));
  document.getElementById('budget-fill').style.width  = `${pct}%`;
  document.getElementById('budget-pct').textContent   = `${pct}%`;
  document.getElementById('budget-label').textContent =
    `${formatTokens(tok.total)} / ${formatTokens(budget)} tokens used`;
}

export function updateTerminationTracker(checks, cfg) {
  const maxTurns = checks.max_turns || cfg.max_turns || 8;
  const turns    = checks.turns_used || 0;
  const pct      = Math.min(100, Math.round((turns / maxTurns) * 100));
  document.getElementById('term-turns').style.width     = `${pct}%`;
  document.getElementById('term-turns-val').textContent = `${turns} / ${maxTurns}`;
  document.getElementById('term-challenges-val').textContent =
    checks.outstanding_challenge_count ?? '—';
  if (checks.repetition_count > 0) {
    const el = document.getElementById('term-rep-val');
    el.textContent = `${checks.repetition_count} detected`;
    el.classList.remove('term-val-ok');
    el.style.color = 'var(--text-warning)';
  }
}
