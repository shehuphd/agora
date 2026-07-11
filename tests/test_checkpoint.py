"""Tests for core/checkpoint.py — uses in-memory SQLite, no filesystem side effects except tmpdir."""
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from core.state import Act, Claim, DialogueState, TokenUsage
from core.checkpoint import (
    init_db,
    write_act_to_db,
    update_claim_statuses,
    write_state_json,
    append_act_to_markdown,
    export_markdown,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """Open a fresh in-memory SQLite connection for a single test."""
    return sqlite3.connect(":memory:")


def _make_act(run_id: str = "sess-001") -> Act:
    """Construct a minimal Act fixture."""
    return Act(
        act_id="act-001",
        run_id=run_id,
        turn=1,
        agent="Thesis",
        agent_role="proposition",
        act_type="ASSERT",
        claim_id="claim-001",
        target_act_id=None,
        content="AI will fundamentally transform the global economy.",
        reason="Opening assertion — strongest available claim.",
        input_tokens=120,
        output_tokens=60,
        model_used="claude-sonnet-4-6",
        timestamp=datetime.utcnow().isoformat(),
    )


def _make_state(run_id: str = "sess-001") -> DialogueState:
    """Construct a minimal DialogueState fixture with one claim and one act."""
    now = datetime.utcnow().isoformat()
    act = _make_act(run_id)
    claim = Claim(
        claim_id="claim-001",
        run_id=run_id,
        author="proposition",
        content="AI will fundamentally transform the global economy.",
        status="open",
        last_updated=now,
    )
    return DialogueState(
        run_id=run_id,
        turn=1,
        phase="assert",
        claims={"claim-001": claim},
        acts=[act],
        outstanding_challenges=[],
        next_agent="opposition",
        legal_acts=["CHALLENGE", "CONCEDE"],
        token_usage={
            "proposition": TokenUsage(input_tokens=120, output_tokens=60),
            "opposition":  TokenUsage(),
            "moderator":   TokenUsage(),
            "synthesiser": TokenUsage(),
        },
        debate_title="Test Debate",
        topic="AI economics",
        config={},
        created_at=now,
        closed_at=None,
        closure_reason=None,
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_init_db_creates_all_tables():
    """init_db must create runs, acts, claims, and meta tables."""
    conn = _conn()
    init_db(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "runs" in tables
    assert "acts" in tables
    assert "claims" in tables
    assert "meta" in tables


def test_init_db_is_idempotent():
    """Calling init_db twice must not raise (CREATE TABLE IF NOT EXISTS)."""
    conn = _conn()
    init_db(conn)
    init_db(conn)  # second call — must be silent


# ---------------------------------------------------------------------------
# Act round-trip
# ---------------------------------------------------------------------------

def test_write_act_persists_all_fields():
    """write_act_to_db should persist act_id, act_type, content, and token counts."""
    conn = _conn()
    init_db(conn)
    act = _make_act()
    write_act_to_db(conn, act)

    row = conn.execute(
        "SELECT act_id, act_type, content, input_tokens, output_tokens, model_used FROM acts WHERE act_id=?",
        (act.act_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == "act-001"
    assert row[1] == "ASSERT"
    assert "economy" in row[2]
    assert row[3] == 120
    assert row[4] == 60
    assert row[5] == "claude-sonnet-4-6"


def test_write_act_is_upsertable():
    """Writing the same act twice must not raise (INSERT OR REPLACE)."""
    conn = _conn()
    init_db(conn)
    act = _make_act()
    write_act_to_db(conn, act)
    write_act_to_db(conn, act)  # second write — must be silent
    count = conn.execute("SELECT COUNT(*) FROM acts WHERE act_id=?", (act.act_id,)).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Claim round-trip
# ---------------------------------------------------------------------------

def test_update_claim_statuses_upserts():
    """update_claim_statuses should insert claim records from state.claims."""
    conn = _conn()
    init_db(conn)
    state = _make_state()
    update_claim_statuses(conn, state)

    row = conn.execute(
        "SELECT claim_id, author, status FROM claims WHERE claim_id=?",
        ("claim-001",),
    ).fetchone()
    assert row is not None
    assert row[1] == "proposition"
    assert row[2] == "open"


def test_claim_status_updates_on_second_call():
    """Calling update_claim_statuses twice with changed status must reflect the update."""
    conn = _conn()
    init_db(conn)
    state = _make_state()
    update_claim_statuses(conn, state)

    # Mutate status and write again
    state.claims["claim-001"].status = "challenged"
    update_claim_statuses(conn, state)

    row = conn.execute(
        "SELECT status FROM claims WHERE claim_id=?", ("claim-001",)
    ).fetchone()
    assert row[0] == "challenged"


# ---------------------------------------------------------------------------
# Filesystem outputs
# ---------------------------------------------------------------------------

def test_write_state_json_creates_file():
    """write_state_json must create state.json with correct run_id (formerly session_id)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        state = _make_state()
        write_state_json(state, run_dir)

        path = run_dir / "state.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["run_id"] == "sess-001"
        assert data["topic"] == "AI economics"


def test_write_state_json_overwrite():
    """Calling write_state_json twice must overwrite, not append."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        state = _make_state()
        write_state_json(state, run_dir)
        state.turn = 99
        write_state_json(state, run_dir)

        data = json.loads((run_dir / "state.json").read_text())
        assert data["turn"] == 99


def test_append_act_to_markdown():
    """append_act_to_markdown must create debate.md and include act content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        act = _make_act()
        append_act_to_markdown(act, run_dir)

        md_path = run_dir / "debate.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "ASSERT" in content
        assert "AI will fundamentally transform" in content
        assert "Thesis" in content


def test_append_act_to_markdown_is_cumulative():
    """Multiple append calls must accumulate content, not overwrite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        act1 = _make_act()
        act2 = _make_act()
        act2.act_id = "act-002"
        act2.act_type = "CHALLENGE"
        act2.content = "This claim lacks empirical support."
        act2.agent = "Antithesis"

        append_act_to_markdown(act1, run_dir)
        append_act_to_markdown(act2, run_dir)

        content = (run_dir / "debate.md").read_text()
        assert "ASSERT" in content
        assert "CHALLENGE" in content
        assert "empirical support" in content


def test_export_markdown_full():
    """export_markdown must create debate.md with header, claims, and act log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        state = _make_state()
        export_markdown(state, run_dir)

        content = (run_dir / "debate.md").read_text()
        assert "Test Debate" in content
        assert "AI economics" in content
        assert "AI will fundamentally transform" in content
        assert "ASSERT" in content
