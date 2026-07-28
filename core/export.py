"""Shared export utilities — build rich JSON + Markdown from DialogueState."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

from core.state import DialogueState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _n(v) -> str:
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return str(v)
    return str(v) if v is not None else "—"


# ---------------------------------------------------------------------------
# Act content rendering
#
# Two act types carry JSON in `content` rather than prose: STATUS holds the
# moderator's termination_checks, ARGUMENT_MAP holds the synthesiser's verdict.
# The UI parses both (appendStatusBubble, renderArgumentMap); a Markdown export
# that dumps the raw object makes the synthesis — the conclusion of the whole
# debate — the least readable part of the document. These render the same
# fields the UI shows, and fall back to the raw string whenever the content is
# not the JSON we expect, so a malformed act still exports its bytes.
# ---------------------------------------------------------------------------

def format_status_content(content: str) -> str:
    """Moderator STATUS termination_checks as an inline chip line."""
    try:
        c = json.loads(content)
        if not isinstance(c, dict):
            return str(content)
    except Exception:
        return str(content)

    chips = []
    if c.get("turns_used") is not None:
        chips.append(f"`turn {c['turns_used']}/{c.get('max_turns', '?')}`")
    if c.get("outstanding_challenge_count") is not None:
        chips.append(f"`challenges open: {c['outstanding_challenge_count']}`")
    if c.get("total_tokens") is not None:
        chips.append(f"`tokens {_n(c['total_tokens'])}/{_n(c.get('token_budget'))}`")
    if c.get("repetition_count"):
        chips.append(f"`⚠ repetitions: {c['repetition_count']}"
                     f"/{c.get('repetition_tolerance', '?')}`")
    return " · ".join(chips) if chips else str(content)


def _claim_bullets(claims: list, notes: list[tuple[str, str]]) -> list[str]:
    """One bullet per claim, with `notes` as (json_field, label) sub-bullets."""
    out = []
    for c in claims:
        if not isinstance(c, dict):
            out.append(f"- {c}")
            continue
        out.append(f"- {c.get('final_text') or c.get('content') or ''}")
        out += [f"  - *{label}: {c[field]}*" for field, label in notes if c.get(field)]
    return out


def format_argument_map_content(content: str) -> str:
    """Synthesiser ARGUMENT_MAP as claim sections plus the arbiter summary."""
    try:
        c = json.loads(content)
        if not isinstance(c, dict):
            return str(content)
    except Exception:
        return str(content)

    sections = [
        ("surviving_claims", "Surviving claims", [("survived_because", "Survived because")]),
        ("revised_claims", "Revised claims",
         [("original_text", "Originally"), ("revised_because", "Revised because")]),
        ("contested_claims", "Contested claims",
         [("contested_because", "Contested because"), ("evidence_needed", "Evidence needed")]),
    ]

    lines: list[str] = []
    for key, heading, notes in sections:
        claims = c.get(key) or []
        if not claims:
            continue
        lines += [f"**{heading}** ({len(claims)})", ""]
        lines += _claim_bullets(claims, notes)
        lines.append("")

    if c.get("arbiter_summary"):
        lines += ["**Arbiter summary**", "", str(c["arbiter_summary"]), ""]

    if not lines:
        # A map with no claims and no summary says something real: nothing survived.
        return "*(empty argument map — no claims recorded)*"
    return "\n".join(lines).rstrip()


_CONTENT_FORMATTERS = {
    "STATUS": format_status_content,
    "ARGUMENT_MAP": format_argument_map_content,
}


def format_act_content(act_type: str, content: str) -> str:
    """Render an act's content for Markdown, unpacking the JSON-bearing types."""
    if not content:
        return ""
    return _CONTENT_FORMATTERS.get(act_type, str)(content)


# ---------------------------------------------------------------------------
# Build the API-response-style export dict from live state
# ---------------------------------------------------------------------------

