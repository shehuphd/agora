"""A debater failure closes the run through the same path as any close:
moderator where possible, synthesiser always.

Before this fix the runner's debater error handlers broke straight out of
the turn loop, so a run killed by a provider failure got no CLOSE act and no
argument map — while the same failure on the moderator's side fell through
to synthesis. Observed live 2026-09-05: a quota-killed run got its map, a
timeout-killed run didn't, for no reason a reader of either could see.
"""
import asyncio
import sqlite3
import uuid
from datetime import datetime

import pytest

from core import runs_db as _runs_db
from core.checkpoint import init_db
from core.config import AgentRunConfig, DebateRunConfig, ProtocolRunConfig
from core.state import Act, DialogueState, TokenUsage
from keycall import ErrorCode, KeyCallError
from runners.debate import TurnOrchestrator


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(_runs_db, "DATABASES_DIR", tmp_path / "databases")
    monkeypatch.setattr(_runs_db, "RUNS_DB_PATH", tmp_path / "databases" / "runs.db")
    yield


class _FakeAgent:
    def __init__(self, role, act_type=None, exc=None):
        self.role = role
        self.nickname = role.title()
        self.model = "fake-model"
        self._provider = "openai"
        self._token_budget = 0
        self.exc = exc
        self.act_type = act_type
        self.calls = 0

    def _act(self, state, reason=None):
        return Act(
            act_id=str(uuid.uuid4()), run_id=state.run_id, turn=state.turn,
            agent=self.nickname, agent_role=self.role, act_type=self.act_type,
            claim_id=None, target_act_id=None, content=f"{self.role} output",
            reason=reason, input_tokens=10, output_tokens=5,
            model_used=self.model, timestamp=datetime.utcnow().isoformat(),
        )

    def generate(self, state, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        # The production moderator echoes the runner's closure_reason into its
        # CLOSE act; mirror that so apply_act records the same reason.
        return self._act(state, reason=kwargs.get("closure_reason"))


def _orchestrator(tmp_path, prop_exc, unattended=True):
    state = DialogueState(
        run_id="run-parity", turn=0, phase="init", claims={}, acts=[],
        outstanding_challenges=[], next_agent="proposition", legal_acts=[],
        token_usage={r: TokenUsage() for r in
                     ("proposition", "opposition", "moderator", "synthesiser")},
        debate_title="t", topic="t", config={},
        created_at=datetime.utcnow().isoformat(),
        closed_at=None, closure_reason=None,
    )
    agent_cfg = AgentRunConfig(model="fake-model", provider="openai",
                               temperature=0.5, nickname="Fake")
    config = DebateRunConfig(
        topic="t", debate_title="t",
        proposition=agent_cfg, opposition=agent_cfg,
        moderator=agent_cfg, synthesiser=agent_cfg,
        protocol=ProtocolRunConfig(max_turns=5, token_budget=40_000),
    )
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    prop = _FakeAgent("proposition", act_type="ASSERT", exc=prop_exc)
    opp = _FakeAgent("opposition", act_type="CHALLENGE")
    mod = _FakeAgent("moderator", act_type="CLOSE")
    synth = _FakeAgent("synthesiser", act_type="ARGUMENT_MAP")
    def build():
        # TurnOrchestrator.__init__ needs a running event loop.
        return TurnOrchestrator(
            state=state, agents=[prop, opp], moderator=mod, synthesiser=synth,
            conn=conn, event_queue=asyncio.Queue(), run_dir=tmp_path,
            config=config, unattended=unattended,
        )
    return build, mod, synth


def _run(build):
    holder = {}

    async def scenario():
        orch = build()
        holder["orch"] = orch
        await orch.run()

    asyncio.run(scenario())
    return holder["orch"]


class TestDebaterFailureStillSynthesises:
    @pytest.mark.parametrize("exc", [
        KeyCallError("read timeout", code=ErrorCode.TIMEOUT,
                     provider="moonshot", retryable=True),
        KeyCallError("spend limit", code=ErrorCode.PERMISSION_DENIED,
                     provider="anthropic"),
        RuntimeError("anything else"),
    ])
    def test_map_and_close_recorded(self, tmp_path, exc):
        build, mod, synth = _orchestrator(tmp_path, prop_exc=exc)
        orch = _run(build)
        assert mod.calls == 1, "moderator should still get to close the run"
        assert synth.calls == 1, "synthesiser should still map the record"
        acts = [a.act_type for a in orch.state.acts]
        assert "CLOSE" in acts
        assert "ARGUMENT_MAP" in acts
        assert orch._failed is True

    def test_status_stays_error_with_the_failure_reason(self, tmp_path):
        exc = KeyCallError("read timeout", code=ErrorCode.TIMEOUT,
                           provider="moonshot", retryable=True)
        build, mod, _ = _orchestrator(tmp_path, prop_exc=exc)
        orch = _run(build)
        # The failure reason reaches the moderator's close and the record.
        assert mod.last_kwargs.get("closure_reason") == "proposition_error"
        assert orch.state.closure_reason == "proposition_error"


class TestCleanRunStillWorks:
    def test_no_failure_no_error_status(self, tmp_path):
        build, mod, synth = _orchestrator(tmp_path, prop_exc=None)
        # Moderator closes on its first turn (CLOSE act), so one debater
        # turn then an orderly close with a map.
        orch = _run(build)
        assert orch._failed is False
        assert synth.calls == 1
        assert "ARGUMENT_MAP" in [a.act_type for a in orch.state.acts]
