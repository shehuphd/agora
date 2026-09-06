"""The fixed-size turn-context frame.

Per-turn spend in long runs was growing linearly (+195 tokens/turn/turn in the
100-turn measurement) because every seat's prompt carried the full
outstanding-challenge ledger. These tests pin the bounds that make prompts
fixed-size: the audited challenge window, capped defence lists, the bounded
URL-freshness set, challenge lapsing, chapter injection, the chapter cap, and
windowed turn cards.
"""
import uuid
from datetime import datetime

import pytest

from core.state import (
    Act,
    DialogueState,
    TokenUsage,
    lapse_stale_challenges,
)


def _state(**kwargs) -> DialogueState:
    base = dict(
        run_id="run-b", turn=0, phase="challenge", claims={}, acts=[],
        outstanding_challenges=[], next_agent="proposition",
        legal_acts=["CHALLENGE", "CONCEDE"],
        token_usage={r: TokenUsage() for r in
                     ("proposition", "opposition", "moderator", "synthesiser")},
        debate_title="t", topic="Motion under test", config={},
        created_at=datetime.utcnow().isoformat(),
        closed_at=None, closure_reason=None,
    )
    base.update(kwargs)
    return DialogueState(**base)


def _act(turn, role, act_type, content="c", claim_id=None, target_act_id=None,
         challenge_type=None, act_id=None):
    return Act(
        act_id=act_id or str(uuid.uuid4()), run_id="run-b", turn=turn,
        agent=role.title(), agent_role=role, act_type=act_type,
        claim_id=claim_id, target_act_id=target_act_id, content=content,
        reason=None, input_tokens=1, output_tokens=1, model_used="m",
        timestamp=datetime.utcnow().isoformat(), challenge_type=challenge_type,
    )


def _opposition():
    from agents.opposition import OppositionAgent
    return OppositionAgent(provider="openai")


def _proposition():
    from agents.proposition import PropositionAgent
    return PropositionAgent(provider="openai")


def _moderator():
    from agents.moderator import ModeratorAgent
    return ModeratorAgent(provider="openai")


def _state_with_challenges(n, cite=False):
    """n outstanding challenges at turns 0..n-1, oldest first."""
    state = _state(turn=n)
    for i in range(n):
        url = f"https://src{i}.org/page" if cite else ""
        content = f"[premise] objection {i}" + (f" ([s]({url}))" if cite else "")
        ch = _act(i, "opposition", "CHALLENGE", content=content,
                  challenge_type="premise", act_id=f"ch-{i:03d}")
        state.acts.append(ch)
        state.outstanding_challenges.append(ch.act_id)
    return state


# ---------------------------------------------------------------------------
# Opposition: audit window, defence-list cap, URL freshness
# ---------------------------------------------------------------------------

class TestOppositionAuditBounds:
    def test_audit_lists_at_most_window_challenges(self):
        state = _state_with_challenges(15)
        system, _ = _opposition()._build_prompt(state)
        audit = system[system.index("CONCEDE AUDIT"):]
        assert audit.count("act_id:ch-") == 10
        assert "ch-014" in audit and "ch-005" in audit
        assert "act_id:ch-004" not in audit

    def test_older_challenges_collapse_to_aggregate_line(self):
        state = _state_with_challenges(15)
        system, _ = _opposition()._build_prompt(state)
        assert "+5 older outstanding challenge(s) omitted" in system
        assert "premise×5" in system

    def test_small_ledger_has_no_aggregate_line(self):
        state = _state_with_challenges(3)
        system, _ = _opposition()._build_prompt(state)
        assert "older outstanding challenge(s) omitted" not in system
        assert system.count("act_id:ch-") == 3

    def test_defence_turn_lists_are_capped(self):
        state = _state_with_challenges(1)
        for turn in (2, 4, 6, 8, 10):
            state.acts.append(_act(
                turn, "proposition", "DEFEND",
                content="defended ([s](https://d.org/x))",
                target_act_id="ch-000",
            ))
        state.turn = 11
        system, _ = _opposition()._build_prompt(state)
        assert "5 defences, most recently at turns [8, 10]" in system
        assert "[2, 4, 6, 8, 10]" not in system

    def test_url_freshness_covers_active_challenges_only(self):
        state = _state_with_challenges(12, cite=True)
        system, _ = _opposition()._build_prompt(state)
        fresh = system[system.index("SOURCE FRESHNESS"):]
        assert "https://src11.org/page" in fresh
        assert "https://src2.org/page" in fresh
        assert "https://src0.org/page" not in fresh
        assert "https://src1.org/page" not in fresh

    def test_lapsed_count_is_reported(self):
        state = _state_with_challenges(2)
        state.lapsed_challenges = ["ch-old-a", "ch-old-b", "ch-old-c"]
        system, _ = _opposition()._build_prompt(state)
        assert "3 earlier challenge(s) lapsed" in system


