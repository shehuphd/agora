"""Tests for core/grammar.py act validation."""
import pytest
from core.state import DialogueState, TokenUsage
from core.grammar import validate_act


def _make_state(phase: str, steelman_mode: bool = False) -> DialogueState:
    """Build a minimal DialogueState for grammar testing without LLM calls."""
    return DialogueState(
        run_id="test",
        turn=1,
        phase=phase,
        claims={},
        acts=[],
        outstanding_challenges=[],
        next_agent="proposition",
        legal_acts=[],
        token_usage={
            "proposition": TokenUsage(),
            "opposition": TokenUsage(),
            "moderator": TokenUsage(),
            "synthesiser": TokenUsage(),
        },
        debate_title="Test Debate",
        topic="Test topic",
        config={},
        created_at="2025-01-01T00:00:00",
        closed_at=None,
        closure_reason=None,
        steelman_mode=steelman_mode,
    )


# --- Legal transitions ---

def test_legal_assert_to_challenge():
    """CHALLENGE is a legal successor to ASSERT phase."""
    state = _make_state("assert")
    validate_act(state, "CHALLENGE")  # must not raise


def test_legal_assert_to_concede():
    """CONCEDE is a legal successor to ASSERT phase."""
    state = _make_state("assert")
    validate_act(state, "CONCEDE")


def test_legal_challenge_to_revise():
    """REVISE is a legal successor to CHALLENGE phase."""
    state = _make_state("challenge")
    validate_act(state, "REVISE")


def test_legal_challenge_to_defend():
    """DEFEND is a legal successor to CHALLENGE phase."""
    state = _make_state("challenge")
    validate_act(state, "DEFEND")


def test_legal_challenge_to_concede():
    """CONCEDE is a legal successor to CHALLENGE phase."""
    state = _make_state("challenge")
    validate_act(state, "CONCEDE")


def test_legal_revise_to_challenge():
    """CHALLENGE is a legal successor to REVISE phase."""
    state = _make_state("revise")
    validate_act(state, "CHALLENGE")


def test_legal_revise_to_propose():
    """PROPOSE is a legal successor to REVISE phase."""
    state = _make_state("revise")
    validate_act(state, "PROPOSE")


def test_legal_defend_to_challenge():
    """CHALLENGE is a legal successor to DEFEND phase."""
    state = _make_state("defend")
    validate_act(state, "CHALLENGE")


def test_legal_concede_to_assert():
    """ASSERT is a legal successor to CONCEDE phase."""
    state = _make_state("concede")
    validate_act(state, "ASSERT")


def test_legal_concede_to_propose():
    """PROPOSE is a legal successor to CONCEDE phase."""
    state = _make_state("concede")
    validate_act(state, "PROPOSE")


# --- Illegal transitions ---

def test_illegal_assert_after_assert():
    """ASSERT cannot follow ASSERT phase — proposition must wait for a response."""
    state = _make_state("assert")
    with pytest.raises(ValueError):
        validate_act(state, "ASSERT")


def test_legal_assert_after_challenge():
    """ASSERT is now legal after CHALLENGE — Proposition may open new fronts while
    challenges are outstanding rather than being bottlenecked on a single claim."""
    state = _make_state("challenge")
    validate_act(state, "ASSERT")  # must not raise


def test_illegal_challenge_after_concede():
    """CHALLENGE cannot follow CONCEDE phase."""
    state = _make_state("concede")
    with pytest.raises(ValueError):
        validate_act(state, "CHALLENGE")


def test_illegal_revise_after_assert():
    """REVISE cannot follow ASSERT phase — nothing to revise yet."""
    state = _make_state("assert")
    with pytest.raises(ValueError):
        validate_act(state, "REVISE")


# --- Always-legal acts ---

def test_status_always_legal():
    """STATUS is always legal regardless of phase."""
    for phase in ("assert", "challenge", "revise", "defend", "concede", "propose"):
        state = _make_state(phase)
        validate_act(state, "STATUS")  # must not raise for any phase


def test_close_always_legal():
    """CLOSE is always legal regardless of phase."""
    for phase in ("assert", "challenge", "revise", "defend"):
        state = _make_state(phase)
        validate_act(state, "CLOSE")  # must not raise


# --- Closed state ---

def test_closed_state_blocks_normal_acts():
    """Any non-moderator act raises ValueError when state is already closed."""
    state = _make_state("closed")
    with pytest.raises(ValueError, match="closed"):
        validate_act(state, "ASSERT")


def test_closed_state_still_allows_status():
    """STATUS remains legal even after debate is closed (Moderator use)."""
    state = _make_state("closed")
    validate_act(state, "STATUS")  # must not raise


# --- PROPOSE terminal check ---

# --- Rapoport / steelman mode ---

def test_rapoport_assert_to_steelman_legal():
    """ASSERT → STEELMAN is legal in Rapoport mode."""
    state = _make_state("assert", steelman_mode=True)
    validate_act(state, "STEELMAN")  # must not raise


def test_rapoport_assert_to_challenge_illegal():
    """ASSERT → CHALLENGE is illegal in Rapoport mode (must steelman first)."""
    state = _make_state("assert", steelman_mode=True)
    with pytest.raises(ValueError):
        validate_act(state, "CHALLENGE")


def test_rapoport_steelman_to_accept_legal():
    """STEELMAN → ACCEPT_STEELMAN is legal."""
    state = _make_state("steelman", steelman_mode=True)
    validate_act(state, "ACCEPT_STEELMAN")


def test_rapoport_steelman_to_reject_legal():
    """STEELMAN → REJECT_STEELMAN is legal."""
    state = _make_state("steelman", steelman_mode=True)
    validate_act(state, "REJECT_STEELMAN")


def test_rapoport_reject_to_steelman_legal():
    """REJECT_STEELMAN → STEELMAN is legal (Opposition must restate)."""
    state = _make_state("reject_steelman", steelman_mode=True)
    validate_act(state, "STEELMAN")


def test_rapoport_accept_to_challenge_legal():
    """ACCEPT_STEELMAN → CHALLENGE is legal."""
    state = _make_state("accept_steelman", steelman_mode=True)
    validate_act(state, "CHALLENGE")


def test_rapoport_accept_to_concede_legal():
    """ACCEPT_STEELMAN → CONCEDE is also legal (standard path after acceptance)."""
    state = _make_state("accept_steelman", steelman_mode=True)
    validate_act(state, "CONCEDE")


def test_moderator_intervention_always_legal():
    """MODERATOR_INTERVENTION is always legal regardless of phase or mode."""
    for phase in ("assert", "challenge", "revise", "defend", "concede", "propose",
                  "steelman", "accept_steelman", "reject_steelman"):
        for steelman in (False, True):
            state = _make_state(phase, steelman_mode=steelman)
            validate_act(state, "MODERATOR_INTERVENTION")  # must not raise


def test_propose_has_no_non_moderator_successors():
    """After PROPOSE phase neither ASSERT nor CHALLENGE is legal — only moderator acts."""
    state = _make_state("propose")
    with pytest.raises(ValueError):
        validate_act(state, "ASSERT")
    with pytest.raises(ValueError):
        validate_act(state, "CHALLENGE")
    with pytest.raises(ValueError):
        validate_act(state, "REVISE")
