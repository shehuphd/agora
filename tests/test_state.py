"""Tests for core/state.py — apply_act mutations and ActType enum."""
import uuid
import pytest
from datetime import datetime

from core.state import (
    ActType, Act, Claim, DialogueState, TokenUsage,
    apply_act, legal_acts_for,
)


def _state(**kwargs) -> DialogueState:
    now = datetime.utcnow().isoformat()
    defaults = dict(
        session_id="test-session",
        turn=0,
        phase="init",
        claims={},
        acts=[],
        outstanding_challenges=[],
        next_agent="proposition",
        legal_acts=["ASSERT"],
        token_usage={
            "proposition": TokenUsage(),
            "opposition":  TokenUsage(),
            "moderator":   TokenUsage(),
            "synthesiser": TokenUsage(),
        },
        debate_title="Test debate",
        topic="Test topic",
        config={},
        created_at=now,
        closed_at=None,
        closure_reason=None,
        steelman_mode=False,
    )
    defaults.update(kwargs)
    return DialogueState(**defaults)


def _act(act_type: str, claim_id: str = None, target_act_id: str = None,
         agent_role: str = "proposition", content: str = "test content") -> Act:
    return Act(
        act_id=str(uuid.uuid4()),
        session_id="test-session",
        turn=0,
        agent="Thesis",
        agent_role=agent_role,
        act_type=act_type,
        claim_id=claim_id,
        target_act_id=target_act_id,
        content=content,
        reason="test reason",
        input_tokens=10,
        output_tokens=5,
        model_used="test-model",
        timestamp=datetime.utcnow().isoformat(),
    )


class TestActTypeEnum:
    def test_str_equality(self):
        assert ActType.ASSERT == "ASSERT"
        assert ActType.CHALLENGE == "CHALLENGE"

    def test_set_membership(self):
        allowed = {"ASSERT", "REVISE"}
        assert ActType.ASSERT in allowed

    def test_all_values_unique(self):
        values = [m.value for m in ActType]
        assert len(values) == len(set(values))


class TestApplyActAssert:
    def test_assert_creates_claim_with_server_uuid(self):
        state = _state()
        model_supplied_id = "model-wants-this-id"
        act = _act(ActType.ASSERT, claim_id=model_supplied_id)
        apply_act(state, act)

        # Server must override the model-supplied ID with a fresh UUID
        assert act.claim_id != model_supplied_id
        assert act.claim_id in state.claims
        claim = state.claims[act.claim_id]
        assert claim.status == "open"
        assert claim.author == "proposition"

    def test_assert_advances_turn(self):
        state = _state()
        apply_act(state, _act(ActType.ASSERT))
        assert state.turn == 1

    def test_assert_sets_phase(self):
        state = _state()
        apply_act(state, _act(ActType.ASSERT))
        assert state.phase == "assert"

    def test_two_asserts_get_different_claim_ids(self):
        state = _state()
        a1 = _act(ActType.ASSERT, claim_id="same-id")
        a2 = _act(ActType.ASSERT, claim_id="same-id")
        apply_act(state, a1)
        apply_act(state, a2)
        assert a1.claim_id != a2.claim_id
        assert len(state.claims) == 2


class TestApplyActChallenge:
    def setup_method(self):
        self.state = _state()
        assert_act = _act(ActType.ASSERT)
        apply_act(self.state, assert_act)
        self.claim_id = assert_act.claim_id

    def test_challenge_marks_claim_challenged(self):
        ch = _act(ActType.CHALLENGE, claim_id=self.claim_id, agent_role="opposition")
        apply_act(self.state, ch)
        assert self.state.claims[self.claim_id].status == "challenged"
        assert ch.act_id in self.state.outstanding_challenges

    def test_challenge_advances_turn(self):
        turn_before = self.state.turn
        apply_act(self.state, _act(ActType.CHALLENGE, claim_id=self.claim_id, agent_role="opposition"))
        assert self.state.turn == turn_before + 1


class TestApplyActRevise:
    def setup_method(self):
        self.state = _state()
        assert_act = _act(ActType.ASSERT)
        apply_act(self.state, assert_act)
        self.claim_id = assert_act.claim_id
        ch = _act(ActType.CHALLENGE, claim_id=self.claim_id, agent_role="opposition")
        apply_act(self.state, ch)
        self.challenge_id = ch.act_id

    def test_revise_updates_content(self):
        rev = _act(ActType.REVISE, claim_id=self.claim_id,
                   target_act_id=self.challenge_id, content="revised content")
        apply_act(self.state, rev)
        assert self.state.claims[self.claim_id].content == "revised content"
        assert self.state.claims[self.claim_id].status == "revised"

    def test_revise_removes_challenge(self):
        rev = _act(ActType.REVISE, claim_id=self.claim_id, target_act_id=self.challenge_id)
        apply_act(self.state, rev)
        assert self.challenge_id not in self.state.outstanding_challenges


class TestApplyActConcede:
    def setup_method(self):
        self.state = _state()
        assert_act = _act(ActType.ASSERT)
        apply_act(self.state, assert_act)
        self.claim_id = assert_act.claim_id
        ch = _act(ActType.CHALLENGE, claim_id=self.claim_id, agent_role="opposition")
        apply_act(self.state, ch)
        self.challenge_id = ch.act_id

    def test_concede_returns_claim_to_open(self):
        # Opposition concedes THEIR challenge — the proposition's claim is no longer "challenged"
        con = _act(ActType.CONCEDE, claim_id=self.claim_id,
                   target_act_id=self.challenge_id, agent_role="opposition")
        apply_act(self.state, con)
        assert self.state.claims[self.claim_id].status == "open"

    def test_concede_removes_challenge(self):
        con = _act(ActType.CONCEDE, claim_id=self.claim_id,
                   target_act_id=self.challenge_id, agent_role="opposition")
        apply_act(self.state, con)
        assert self.challenge_id not in self.state.outstanding_challenges


class TestApplyActClose:
    def test_close_marks_open_claims_survived(self):
        state = _state()
        a = _act(ActType.ASSERT)
        apply_act(state, a)
        apply_act(state, _act(ActType.CLOSE, agent_role="moderator"))
        assert state.claims[a.claim_id].status == "survived"
        assert state.phase == "closed"

    def test_close_marks_challenged_claims_contested(self):
        state = _state()
        a = _act(ActType.ASSERT)
        apply_act(state, a)
        ch = _act(ActType.CHALLENGE, claim_id=a.claim_id, agent_role="opposition")
        apply_act(state, ch)
        apply_act(state, _act(ActType.CLOSE, agent_role="moderator"))
        assert state.claims[a.claim_id].status == "contested"