def build_export_dict(
    state: DialogueState,
    config_dict: dict | None = None,
    override_log: list | None = None,
) -> dict:
    """Build a JSON-serialisable export dict that matches the /debates/{id} API shape."""
    cfg = config_dict or {}

    acts_out = [
        {
            "act_id":        a.act_id,
            "run_id":        a.run_id,
            "turn":          a.turn,
            "agent":         a.agent,
            "agent_role":    a.agent_role,
            "act_type":      a.act_type,
            "claim_id":      a.claim_id,
            "target_act_id": a.target_act_id,
            "content":       a.content,     # full, untruncated
            "reason":        a.reason,
            "input_tokens":  a.input_tokens,
            "output_tokens": a.output_tokens,
            "model_used":    a.model_used,
            "timestamp":     a.timestamp,
        }
        for a in state.acts
    ]

    claims_out = [
        {
            "claim_id":    c.claim_id,
            "run_id":      c.run_id,
            "author":      c.author,
            "content":     c.content,
            "status":      c.status,
            "last_updated": c.last_updated,
        }
        for c in state.claims.values()
    ]

    status = "closed" if (state.phase == "closed" or state.closure_reason) else "running"

    return {
        "run_id":        state.run_id,
        "created_at":    state.created_at,
        "closed_at":     state.closed_at,
        "status":        status,
        "debate_title":  state.debate_title,
        "topic":         state.topic,
        "closure_reason": state.closure_reason,
        "config":        cfg,
        "acts":          acts_out,
        "claims":        claims_out,
        "override_log":  override_log or [],
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def build_markdown(data: dict) -> str:
    """Render a debate export dict as a rich human-readable Markdown transcript."""
    cfg  = data.get("config") or {}
    acts = data.get("acts") or []

    created_at = data.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S UTC")
    except Exception:
        date_str = created_at[:10] if len(created_at) >= 10 else created_at
        time_str = created_at[11:19] if len(created_at) >= 19 else ""

    # Derive nicknames from act records (more reliable than config for moderator)
    nickname_map: dict[str, str] = {}
    for act in acts:
        role  = act.get("agent_role", "")
        agent = act.get("agent", "")
        if role and agent and role not in nickname_map:
            nickname_map[role] = agent

    prop_nick  = cfg.get("proposition_nickname") or nickname_map.get("proposition", "Proposition")
    opp_nick   = cfg.get("opposition_nickname")  or nickname_map.get("opposition",  "Opposition")
    mod_nick   = nickname_map.get("moderator", "Moderator")
    synth_nick = nickname_map.get("synthesiser", "Synthesiser")

    tok: dict[str, list[int]] = {
        "proposition": [0, 0],
        "opposition":  [0, 0],
        "moderator":   [0, 0],
        "synthesiser": [0, 0],
    }
    for act in acts:
        role = act.get("agent_role", "")
        if role in tok:
            tok[role][0] += act.get("input_tokens")  or 0
            tok[role][1] += act.get("output_tokens") or 0
    total_in  = sum(v[0] for v in tok.values())
    total_out = sum(v[1] for v in tok.values())

    title    = data.get("debate_title") or data.get("topic") or data.get("run_id", "Debate")
    steelman = cfg.get("steelman_mode", False)

    lines: list[str] = [
        f"# {title}",
        "",
        "> Generated by [Agora](https://github.com/shehuphd/agora)",
        "",
        "## Metadata",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Run | `{data.get('run_id', '')}` |",
        f"| Date | {date_str} |",
        f"| Time | {time_str} |",
        f"| Topic | {data.get('topic', '')} |",
        f"| Status | {data.get('status', '')} |",
    ]
    if data.get("closure_reason"):
        lines.append(f"| Closure reason | {data['closure_reason']} |")

    lines += [
        "",
        "## Participants",
        "",
        "| Role | Nickname | Model | Temperature |",
        "|------|----------|-------|-------------|",
        f"| Proposition | {prop_nick} | {cfg.get('proposition_model', '—')} | {cfg.get('temperature_proposition', '—')} |",
        f"| Opposition  | {opp_nick}  | {cfg.get('opposition_model',  '—')} | {cfg.get('temperature_opposition',  '—')} |",
        f"| Moderator   | {mod_nick}  | {cfg.get('moderator_model',   '—')} | — |",
        "",
        "## Settings",
        "",
        "| Parameter | Value |",
        "|-----------|-------|",
        f"| Max turns | {_n(cfg.get('max_turns'))} |",
        f"| Max time | {_n(cfg.get('max_time_minutes'))} min |",
        f"| Token budget | {_n(cfg.get('token_budget'))} |",
        f"| Min challenges | {_n(cfg.get('min_challenges'))} |",
        f"| Min concessions | {_n(cfg.get('min_concessions'))} |",
        f"| Repetition tolerance | {_n(cfg.get('repetition_tolerance'))} |",
        f"| Aggression | {_n(cfg.get('aggression'))} |",
        f"| Rapoport (steelman) mode | {'Yes' if steelman else 'No'} |",
        "",
        "## Token Usage",
        "",
        "| Agent | Input | Output | Total |",
        "|-------|------:|-------:|------:|",
        f"| {prop_nick} | {_n(tok['proposition'][0])} | {_n(tok['proposition'][1])} | {_n(sum(tok['proposition']))} |",
        f"| {opp_nick}  | {_n(tok['opposition'][0])}  | {_n(tok['opposition'][1])}  | {_n(sum(tok['opposition']))}  |",
        f"| {mod_nick}  | {_n(tok['moderator'][0])}   | {_n(tok['moderator'][1])}   | {_n(sum(tok['moderator']))}   |",
    ]
    if tok["synthesiser"][0] or tok["synthesiser"][1]:
        lines.append(
            f"| {synth_nick} | {_n(tok['synthesiser'][0])} | {_n(tok['synthesiser'][1])} | {_n(sum(tok['synthesiser']))} |"
        )
    lines += [
        f"| **Total** | **{_n(total_in)}** | **{_n(total_out)}** | **{_n(total_in + total_out)}** |",
        "",
    ]

    override_log = data.get("override_log") or []
    if override_log:
        lines += [
            "## Mid-Run Overrides",
            "",
            "| Timestamp | Field | Old Value | New Value |",
            "|-----------|-------|----------:|----------:|",
        ]
        for ov in override_log:
            field   = ov.get("field", "")
            old_val = _n(ov.get("old_value")) if ov.get("old_value") is not None else "original"
            new_val = _n(ov.get("new_value", ""))
            ts      = str(ov.get("timestamp", ""))[:19].replace("T", " ")
            lines.append(f"| {ts} | {field} | {old_val} | {new_val} |")
        lines.append("")

    lines += [
        "## Transcript",
        "",
    ]

    for act in acts:
        act_type = act.get("act_type", "")
        agent    = act.get("agent") or act.get("agent_role", "")
        turn     = act.get("turn", "")
        content  = act.get("content", "")
        reason   = act.get("reason", "")
        claim_id = act.get("claim_id", "")
        model    = act.get("model_used", "")

        lines.append(f"### Turn {turn} · {agent} · {act_type}")
        lines.append("")
        meta_parts = []
        if claim_id:
            meta_parts.append(f"claim `{claim_id}`")
        if model:
            meta_parts.append(f"model: {model}")
        if meta_parts:
            lines.append(f"*{' · '.join(meta_parts)}*")
            lines.append("")
        if content:
            lines.append(format_act_content(act_type, content))
            lines.append("")
        if reason:
            lines.append(f"*{reason}*")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Filesystem writers
# ---------------------------------------------------------------------------

def write_debate_files(state: DialogueState, run_dir: Path) -> None:
    """Rewrite debate.json in run_dir from current state.

    Called after every checkpoint so the file is always current. Reads
    config.json and overrides.json from the run dir if they exist.

    Only data is persisted. Markdown is a *rendering* of this file and is
    produced on demand at export time — writing it here would freeze every run
    at whichever renderer existed when it closed, so a later fix to
    build_markdown would never reach it.
    """
    config_path   = run_dir / "config.json"
    overrides_path = run_dir / "overrides.json"

    config_dict: dict = {}
    if config_path.exists():
        try:
            config_dict = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    override_log: list = []
    if overrides_path.exists():
        try:
            override_log = json.loads(overrides_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    data = build_export_dict(state, config_dict, override_log)

    (run_dir / "debate.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
