"""Tests for agents/base.py — JSON parser, allowlist, rolling history."""
import json
import uuid
import pytest
from datetime import datetime

from agents.base import BaseAgent
from core.state import Act, ActType, DialogueState, TokenUsage


# ------------------------------------------------------------------
# Minimal concrete agent for testing BaseAgent directly
# ------------------------------------------------------------------

class _FakeAgent(BaseAgent):
    def __init__(self, role="proposition"):
        super().__init__(role=role, nickname="Test", model="gpt-4o",
                         temperature=0.5, config={})

    def _build_prompt(self, state):
        return "system", "user"


def _state(acts=None) -> DialogueState:
    now = datetime.utcnow().isoformat()
    return DialogueState(
        session_id="s1", turn=0, phase="init",
        claims={}, acts=acts or [],
        outstanding_challenges=[], next_agent="proposition",
        legal_acts=["ASSERT"],
        token_usage={"proposition": TokenUsage(), "opposition": TokenUsage(),
                     "moderator": TokenUsage(), "synthesiser": TokenUsage()},
        debate_title="T", topic="T", config={},
        created_at=now, closed_at=None, closure_reason=None,
    )


def _act(turn=0, act_type="ASSERT", content="c", agent_role="proposition") -> Act:
    return Act(
        act_id=str(uuid.uuid4()), session_id="s1", turn=turn,
        agent="Thesis", agent_role=agent_role, act_type=act_type,
        claim_id=None, target_act_id=None, content=content, reason="r",
        input_tokens=1, output_tokens=1, model_used="m",
        timestamp=datetime.utcnow().isoformat(),
    )


# ------------------------------------------------------------------
# _strip_and_parse
# ------------------------------------------------------------------

class TestStripAndParse:
    def test_bare_json(self):
        raw = '{"act_type": "ASSERT", "content": "hello"}'
        data = BaseAgent._strip_and_parse(raw)
        assert data["act_type"] == "ASSERT"

    def test_json_fenced(self):
        raw = '```json\n{"act_type": "ASSERT"}\n```'
        data = BaseAgent._strip_and_parse(raw)
        assert data["act_type"] == "ASSERT"

    def test_fenced_no_lang(self):
        raw = '```\n{"act_type": "ASSERT"}\n```'
        data = BaseAgent._strip_and_parse(raw)
        assert data["act_type"] == "ASSERT"

    def test_leading_trailing_whitespace(self):
        raw = '  \n  {"act_type": "CLOSE"}  \n  '
        data = BaseAgent._strip_and_parse(raw)
        assert data["act_type"] == "CLOSE"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            BaseAgent._strip_and_parse("not json at all")


# ------------------------------------------------------------------
# _parse_response — allowlist
# ------------------------------------------------------------------

class TestAllowlist:
    def _parse(self, role, act_type):
        agent = _FakeAgent(role=role)
        raw = json.dumps({"act_type": act_type, "content": "x"})
        return agent._parse_response(raw, _state(), 1, 1)

    def test_proposition_assert_allowed(self):
        act = self._parse("proposition", "ASSERT")
        assert act.act_type == ActType.ASSERT

    def test_proposition_challenge_forbidden(self):
        with pytest.raises(ValueError, match="forbidden act_type"):
            self._parse("proposition", "CHALLENGE")

    def test_opposition_challenge_allowed(self):
        act = self._parse("opposition", "CHALLENGE")
        assert act.act_type == ActType.CHALLENGE

    def test_opposition_assert_forbidden(self):
        with pytest.raises(ValueError, match="forbidden act_type"):
            self._parse("opposition", "ASSERT")

    def test_moderator_status_allowed(self):
        act = self._parse("moderator", "STATUS")
        assert act.act_type == ActType.STATUS

    def test_moderator_assert_forbidden(self):
        with pytest.raises(ValueError, match="forbidden act_type"):
            self._parse("moderator", "ASSERT")

    def test_synthesiser_argument_map_allowed(self):
        act = self._parse("synthesiser", "ARGUMENT_MAP")
        assert act.act_type == ActType.ARGUMENT_MAP

    def test_content_truncated_at_3000(self):
        agent = _FakeAgent()
        raw = json.dumps({"act_type": "ASSERT", "content": "x" * 5000})
        act = agent._parse_response(raw, _state(), 1, 1)
        assert len(act.content) == 3000

    def test_target_claim_id_normalised(self):
        agent = _FakeAgent(role="opposition")
        raw = json.dumps({"act_type": "CHALLENGE", "target_claim_id": "cid-123", "content": "x"})
        act = agent._parse_response(raw, _state(), 1, 1)
        assert act.claim_id == "cid-123"


# ------------------------------------------------------------------
# _format_act_history — rolling window
# ------------------------------------------------------------------

class TestRollingHistory:
    def test_empty_state(self):
        agent = _FakeAgent()
        result = agent._format_act_history(_state())
        assert result == "(no acts yet)"

    def test_within_window_no_omission_header(self):
        agent = _FakeAgent()
        acts = [_act(turn=i) for i in range(5)]
        state = _state(acts=acts)
        result = agent._format_act_history(state)
        assert "omitted" not in result
        assert "Turn 4" in result

    def test_beyond_window_adds_summary(self):
        agent = _FakeAgent()
        acts = [_act(turn=i) for i in range(15)]
        state = _state(acts=acts)
        result = agent._format_act_history(state)
        assert "omitted" in result
        assert "5 earlier act" in result  # 15 - 10 = 5 omitted

    def test_beyond_window_shows_recent_acts(self):
        agent = _FakeAgent()
        acts = [_act(turn=i, content=f"content-{i}") for i in range(15)]
        state = _state(acts=acts)
        result = agent._format_act_history(state)
        # Last 10 acts (turns 5-14) should be visible
        assert "Turn 14" in result
        assert "Turn 4" not in result
