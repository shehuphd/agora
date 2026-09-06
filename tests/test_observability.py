"""Observability of what runs: prompt bodies in traces, chapters in state.json,
and auxiliary calls (chapter and epoch summaries) recorded in tokens and cost.

Motivated by the 2026-09-05 growth-run investigation, where nine chapter
summary calls were provable only by inter-turn timing: state.json omitted the
chapters field, the calls were untraced, and their tokens and cost appeared
nowhere.
"""
import asyncio
import json
import uuid
from datetime import datetime

from core.checkpoint import write_state_json
from core.state import Act, DialogueState, TokenUsage


def _state(**kwargs) -> DialogueState:
    base = dict(
        run_id="run-o", turn=10, phase="assert", claims={}, acts=[],
        outstanding_challenges=[], next_agent="proposition",
        legal_acts=["ASSERT"],
        token_usage={r: TokenUsage() for r in
                     ("proposition", "opposition", "moderator", "synthesiser")},
        debate_title="t", topic="Motion", config={},
        created_at=datetime.utcnow().isoformat(),
        closed_at=None, closure_reason=None,
    )
    base.update(kwargs)
    return DialogueState(**base)


def _act(turn, role, act_type, content="c", cost=None):
    return Act(
        act_id=str(uuid.uuid4()), run_id="run-o", turn=turn, agent=role.title(),
        agent_role=role, act_type=act_type, claim_id=None, target_act_id=None,
        content=content, reason=None, input_tokens=10, output_tokens=5,
        model_used="m", timestamp=datetime.utcnow().isoformat(), cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# state.json carries chapters and the auxiliary ledger
# ---------------------------------------------------------------------------

class TestStateJson:
    def test_chapters_lapsed_and_aux_cost_are_persisted(self, tmp_path):
        state = _state()
        state.chapters = ["[Turns 1-10] chapter one"]
        state.lapsed_challenges = ["ch-000"]
        state.aux_cost_usd = 0.0123
        write_state_json(state, tmp_path)
        data = json.loads((tmp_path / "state.json").read_text())
        assert data["chapters"] == ["[Turns 1-10] chapter one"]
        assert data["lapsed_challenges"] == ["ch-000"]
        assert data["aux_cost_usd"] == 0.0123


# ---------------------------------------------------------------------------
# Prompt bodies reach the trace
# ---------------------------------------------------------------------------

class _TraceRecorder:
    """Stand-in for traceact.ActionTrace that records what was traced."""
    instances: list["_TraceRecorder"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.inputs: list[dict] = []
        self.steps: list[str] = []
        self.outputs: list[dict] = []
        self.models: list[dict] = []

    @classmethod
    def start(cls, **kwargs):
        inst = cls(**kwargs)
        cls.instances.append(inst)
        return inst

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def input(self, data):
        self.inputs.append(data)

    def step(self, msg):
        self.steps.append(msg)

    def output(self, data):
        self.outputs.append(data)

    def model(self, **kwargs):
        self.models.append(kwargs)


class TestPromptBodiesInTraces:
    def test_traced_generate_records_system_and_user(self, monkeypatch):
        from agents import base as _base
        from agents.proposition import PropositionAgent

        _TraceRecorder.instances = []
        monkeypatch.setattr(_base, "ActionTrace", _TraceRecorder)
        agent = PropositionAgent(provider="openai")
        monkeypatch.setattr(agent, "_call_provider", lambda s, u, max_tokens=None: (
            json.dumps({"act_type": "ASSERT", "claim_id": None,
                        "target_act_id": None, "content": "a claim",
                        "reason": "r", "citations": []}),
            100, 50,
        ))
        state = _state()
        act = agent._traced_generate(state, "SYSTEM PROMPT BODY", "USER PROMPT BODY")
        assert act.act_type == "ASSERT"
        trace = _TraceRecorder.instances[0]
        assert {"system": "SYSTEM PROMPT BODY", "user": "USER PROMPT BODY"} in trace.inputs
        # The raw model response is recorded too.
        assert any("response_raw" in o for o in trace.outputs)


# ---------------------------------------------------------------------------
# Auxiliary calls: traced, tokens on state, cost on state
# ---------------------------------------------------------------------------

def _synth(monkeypatch, text="summary text", cost=0.002):
    from agents import synthesiser as _synth_mod
    from agents.synthesiser import SynthesiserAgent

    agent = SynthesiserAgent(provider="openai")
    monkeypatch.setattr(agent, "_call_provider",
                        lambda s, u, max_tokens=None: (text, 200, 80))
    monkeypatch.setattr(_synth_mod._cost, "cost_usd",
                        lambda *a, **k: cost)
    return agent


class TestAuxiliaryCallAccounting:
    def test_chapter_summary_bills_tokens_and_cost_to_state(self, monkeypatch):
        agent = _synth(monkeypatch)
        state = _state()
        state.acts = [_act(t, "proposition", "ASSERT") for t in range(1, 11)]
        summary = agent.summarise_chapter(state, 1, 10)
        assert summary.startswith("[Turns 1-10]")
        usage = state.token_usage["synthesiser"]
        assert (usage.input_tokens, usage.output_tokens) == (200, 80)
        assert state.aux_cost_usd == 0.002

    def test_chapter_summary_is_traced_with_prompt(self, monkeypatch):
        import traceact as _traceact
        _TraceRecorder.instances = []
        monkeypatch.setattr(_traceact, "ActionTrace", _TraceRecorder)
        agent = _synth(monkeypatch)
        state = _state()
        state.acts = [_act(t, "proposition", "ASSERT") for t in range(1, 11)]
        agent.summarise_chapter(state, 1, 10)
        traces = [t for t in _TraceRecorder.instances
                  if t.kwargs.get("action") == "synthesiser.chapter"]
        assert len(traces) == 1
        assert traces[0].inputs and "system" in traces[0].inputs[0]
        assert traces[0].models and traces[0].models[0]["tokens_in"] == 200

    def test_failed_chapter_call_is_traced_not_raised(self, monkeypatch):
        import traceact as _traceact
        _TraceRecorder.instances = []
        monkeypatch.setattr(_traceact, "ActionTrace", _TraceRecorder)
        agent = _synth(monkeypatch)

        def boom(s, u, max_tokens=None):
            raise RuntimeError("provider down")

        monkeypatch.setattr(agent, "_call_provider", boom)
        state = _state()
        state.acts = [_act(1, "proposition", "ASSERT")]
        assert agent.summarise_chapter(state, 1, 10) == ""
        trace = _TraceRecorder.instances[0]
        assert any("failed" in s for s in trace.steps)
        assert any("error" in o for o in trace.outputs)

    def test_epoch_summary_bills_the_same_way(self, monkeypatch):
        agent = _synth(monkeypatch, text="epoch text")
        state = _state()
        out = agent.summarise_epoch(state, ["[Turns 1-10] a", "[Turns 11-20] b"])
        assert out == "[Turns 1-20] epoch text"
        assert state.token_usage["synthesiser"].input_tokens == 200
        assert state.aux_cost_usd == 0.002


# ---------------------------------------------------------------------------
# Run cost total includes the auxiliary ledger
# ---------------------------------------------------------------------------

class TestFinalCostIncludesAux:
    def _orch(self, state):
        from runners import debate as _debate
        orch = object.__new__(_debate.TurnOrchestrator)
        orch.state = state
        return orch

    def test_aux_cost_added_to_total(self):
        state = _state()
        state.acts = [_act(1, "proposition", "ASSERT", cost=0.01)]
        state.aux_cost_usd = 0.005
        total, partial = self._orch(state)._final_cost()
        assert total == 0.015
        assert partial is False

    def test_aux_cost_alone_still_prices_the_run(self):
        state = _state()
        state.aux_cost_usd = 0.005
        total, partial = self._orch(state)._final_cost()
        assert total == 0.005
