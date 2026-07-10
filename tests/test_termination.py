"""Tests for core/termination.py check_termination function — no LLM calls."""
import pytest
from datetime import datetime, timedelta
from core.state import DialogueState, TokenUsage, Claim
from core.termination import check_termination


def _base_config() -> dict:
    """Minimal valid config dict for termination checks."""
    return {
        "protocol": {
            "max_turns": 8,
            "max_time_minutes": 15,
            "token_budget": 100000,
            "min_challenges": 2,
            "repetition_tolerance": 1,
        }
    }


def _make_state(**overrides) -> DialogueState:
    """Build a mid-debate DialogueState with sensible defaults, overridable via kwargs."""
    defaults = dict(
        session_id="test",
        turn=2,
        phase="challenge",
        claims={},
        acts=[],
        outstanding_challenges=["a1", "a2"],
        next_agent="proposition",
        legal_acts=["REVISE", "DEFEND", "CONCEDE"],
        token_usage={
            "proposition": TokenUsage(input_tokens=100, output_tokens=100),
            "opposition":  TokenUsage(input_tokens=100, output_tokens=100),
            "moderator":   TokenUsage(input_tokens=50,  output_tokens=50),
            "synthesiser": TokenUsage(),
        },
        debate_title="Test Debate",
        topic="Test topic",
        config={},
        created_at=datetime.utcnow().isoformat(),
        closed_at=None,
        closure_reason=None,
    )
    defaults.update(overrides)
    return DialogueState(**defaults)


# --- Hard stops ---

def test_max_turns_triggers_close():
    """Hard stop: debate must close when turn count equals max_turns."""
    state = _make_state(turn=8)
    should_close, reason = check_termination(state, _base_config())
    assert should_close is True
    assert "max_turns" in reason


def test_turn_below_max_does_not_close():
    """No closure when turn count is below max_turns."""
    state = _make_state(turn=4)
    should_close, _ = check_termination(state, _base_config())
    assert should_close is False


def test_token_budget_exhausted():
    """Hard stop: total tokens across all roles exceeds budget."""
    state = _make_state(token_usage={
        "proposition": TokenUsage(input_tokens=40000, output_tokens=30000),
        "opposition":  TokenUsage(input_tokens=20000, output_tokens=15000),
        "moderator":   TokenUsage(input_tokens=5000,  output_tokens=5000),
        "synthesiser": TokenUsage(),
    })
    should_close, reason = check_termination(state, _base_config())
    assert should_close is True
    assert "token" in reason.lower()


def test_token_budget_not_exhausted():
    """No closure when token usage is safely below budget."""
    state = _make_state()  # uses tiny default token counts
    should_close, _ = check_termination(state, _base_config())
    assert should_close is False


def test_all_claims_resolved_does_not_close():
    """Empty outstanding_challenges with all-terminal claims is NOT a stop condition.
    Debates run until token/turn budget — a concession clears a challenge, not the debate."""
    now = datetime.utcnow().isoformat()
    claims = {
        "c1": Claim("c1", "test", "proposition", "Claim one",   "conceded", now),
        "c2": Claim("c2", "test", "proposition", "Claim two",   "survived", now),
        "c3": Claim("c3", "test", "opposition",  "Claim three", "contested", now),
    }
    state = _make_state(claims=claims, outstanding_challenges=[])
    should_close, _ = check_termination(state, _base_config())
    assert should_close is False  # no budget/turn limits hit — debate continues


# --- Soft stops ---

def test_repetition_detection():
    """Soft stop: claim with same normalised text as a revised claim triggers close."""
    now = datetime.utcnow().isoformat()
    claims = {
        "c1": Claim("c1", "test", "proposition", "AI is transformative",   "revised", now),
        "c2": Claim("c2", "test", "proposition", "AI  is  transformative", "open",    now),
    }
    # outstanding_challenges must have 2+ entries to avoid min_challenges soft stop first
    state = _make_state(claims=claims, outstanding_challenges=["x1", "x2"])
    should_close, reason = check_termination(state, _base_config())
    assert should_close is True
    assert "repetit" in reason.lower()


def test_no_repetition_different_content():
    """No repetition closure when open and revised claims have distinct content."""
    now = datetime.utcnow().isoformat()
    claims = {
        "c1": Claim("c1", "test", "proposition", "AI is transformative", "revised", now),
        "c2": Claim("c2", "test", "proposition", "AI increases inequality", "open",  now),
    }
    state = _make_state(claims=claims, outstanding_challenges=["x1", "x2"])
    should_close, _ = check_termination(state, _base_config())
    assert should_close is False


# --- Normal mid-debate state ---

def test_no_stop_normal_midpoint():
    """Normal mid-debate state should return (False, None)."""
    state = _make_state(turn=2)
    should_close, reason = check_termination(state, _base_config())
    assert should_close is False
    assert reason is None
