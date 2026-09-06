"""Tests for agents/base.py — JSON parser, allowlist, rolling history."""
import json
import uuid
import pytest
from datetime import datetime

from agents.base import (
    BaseAgent, ResponseParseError, InvalidContentError, _MAX_PARSE_ATTEMPTS,
    _correction_prompt, _claims_missing_context,
)
from core.state import Act, ActType, DialogueState, TokenUsage


# ------------------------------------------------------------------
# Minimal concrete agent for testing BaseAgent directly
# ------------------------------------------------------------------

class _FakeAgent(BaseAgent):
    def __init__(self, role="proposition"):
        super().__init__(role=role, nickname="Test", model="gpt-4o",
                         temperature=0.5, config={},
                         # Routing is resolved before construction; these
                         # tests exercise parsing, not provider selection.
                         provider="openai")

    def _build_prompt(self, state):
        return "system", "user"


def _state(acts=None) -> DialogueState:
    now = datetime.utcnow().isoformat()
    return DialogueState(
        run_id="s1", turn=0, phase="init",
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
        act_id=str(uuid.uuid4()), run_id="s1", turn=turn,
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
        acts = [_act(turn=i) for i in range(10)]
        state = _state(acts=acts)
        result = agent._format_act_history(state)
        assert "omitted" in result
        assert "4 earlier act" in result  # 10 - 6 = 4 omitted

    def test_beyond_window_shows_recent_acts(self):
        agent = _FakeAgent()
        acts = [_act(turn=i, content=f"content-{i}") for i in range(10)]
        state = _state(acts=acts)
        result = agent._format_act_history(state)
        # Last 6 acts (turns 4-9) should be visible
        assert "Turn 9" in result
        assert "Turn 3" not in result


# ------------------------------------------------------------------
# _traced_generate — bounded JSON-parse retry
#
# Before this fix, a single JSON parse failure got just one repair
# retry with no bound on what happened if *that* also failed — the
# resulting json.JSONDecodeError propagated uncaught out of the runner's
# turn loop and killed the whole debate. Two live debates crashed this
# way in the same run (gemini-3.5-flash and claude-sonnet-5, two
# unrelated providers), 2026-08-30.
# ------------------------------------------------------------------

_GOOD_JSON = json.dumps({"act_type": "ASSERT", "content": "a valid response"})
_BAD_JSON = '{"act_type": "ASSERT", "content": "unterminated'


class TestTracedGenerateParseRetry:
    def _agent_with_responses(self, responses):
        """A _FakeAgent whose _call_provider yields each of `responses` in
        turn (as raw text), reporting 1/1 tokens per call."""
        agent = _FakeAgent()
        it = iter(responses)

        def _fake_call_provider(system, user, max_tokens=2048):
            return next(it), 1, 1

        agent._call_provider = _fake_call_provider
        return agent

    def test_succeeds_first_try_with_no_retry(self):
        agent = self._agent_with_responses([_GOOD_JSON])
        act = agent._traced_generate(_state(), "sys", "user")
        assert act.act_type == ActType.ASSERT

    def test_recovers_after_one_bad_attempt(self):
        agent = self._agent_with_responses([_BAD_JSON, _GOOD_JSON])
        act = agent._traced_generate(_state(), "sys", "user")
        assert act.act_type == ActType.ASSERT

    def test_recovers_on_the_final_allowed_attempt(self):
        # _MAX_PARSE_ATTEMPTS total attempts: the last one must still be
        # able to succeed, not just the first retry.
        responses = [_BAD_JSON] * (_MAX_PARSE_ATTEMPTS - 1) + [_GOOD_JSON]
        agent = self._agent_with_responses(responses)
        act = agent._traced_generate(_state(), "sys", "user")
        assert act.act_type == ActType.ASSERT

    def test_raises_response_parse_error_after_exhausting_all_attempts(self):
        agent = self._agent_with_responses([_BAD_JSON] * _MAX_PARSE_ATTEMPTS)
        with pytest.raises(ResponseParseError, match="invalid JSON"):
            agent._traced_generate(_state(), "sys", "user")

    def test_does_not_exceed_the_configured_attempt_bound(self):
        # One extra good response after the bound is exhausted must NOT be
        # consumed — proves the loop stops at _MAX_PARSE_ATTEMPTS
        # rather than retrying indefinitely until it happens to succeed.
        agent = self._agent_with_responses([_BAD_JSON] * _MAX_PARSE_ATTEMPTS + [_GOOD_JSON])
        with pytest.raises(ResponseParseError):
            agent._traced_generate(_state(), "sys", "user")


class TestCorrectionPrompt:
    def _exc(self):
        try:
            json.loads("{bad")
        except json.JSONDecodeError as e:
            return e

    def test_non_strict_asks_for_correction(self):
        prompt = _correction_prompt("the original user message", "{bad", self._exc(), strict=False)
        assert "corrected JSON" in prompt

    def test_strict_names_the_specific_failure_modes(self):
        # The stricter prompt is the one substantive difference introduced by
        # this fix: it names the exact failure modes seen in production
        # (unescaped newlines and quotes inside strings) instead of a bare
        # "that wasn't JSON" — this is what should make the final attempt
        # more likely to succeed than a repeat of the first correction.
        prompt = _correction_prompt("the original user message", "{bad", self._exc(), strict=True)
        assert "\\n" in prompt
        assert "final attempt" in prompt.lower()

    def test_correction_preserves_the_original_user_message(self):
        # The root cause of the recorded "context wasn't supplied" acts
        # (2026-08-30): the old correction prompt REPLACED the user message,
        # so the retry carried no topic, dialogue state, or evidence pool,
        # and the model's honest report of that absence became a debate act.
        # Both correction prompts must carry the full original message.
        original = "<debate_data>topic and evidence pool live here</debate_data>"
        for strict in (False, True):
            prompt = _correction_prompt(original, "{bad", self._exc(), strict=strict)
            assert original in prompt

    def test_empty_response_is_named_not_quoted(self):
        # A reasoning model that exhausts its completion budget returns ""
        # (measured: 7 of 9 kimi-k3 trials under the old 2048 cap). Quoting
        # an empty string back reads as a glitch; the prompt should say the
        # response was empty instead.
        prompt = _correction_prompt("original", "", self._exc(), strict=False)
        assert "empty" in prompt.lower()
        assert "Here is what you returned" not in prompt


# ------------------------------------------------------------------
# Citation repair — a mismatched quote gets one corrective retry
# before the act enters the record (2 mismatches slipped into the
# record across three live runs, 2026-09-06, before this existed)
# ------------------------------------------------------------------

_REPAIR_URL = "https://example.com/study"
_REPAIR_SOURCE = "The study found induced effects of 18 to 25 per cent."


class _RepairPool:
    """Pool fake for the full _traced_generate path: membership check
    (verify_citations) plus the quote-contract lookups."""

    def source_text_for(self, url):
        return _REPAIR_SOURCE if url == _REPAIR_URL else None

    def source_title_for(self, url):
        return "Study" if url == _REPAIR_URL else None

    def ensure_full_text(self, urls):
        return 0

    def verify_citations(self, text):
        return ([_REPAIR_URL] if _REPAIR_URL in text else []), []


def _cited_json(quote):
    return json.dumps({
        "act_type": "ASSERT",
        "content": f"Induced effects were large ([s]({_REPAIR_URL})).",
        "citations": [{"url": _REPAIR_URL, "quote": quote}],
    })


_BAD_QUOTE_JSON = _cited_json("ridership rose 40 per cent")
_GOOD_QUOTE_JSON = _cited_json("induced effects of 18 to 25 per cent")


class TestCitationRepair:
    def _agent_with_responses(self, responses):
        agent = _FakeAgent()
        it = iter(responses)
        calls = []

        def _fake_call_provider(system, user, max_tokens=2048):
            calls.append(user)
            return next(it), 1, 1

        agent._call_provider = _fake_call_provider
        return agent, calls

    def test_mismatch_triggers_one_corrective_retry(self):
        agent, calls = self._agent_with_responses([_BAD_QUOTE_JSON, _GOOD_QUOTE_JSON])
        act = agent._traced_generate(_state(), "sys", "user", pool=_RepairPool())
        assert len(calls) == 2
        assert act.citations[0]["status"] == "verified"
        assert act.citation_repairs == 1
        assert act.retries == 0
        assert "[citation check:" not in act.content

    def test_repair_prompt_names_the_failing_quote_and_source(self):
        agent, calls = self._agent_with_responses([_BAD_QUOTE_JSON, _GOOD_QUOTE_JSON])
        agent._traced_generate(_state(), "sys", "user", pool=_RepairPool())
        assert _REPAIR_URL in calls[1]
        assert "ridership rose 40 per cent" in calls[1]
        assert "verbatim" in calls[1]
        # The repair, like every correction, carries the original message.
        assert calls[1].startswith("user")

    def test_still_mismatched_after_repair_records_the_violation(self):
        agent, calls = self._agent_with_responses([_BAD_QUOTE_JSON, _BAD_QUOTE_JSON])
        act = agent._traced_generate(_state(), "sys", "user", pool=_RepairPool())
        assert len(calls) == 2
        assert act.citations[0]["status"] == "mismatch"
        assert act.citation_repairs == 1
        assert "[citation check:" in act.content

    def test_clean_act_makes_no_repair_call(self):
        agent, calls = self._agent_with_responses([_GOOD_QUOTE_JSON])
        act = agent._traced_generate(_state(), "sys", "user", pool=_RepairPool())
        assert len(calls) == 1
        assert act.citation_repairs == 0
        assert act.citations[0]["status"] == "verified"


# ------------------------------------------------------------------
# _claims_missing_context — content validity beyond act-type legality
#
# Found via a synthesiser-model comparison against a recorded closed debate
# (2026-08-30): kimi-k3 (proposition, Moonshot) twice asserted a
# content-free "the dialogue state/evidence pool were not included" claim
# instead of a substantive argument, and claude-sonnet-5 and gpt-4.1 (both
# synthesiser candidates) independently made the same false claim against
# the same well-formed prompt. All four were syntactically legal acts —
# the act-type allowlist has nothing to say about whether the content is
# substantive. This is the second check: legality, then validity.
# ------------------------------------------------------------------

class TestClaimsMissingContext:
    @pytest.mark.parametrize("content", [
        "The dialogue state and evidence pool were not included in this turn, "
        "so I cannot identify the motion.",
        "No debate topic, dialogue state, act history, or evidence pool was "
        "supplied with this turn. I have nothing honest to assert yet.",
        "No debate record was provided in this session, so no claims could be identified.",
        "The dialogue state and evidence pool were not included in this turn, so I cannot "
        "identify the motion, the legal acts available, or any citable sources.",
    ])
    def test_flags_known_refusal_phrasings(self, content):
        assert _claims_missing_context(content) is True

    @pytest.mark.parametrize("content", [
        "Universal basic income reduces poverty because recipients report less hunger.",
        "The opposition's evidence pool is thin on cross-national data.",
        "This claim rests on a narrow reading of the dialogue between economists.",
        "",
    ])
    def test_does_not_flag_real_arguments(self, content):
        # A substantive claim can legitimately use words like "evidence" or "dialogue"
        # — only the compound refusal phrasing should trip this.
        assert _claims_missing_context(content) is False


class TestTracedGenerateContentValidity:
    _REFUSAL = json.dumps({
        "act_type": "ASSERT",
        "content": "The dialogue state and evidence pool were not included in this turn.",
    })

    def _agent_with_responses(self, responses):
        agent = _FakeAgent()
        it = iter(responses)

        def _fake_call_provider(system, user, max_tokens=2048):
            return next(it), 1, 1

        agent._call_provider = _fake_call_provider
        return agent

    def test_recovers_after_one_refusal(self):
        agent = self._agent_with_responses([self._REFUSAL, _GOOD_JSON])
        act = agent._traced_generate(_state(), "sys", "user")
        assert act.act_type == ActType.ASSERT
        assert act.content == "a valid response"

    def test_raises_invalid_content_error_after_exhausting_all_attempts(self):
        agent = self._agent_with_responses([self._REFUSAL] * _MAX_PARSE_ATTEMPTS)
        with pytest.raises(InvalidContentError, match="missing"):
            agent._traced_generate(_state(), "sys", "user")

    def test_valid_content_on_first_try_needs_no_retry(self):
        agent = self._agent_with_responses([_GOOD_JSON])
        act = agent._traced_generate(_state(), "sys", "user")
        assert act.content == "a valid response"


class TestRetriesCarryOriginalContext:
    """Every retry _traced_generate sends — JSON repair or content-validity —
    must contain the full original user message. The replacement-style retry
    was how empty reasoning-model responses became context-free calls whose
    honest 'nothing was supplied' answers entered the debate record."""

    _ORIGINAL = "<debate_data>the topic, dialogue state, and evidence pool</debate_data>"
    _REFUSAL = json.dumps({"act_type": "ASSERT",
                           "content": "No debate record was provided in this session."})

    def _capture_agent(self, responses):
        agent = _FakeAgent()
        it = iter(responses)
        seen_users = []

        def _fake_call_provider(system, user, max_tokens=None):
            seen_users.append(user)
            return next(it), 1, 1

        agent._call_provider = _fake_call_provider
        return agent, seen_users

    def test_json_repair_retry_includes_original_user_message(self):
        agent, seen = self._capture_agent([_BAD_JSON, _GOOD_JSON])
        agent._traced_generate(_state(), "sys", self._ORIGINAL)
        assert len(seen) == 2
        assert self._ORIGINAL in seen[1]

    def test_missing_context_retry_includes_original_user_message(self):
        agent, seen = self._capture_agent([self._REFUSAL, _GOOD_JSON])
        agent._traced_generate(_state(), "sys", self._ORIGINAL)
        assert len(seen) == 2
        assert self._ORIGINAL in seen[1]
