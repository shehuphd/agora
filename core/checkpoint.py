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
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
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
    session_id     TEXT,
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
    session_id        TEXT,
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
    # Migrate existing DBs that predate the config column
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN config TEXT")
        conn.commit()
    except Exception:
        pass
    conn.commit()


def write_act_to_db(conn: sqlite3.Connection, act: Act) -> None:
    """Insert a single Act record into the acts table."""
    conn.execute(
        """INSERT OR REPLACE INTO acts
           (act_id, session_id, turn, agent, agent_role, act_type,
            claim_id, target_act_id, content, reason,
            input_tokens, output_tokens, model_used, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            act.act_id, act.session_id, act.turn, act.agent, act.agent_role,
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
               (claim_id, session_id, author, content, status, last_updated, steelman_attempts)
               VALUES (?,?,?,?,?,?,?)""",
            (
                claim.claim_id, claim.session_id, claim.author,
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
        "session_id": state.session_id,
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
        f"**Session:** `{state.session_id}`",
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

def load_state(conn: sqlite3.Connection, session_id: str) -> DialogueState:
    """Reconstruct a DialogueState from the SQLite database for a given session_id."""
    from core.state import TokenUsage, Claim, Act as ActDC

    session_row = conn.execute(
        "SELECT session_id, created_at, status, debate_title, topic, closure_reason, steelman_mode FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not session_row:
        raise ValueError(f"Session {session_id} not found in database")

    act_rows = conn.execute(
        "SELECT act_id, session_id, turn, agent, agent_role, act_type, claim_id, target_act_id, content, reason, input_tokens, output_tokens, model_used, timestamp FROM acts WHERE session_id=? ORDER BY turn, timestamp",
        (session_id,),
    ).fetchall()

    claim_rows = conn.execute(
        "SELECT claim_id, session_id, author, content, status, last_updated, steelman_attempts FROM claims WHERE session_id=?",
        (session_id,),
    ).fetchall()

    acts = [
        ActDC(
            act_id=r[0], session_id=r[1], turn=r[2], agent=r[3],
            agent_role=r[4], act_type=r[5], claim_id=r[6],
            target_act_id=r[7], content=r[8], reason=r[9],
            input_tokens=r[10], output_tokens=r[11],
            model_used=r[12], timestamp=r[13],
        )
        for r in act_rows
    ]

    claims = {
        r[0]: Claim(
            claim_id=r[0], session_id=r[1], author=r[2],
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

    # Derive current phase from last non-moderator act type
    phase = "init"  # before any act, opening state allows ASSERT
    for act in reversed(acts):
        if act.act_type == "CLOSE":
            phase = "closed"
            break
        if act.act_type not in ("STATUS",):
            from core.state import PHASE_MAP
            phase = PHASE_MAP.get(act.act_type, "init")
            break

    # steelman_mode stored as INTEGER 0/1 in DB
    steelman_mode = bool(session_row[6]) if len(session_row) > 6 and session_row[6] is not None else False

    return DialogueState(
        session_id=session_row[0],
        turn=len(acts),
        phase=phase,
        claims=claims,
        acts=acts,
        outstanding_challenges=[],
        next_agent="proposition",
        legal_acts=[],
        token_usage=token_usage,
        debate_title=session_row[3],
        topic=session_row[4],
        config={},
        created_at=session_row[1],
        closed_at=None,
        closure_reason=session_row[5],
        steelman_mode=steelman_mode,
    )