# ---------------------------------------------------------------------------
# Bounded dialogue-state view (all seats)
# ---------------------------------------------------------------------------

class TestBoundedChallengeView:
    def test_bounded_view_shape(self):
        state = _state_with_challenges(13)
        state.lapsed_challenges = ["x"]
        view = _proposition()._bounded_challenges(state)
        assert view["count"] == 13
        assert len(view["active"]) == 10
        assert view["active"][-1] == "ch-012"
        assert view["older_omitted"] == 3
        assert view["lapsed_count"] == 1

    def test_proposition_prompt_uses_bounded_view(self):
        state = _state_with_challenges(13)
        _, user = _proposition()._build_prompt(state)
        assert '"count": 13' in user
        assert "ch-000" not in user.split("<act_history>")[0]

    def test_moderator_prompt_uses_bounded_view(self):
        state = _state_with_challenges(13)
        _, user = _moderator()._build_prompt(state)
        assert '"count": 13' in user
        assert '"older_omitted": 3' in user


# ---------------------------------------------------------------------------
# Challenge lapsing
# ---------------------------------------------------------------------------

def _defended_state(defence_turn, now, followup_turn=None, followup_target="ch-000"):
    state = _state(turn=now)
    ch = _act(0, "opposition", "CHALLENGE", claim_id="claim-1", act_id="ch-000")
    state.acts.append(ch)
    state.outstanding_challenges.append(ch.act_id)
    state.acts.append(_act(defence_turn, "proposition", "DEFEND",
                           claim_id="claim-1", target_act_id="ch-000",
                           act_id="def-000"))
    if followup_turn is not None:
        state.acts.append(_act(followup_turn, "opposition", "CHALLENGE",
                               claim_id="claim-1", act_id="ch-follow",
                               target_act_id=followup_target))
        state.outstanding_challenges.append("ch-follow")
    return state


class TestLapseStaleChallenges:
    def test_defended_and_ignored_challenge_lapses_after_n_turns(self):
        state = _defended_state(defence_turn=3, now=13)
        lapsed = lapse_stale_challenges(state)
        assert lapsed == ["ch-000"]
        assert "ch-000" not in state.outstanding_challenges
        assert state.lapsed_challenges == ["ch-000"]

    def test_does_not_lapse_before_n_turns(self):
        state = _defended_state(defence_turn=3, now=12)
        assert lapse_stale_challenges(state) == []
        assert "ch-000" in state.outstanding_challenges

    def test_followup_targeting_the_challenge_keeps_it_alive(self):
        state = _defended_state(defence_turn=3, now=20, followup_turn=5,
                                followup_target="ch-000")
        assert lapse_stale_challenges(state) == []
        assert "ch-000" in state.outstanding_challenges

    def test_followup_targeting_a_defence_keeps_it_alive(self):
        state = _defended_state(defence_turn=3, now=20, followup_turn=5,
                                followup_target="def-000")
        assert lapse_stale_challenges(state) == []
        assert "ch-000" in state.outstanding_challenges

    def test_same_claim_untargeted_followup_does_not_keep_it_alive(self):
        # The single-claim inertness regression (2026-09-05): with one claim,
        # a claim-wide follow-up test made every opposition act keep every
        # challenge alive — 0 lapses in 100 turns, 50 outstanding. An
        # opposition act on the same claim that doesn't target this thread
        # must not block the lapse.
        state = _defended_state(defence_turn=3, now=20, followup_turn=5,
                                followup_target=None)
        assert "ch-000" in lapse_stale_challenges(state)

    def test_undefended_challenge_never_lapses(self):
        state = _state(turn=50)
        ch = _act(0, "opposition", "CHALLENGE", claim_id="c", act_id="ch-000")
        state.acts.append(ch)
        state.outstanding_challenges.append(ch.act_id)
        assert lapse_stale_challenges(state) == []
        assert state.outstanding_challenges == ["ch-000"]


# ---------------------------------------------------------------------------
# Chapters reach debater and moderator prompts
# ---------------------------------------------------------------------------

