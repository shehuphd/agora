"""Checkpoint functions — persist acts, claims, and state to SQLite and filesystem."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from core.state import Act, DialogueState
from core.export import write_debate_files


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    created_at    TEXT,
    status        TEXT,
    debate_title  TEXT,
    topic         TEXT,
    closure_reason TEXT,
    steelman_mode INTEGER DEFAULT 0,
    config        TEXT
);

CREATE TABLE IF NOT EXISTS acts (
    act_id         TEXT PRIMARY KEY,
    run_id         TEXT,
    turn           INTEGER,
    agent          TEXT,
    agent_role     TEXT,
    act_type       TEXT,
    claim_id       TEXT,
    target_act_id  TEXT,
    content        TEXT,
    reason         TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    model_used     TEXT,
    timestamp      TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id          TEXT PRIMARY KEY,
    run_id            TEXT,
    author            TEXT,
    content           TEXT,
    status            TEXT,
    last_updated      TEXT,
    steelman_attempts INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    """Create all required tables in the given SQLite connection if they don't exist."""
    conn.executescript(_DDL)
    for col_sql in (
        "ALTER TABLE runs ADD COLUMN config TEXT",
        "ALTER TABLE runs ADD COLUMN continued_from TEXT",
    ):
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass
    conn.commit()


