"""Tests for TurnOrchestrator._final_cost() — the per-run cost total that
feeds core.runs_db.update_on_close.

The cost layer is additive by design: a run with
an unpriced model, or with rates missing entirely, must still close
normally with a sane (None, True) or (partial_total, True) result — never
raise. These tests exercise _final_cost() directly against a bare state,
without constructing the full TurnOrchestrator (its __init__ needs a live
event loop, DB connection, and running agents unrelated to this method).
"""
from datetime import datetime

from core.state import Act, DialogueState
from runners.debate import TurnOrchestrator


def _orchestrator_with_acts(acts: list[Act]) -> TurnOrchestrator:
    orch = object.__new__(TurnOrchestrator)  # skip __init__: needs a running loop
    orch.state = DialogueState(
        run_id="r1", turn=len(acts), phase="closed", claims={}, acts=acts,
        outstanding_challenges=[], next_agent="proposition", legal_acts=[],
        token_usage={}, debate_title="t", topic="t", config={},
        created_at=datetime.utcnow().isoformat(), closed_at=None,
        closure_reason="max_turns",
    )
    return orch


def _act(role="proposition", model="gpt-4.1", cost_usd=None, turn=0) -> Act:
    return Act(
        act_id=f"a{turn}", run_id="r1", turn=turn, agent="T", agent_role=role,
        act_type="ASSERT", claim_id=None, target_act_id=None, content="x",
        reason=None, input_tokens=100, output_tokens=50, model_used=model,
        timestamp=datetime.utcnow().isoformat(), cost_usd=cost_usd,
    )


class TestFinalCost:
    def test_all_acts_priced_sums_exactly(self):
        orch = _orchestrator_with_acts([_act(cost_usd=0.5), _act(cost_usd=0.25, turn=1)])
        total, partial = orch._final_cost()
        assert total == 0.75
        assert partial is False

    def test_no_acts_priced_returns_none_not_zero(self):
        """An unpriceable run reports 'unknown', never a misleading $0.00."""
        orch = _orchestrator_with_acts([_act(cost_usd=None), _act(cost_usd=None, turn=1)])
        total, partial = orch._final_cost()
        assert total is None
        assert partial is True

    def test_mixed_priced_and_unpriced_sums_known_and_flags_partial(self):
        """One role's model couldn't be priced (e.g. rates has no entry for
        it) — the run must still close, with a partial total that says so
        rather than silently dropping that role's cost to zero."""
        orch = _orchestrator_with_acts([
            _act(role="proposition", cost_usd=1.0),
            _act(role="opposition", cost_usd=None, turn=1),  # e.g. an unpriced model
        ])
        total, partial = orch._final_cost()
        assert total == 1.0
        assert partial is True

    def test_no_acts_at_all_returns_none(self):
        """A run that closes before any act completed (e.g. an immediate
        key failure) must not crash update_on_close."""
        orch = _orchestrator_with_acts([])
        total, partial = orch._final_cost()
        assert total is None
        assert partial is False


class TestActConstructionNeverRaisesOnUnpriceableModel:
    """agents/base.py computes cost_usd inline when building each Act. A
    model or provider core.cost can't price must degrade to cost_usd=None,
    never raise and never block the act from being recorded."""

    def test_unknown_model_produces_none_cost_not_an_exception(self):
        from core import cost
        assert cost.cost_usd("openai", "a-model-that-will-never-exist", 100, 50) is None

    def test_act_with_no_price_is_still_constructible(self):
        act = _act(cost_usd=None)
        assert act.cost_usd is None
        assert act.input_tokens == 100  # the rest of the act is unaffected