class TestChaptersInPrompts:
    def test_debaters_and_moderator_see_chapters(self):
        state = _state(turn=12)
        state.chapters = ["[Turns 1-10] the opening chapter"]
        for agent in (_proposition(), _opposition()):
            _, user = agent._build_prompt(state)
            assert "<chapter_summaries>" in user
            assert "the opening chapter" in user
        _, mod_user = _moderator()._build_prompt(state)
        assert "the opening chapter" in mod_user

    def test_no_chapters_no_block(self):
        state = _state(turn=2)
        _, user = _proposition()._build_prompt(state)
        assert "<chapter_summaries>" not in user


# ---------------------------------------------------------------------------
# Turn-card window
# ---------------------------------------------------------------------------

class TestTurnCardWindow:
    def test_limit_windows_the_cards(self):
        state = _state(turn=60)
        for i in range(60):
            state.acts.append(_act(i, "proposition", "ASSERT", content=f"claim {i}"))
        cards = _proposition()._format_turn_cards(state, limit=40)
        assert "20 earlier act(s) omitted" in cards
        assert "claim 59" in cards and "claim 20" in cards
        assert "claim 19" not in cards

    def test_no_limit_keeps_everything(self):
        state = _state(turn=60)
        for i in range(60):
            state.acts.append(_act(i, "proposition", "ASSERT", content=f"claim {i}"))
        cards = _proposition()._format_turn_cards(state)
        assert "omitted" not in cards
        assert "claim 0" in cards


# ---------------------------------------------------------------------------
# Chapter cap and epoch collapse
# ---------------------------------------------------------------------------

def _run_chapter_pass(state, synth):
    import asyncio
    from runners import debate as _debate

    async def scenario():
        orch = object.__new__(_debate.TurnOrchestrator)
        orch._loop = asyncio.get_running_loop()
        orch.synthesiser = synth
        orch.state = state
        await orch._maybe_summarise_chapter()

    asyncio.run(scenario())


class TestChapterCap:
    def test_oldest_half_collapses_into_an_epoch(self):
        class FakeSynth:
            def __init__(self):
                self.epoch_input = None

            def summarise_chapter(self, state, start, end):
                return f"[Turns {start}-{end}] chapter"

            def summarise_epoch(self, state, chapters):
                self.epoch_input = list(chapters)
                return "[Turns 1-50] epoch"

        state = _state(turn=110)
        state.chapters = [
            f"[Turns {i * 10 + 1}-{(i + 1) * 10}] chapter" for i in range(10)
        ]
        synth = FakeSynth()
        _run_chapter_pass(state, synth)
        # 10 stored + 1 new = 11 > cap of 10 → oldest 5 collapse to 1 epoch.
        assert len(state.chapters) == 7
        assert state.chapters[0] == "[Turns 1-50] epoch"
        assert len(synth.epoch_input) == 5

    def test_under_cap_no_collapse(self):
        class FakeSynth:
            def summarise_chapter(self, state, start, end):
                return f"[Turns {start}-{end}] chapter"

            def summarise_epoch(self, state, chapters):  # pragma: no cover
                raise AssertionError("must not collapse under the cap")

        state = _state(turn=20)
        state.chapters = ["[Turns 1-10] chapter"]
        _run_chapter_pass(state, FakeSynth())
        assert len(state.chapters) == 2

    def test_hung_chapter_call_is_skipped_not_waited_on(self, monkeypatch):
        # Observed live 2026-09-05: one hung provider connection inside a
        # chapter call froze a run at turn 50 for two hours. The chapter
        # call gets the same hard timeout as agent calls.
        import time
        from runners import debate as _debate

        class HangingSynth:
            def summarise_chapter(self, state, start, end):
                time.sleep(5)
                return "[Turns 41-50] too late"

        monkeypatch.setattr(_debate, "_AGENT_TIMEOUT", 0.3)
        state = _state(turn=50)
        elapsed = {}

        async def scenario():
            import asyncio
            orch = object.__new__(_debate.TurnOrchestrator)
            orch._loop = asyncio.get_running_loop()
            orch.synthesiser = HangingSynth()
            orch.state = state
            t0 = time.time()
            await orch._maybe_summarise_chapter()
            # Measured inside the loop: asyncio.run's shutdown joins the
            # executor thread afterwards, which the live server never does
            # between turns.
            elapsed["s"] = time.time() - t0

        import asyncio
        asyncio.run(scenario())
        assert elapsed["s"] < 3, "the loop must not wait out the hung call"
        assert state.chapters == []