def write_act_to_db(conn: sqlite3.Connection, act: Act) -> None:
    """Insert a single Act record into the acts table."""
    conn.execute(
        """INSERT OR REPLACE INTO acts
           (act_id, run_id, turn, agent, agent_role, act_type,
            claim_id, target_act_id, content, reason,
            input_tokens, output_tokens, model_used, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            act.act_id, act.run_id, act.turn, act.agent, act.agent_role,
            act.act_type, act.claim_id, act.target_act_id, act.content,
            act.reason, act.input_tokens, act.output_tokens,
            act.model_used, act.timestamp,
        ),
    )
    conn.commit()


def update_claim_statuses(conn: sqlite3.Connection, state: DialogueState) -> None:
    """Upsert all claims from current state into the claims table."""
    for claim in state.claims.values():
        steelman_attempts = getattr(claim, "steelman_attempts", 0)
        conn.execute(
            """INSERT OR REPLACE INTO claims
               (claim_id, run_id, author, content, status, last_updated, steelman_attempts)
               VALUES (?,?,?,?,?,?,?)""",
            (
                claim.claim_id, claim.run_id, claim.author,
                claim.content, claim.status, claim.last_updated,
                steelman_attempts,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Filesystem persistence
# ---------------------------------------------------------------------------

def write_state_json(state: DialogueState, run_dir: Path) -> None:
    """Serialise full DialogueState to state.json in the run directory."""
    # Convert dataclass to dict, handling nested dataclasses manually
    data = {
        "run_id": state.run_id,
        "turn": state.turn,
        "phase": state.phase,
        "debate_title": state.debate_title,
        "topic": state.topic,
        "created_at": state.created_at,
        "closed_at": state.closed_at,
        "closure_reason": state.closure_reason,
        "next_agent": state.next_agent,
        "legal_acts": state.legal_acts,
        "outstanding_challenges": state.outstanding_challenges,
        "steelman_mode": getattr(state, "steelman_mode", False),
        "claims": {
            cid: {
                "claim_id": c.claim_id,
                "author": c.author,
                "content": c.content,
                "status": c.status,
                "last_updated": c.last_updated,
                "steelman_attempts": getattr(c, "steelman_attempts", 0),
            }
            for cid, c in state.claims.items()
        },
        "acts": [
            {
                "act_id": a.act_id,
                "turn": a.turn,
                "agent": a.agent,
                "agent_role": a.agent_role,
                "act_type": a.act_type,
                "claim_id": a.claim_id,
                "content": a.content[:200],  # truncate for readability
                "timestamp": a.timestamp,
            }
            for a in state.acts
        ],
        "token_usage": {
            role: {"input": u.input_tokens, "output": u.output_tokens}
            for role, u in state.token_usage.items()
        },
    }
    path = run_dir / "state.json"
    path.write_text(json.dumps(data, indent=2))


def append_act_to_markdown(act: Act, run_dir: Path) -> None:
    """Append a single act as a Markdown block to debate.md in the run directory."""
    path = run_dir / "debate.md"
    lines = [
        f"\n### [{act.act_type}] Turn {act.turn} — {act.agent} ({act.agent_role})",
        f"*{act.timestamp}*  |  `{act.model_used}`  |  "
        f"tokens: {act.input_tokens}↑ {act.output_tokens}↓",
        "",
        act.content,
    ]
    if act.reason:
        lines += ["", f"> **Reason:** {act.reason}"]
    lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def export_markdown(state: DialogueState, run_dir: Path) -> None:
    """Write a complete Markdown transcript of the debate to debate.md."""
    path = run_dir / "debate.md"
    header = [
        f"# {state.debate_title}",
        "",
        f"**Topic:** {state.topic}",
        f"**Run:** `{state.run_id}`",
        f"**Started:** {state.created_at}",
        f"**Closed:** {state.closed_at or 'ongoing'}",
        f"**Closure reason:** {state.closure_reason or '—'}",
        "",
        "## Claims",
        "",
    ]
    for claim in state.claims.values():
        header.append(f"- `[{claim.claim_id}]` **{claim.status.upper()}** ({claim.author}): {claim.content}")
    header += ["", "## Act Log", ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header))

    for act in state.acts:
        append_act_to_markdown(act, run_dir)


# ---------------------------------------------------------------------------
# Composite checkpoint
# ---------------------------------------------------------------------------

def checkpoint(conn: sqlite3.Connection, state: DialogueState, act: Act, run_dir: Path) -> None:
    """Persist act, update claim statuses, write state.json, debate.json, and debate.md."""
    write_act_to_db(conn, act)
    update_claim_statuses(conn, state)
    write_state_json(state, run_dir)
    write_debate_files(state, run_dir)  # rewrites debate.json + debate.md from full state


# ---------------------------------------------------------------------------
# Load state from DB (for API reads)
# ---------------------------------------------------------------------------

def _reconstruct_outstanding_challenges(acts: list) -> list:
    """Replay challenge/resolve logic across the act list to find unresolved challenges."""
    outstanding: list = []
    for act in acts:
        if act.act_type == "CHALLENGE":
            outstanding.append(act.act_id)
        elif act.act_type == "REVISE":
            if act.target_act_id and act.target_act_id in outstanding:
                outstanding.remove(act.target_act_id)
        elif act.act_type == "CONCEDE":
            if act.target_act_id and act.target_act_id in outstanding:
                outstanding.remove(act.target_act_id)
            elif outstanding:
                for ch_id in reversed(list(outstanding)):
                    ch_act = next(
                        (a for a in acts if a.act_id == ch_id
                         and (not act.claim_id or a.claim_id == act.claim_id)),
                        None,
                    )
                    if ch_act:
                        outstanding.remove(ch_id)
                        break
    return outstanding


def debate_turn_idx(acts: list) -> int:
    """Return the turn_idx to resume from when continuing a debate.

    Counts proposition + opposition acts only — this matches the runner's
    turn_idx counter which alternates between agents[0] and agents[1].
    """
    return sum(1 for a in acts if a.agent_role in ("proposition", "opposition"))


def load_state(conn: sqlite3.Connection, run_id: str) -> DialogueState:
    """Reconstruct a DialogueState from the SQLite database for a given run_id."""
    from core.state import TokenUsage, Claim, Act as ActDC

    run_row = conn.execute(
        "SELECT run_id, created_at, status, debate_title, topic, closure_reason, steelman_mode, config FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not run_row:
        raise ValueError(f"Run {run_id} not found in database")

    act_rows = conn.execute(
        "SELECT act_id, run_id, turn, agent, agent_role, act_type, claim_id, target_act_id, content, reason, input_tokens, output_tokens, model_used, timestamp FROM acts WHERE run_id=? ORDER BY turn, timestamp",
        (run_id,),
    ).fetchall()

    claim_rows = conn.execute(
        "SELECT claim_id, run_id, author, content, status, last_updated, steelman_attempts FROM claims WHERE run_id=?",
        (run_id,),
    ).fetchall()

    acts = [
        ActDC(
            act_id=r[0], run_id=r[1], turn=r[2], agent=r[3],
            agent_role=r[4], act_type=r[5], claim_id=r[6],
            target_act_id=r[7], content=r[8], reason=r[9],
            input_tokens=r[10], output_tokens=r[11],
            model_used=r[12], timestamp=r[13],
        )
        for r in act_rows
    ]

    claims = {
        r[0]: Claim(
            claim_id=r[0], run_id=r[1], author=r[2],
            content=r[3], status=r[4], last_updated=r[5],
            steelman_attempts=r[6] if len(r) > 6 else 0,
        )
        for r in claim_rows
    }

    # Reconstruct token usage by summing across acts
    token_usage: dict = {
        role: TokenUsage()
        for role in ("proposition", "opposition", "moderator", "synthesiser")
    }
    for act in acts:
        role = act.agent_role
        if role in token_usage:
            token_usage[role].input_tokens += act.input_tokens
            token_usage[role].output_tokens += act.output_tokens

    # Derive current phase from last phase-changing act type (STATUS doesn't change phase)
    phase = "init"
    for act in reversed(acts):
        if act.act_type == "CLOSE":
            phase = "closed"
            break
        if act.act_type not in ("STATUS", "ARGUMENT_MAP"):
            from core.state import PHASE_MAP
            phase = PHASE_MAP.get(act.act_type, "init")
            break

    # steelman_mode: DB column first, then fall back to stored config JSON
    # (older runs were inserted without the steelman_mode column value)
    steelman_mode = bool(run_row[6]) if len(run_row) > 6 and run_row[6] else False
    if not steelman_mode and len(run_row) > 7 and run_row[7]:
        try:
            steelman_mode = bool(json.loads(run_row[7]).get("steelman_mode", False))
        except Exception:
            pass

    # Derive next_agent from last proposition/opposition act
    debate_acts = [a for a in acts if a.agent_role in ("proposition", "opposition")]
    if debate_acts:
        last_role = debate_acts[-1].agent_role
        next_agent = "opposition" if last_role == "proposition" else "proposition"
    else:
        next_agent = "proposition"

    return DialogueState(
        run_id=run_row[0],
        turn=len(acts),
        phase=phase,
        claims=claims,
        acts=acts,
        outstanding_challenges=_reconstruct_outstanding_challenges(acts),
        next_agent=next_agent,
        legal_acts=[],
        token_usage=token_usage,
        debate_title=run_row[3],
        topic=run_row[4],
        config={},
        created_at=run_row[1],
        closed_at=None,
        closure_reason=run_row[5],
        steelman_mode=steelman_mode,
    )
