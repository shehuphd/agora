"""Tests for the JSON-bearing act renderers in core/export.py.

STATUS and ARGUMENT_MAP store JSON in `content`. These check the Markdown
renderings match what the UI shows, and that anything unexpected falls back to
the raw string rather than being dropped.
"""
import json

from core.export import (
    build_markdown,
    format_act_content,
    format_argument_map_content,
    format_status_content,
)


# ------------------------------------------------------------------
# STATUS
# ------------------------------------------------------------------

class TestStatusContent:
    def _checks(self, **kw):
        base = {
            "turns_used": 3, "max_turns": 8,
            "total_tokens": 12500, "token_budget": 40000,
            "outstanding_challenge_count": 2,
            "repetition_count": 0, "repetition_tolerance": 2,
        }
        base.update(kw)
        return json.dumps(base)

    def test_renders_turn_challenges_and_tokens(self):
        out = format_status_content(self._checks())
        assert "`turn 3/8`" in out
        assert "`challenges open: 2`" in out
        assert "`tokens 12,500/40,000`" in out

    def test_no_raw_json_braces_remain(self):
        out = format_status_content(self._checks())
        assert "{" not in out and "token_budget" not in out

    def test_repetitions_hidden_when_zero(self):
        assert "repetitions" not in format_status_content(self._checks())

    def test_repetitions_shown_with_warning_when_present(self):
        out = format_status_content(self._checks(repetition_count=3))
        assert "`⚠ repetitions: 3/2`" in out

    def test_zero_challenges_still_shown(self):
        # 0 open challenges is a real reading, not a missing field.
        assert "`challenges open: 0`" in format_status_content(
            self._checks(outstanding_challenge_count=0))

    def test_missing_max_turns_degrades(self):
        out = format_status_content(json.dumps({"turns_used": 4}))
        assert "`turn 4/?`" in out

    def test_non_json_falls_back_to_raw(self):
        assert format_status_content("just prose") == "just prose"

    def test_json_non_dict_falls_back_to_raw(self):
        assert format_status_content("[1, 2, 3]") == "[1, 2, 3]"

    def test_empty_dict_falls_back_to_raw(self):
        assert format_status_content("{}") == "{}"


# ------------------------------------------------------------------
# ARGUMENT_MAP
# ------------------------------------------------------------------

class TestArgumentMapContent:
    def _map(self, **kw):
        base = {
            "act_type": "ARGUMENT_MAP",
            "surviving_claims": [
                {"claim_id": "c1", "final_text": "Survivor claim.",
                 "survived_because": "Never challenged."},
            ],
            "revised_claims": [
                {"claim_id": "c2", "final_text": "Revised claim.",
                 "original_text": "Original claim.",
                 "revised_because": "Narrowed after challenge."},
            ],
            "contested_claims": [
                {"claim_id": "c3", "final_text": "Contested claim.",
                 "contested_because": "Premise questioned.",
                 "evidence_needed": "Comparative study."},
            ],
            "arbiter_summary": "The debate closed with one claim unresolved.",
        }
        base.update(kw)
        return json.dumps(base)

    def test_renders_all_three_sections(self):
        out = format_argument_map_content(self._map())
        assert "**Surviving claims** (1)" in out
        assert "**Revised claims** (1)" in out
        assert "**Contested claims** (1)" in out

    def test_renders_claim_text_as_bullets(self):
        out = format_argument_map_content(self._map())
        assert "- Survivor claim." in out
        assert "- Contested claim." in out

    def test_renders_reason_sub_bullets(self):
        out = format_argument_map_content(self._map())
        assert "*Survived because: Never challenged.*" in out
        assert "*Originally: Original claim.*" in out
        assert "*Revised because: Narrowed after challenge.*" in out
        assert "*Contested because: Premise questioned.*" in out
        assert "*Evidence needed: Comparative study.*" in out

    def test_renders_arbiter_summary(self):
        out = format_argument_map_content(self._map())
        assert "**Arbiter summary**" in out
        assert "The debate closed with one claim unresolved." in out

    def test_empty_sections_omitted(self):
        out = format_argument_map_content(self._map(surviving_claims=[], revised_claims=[]))
        assert "Surviving claims" not in out
        assert "Revised claims" not in out
        assert "**Contested claims** (1)" in out

    def test_no_raw_json_keys_remain(self):
        out = format_argument_map_content(self._map())
        for key in ("final_text", "contested_because", "arbiter_summary", "claim_id"):
            assert key not in out

    def test_fully_empty_map_states_so(self):
        out = format_argument_map_content(json.dumps({
            "surviving_claims": [], "revised_claims": [], "contested_claims": [],
        }))
        assert "empty argument map" in out

    def test_summary_only_map_renders(self):
        out = format_argument_map_content(json.dumps({"arbiter_summary": "Just a summary."}))
        assert "Just a summary." in out

    def test_claim_without_optional_notes(self):
        out = format_argument_map_content(json.dumps({
            "surviving_claims": [{"final_text": "Bare claim."}],
        }))
        assert "- Bare claim." in out
        assert "*" not in out.split("- Bare claim.")[1]

    def test_claim_using_content_key(self):
        out = format_argument_map_content(json.dumps({
            "surviving_claims": [{"content": "From content key."}],
        }))
        assert "- From content key." in out

    def test_non_dict_claim_entry_survives(self):
        out = format_argument_map_content(json.dumps({
            "surviving_claims": ["a plain string claim"],
        }))
        assert "- a plain string claim" in out

    def test_non_json_falls_back_to_raw(self):
        assert format_argument_map_content("prose verdict") == "prose verdict"


# ------------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------------

class TestFormatActContent:
    def test_status_dispatched(self):
        out = format_act_content("STATUS", json.dumps({"turns_used": 1, "max_turns": 2}))
        assert "`turn 1/2`" in out

    def test_argument_map_dispatched(self):
        out = format_act_content("ARGUMENT_MAP", json.dumps({"arbiter_summary": "Done."}))
        assert "**Arbiter summary**" in out

    def test_prose_act_types_passed_through(self):
        for act_type in ("ASSERT", "CHALLENGE", "DEFEND", "CLOSE", "CONCEDE"):
            assert format_act_content(act_type, "Plain prose.") == "Plain prose."

    def test_empty_content_returns_empty(self):
        assert format_act_content("STATUS", "") == ""
        assert format_act_content("ASSERT", None) == ""

    def test_unknown_act_type_passed_through(self):
        assert format_act_content("FUTURE_TYPE", "content") == "content"


# ------------------------------------------------------------------
# End-to-end through the transcript renderer
# ------------------------------------------------------------------

class TestTranscriptIntegration:
    def _data(self, act_type, content):
        return {
            "run_id": "r1", "topic": "T", "debate_title": "T",
            "created_at": "2026-01-01T00:00:00", "status": "closed",
            "config": {}, "claims": [],
            "acts": [{
                "act_id": "a1", "turn": 1, "agent": "Moderator",
                "agent_role": "moderator", "act_type": act_type,
                "content": content, "reason": None, "claim_id": None,
                "input_tokens": 10, "output_tokens": 5,
                "model_used": "m", "timestamp": "2026-01-01T00:00:01",
            }],
        }

    def test_status_act_not_raw_json_in_transcript(self):
        md = build_markdown(self._data("STATUS", json.dumps({
            "turns_used": 1, "max_turns": 2,
            "total_tokens": 500, "token_budget": 1000,
            "outstanding_challenge_count": 0,
        })))
        assert "`turn 1/2`" in md
        assert '"turns_used"' not in md

    def test_argument_map_act_not_raw_json_in_transcript(self):
        md = build_markdown(self._data("ARGUMENT_MAP", json.dumps({
            "contested_claims": [{"final_text": "A contested point.",
                                  "contested_because": "Unresolved."}],
            "arbiter_summary": "Closed unresolved.",
        })))
        assert "**Contested claims** (1)" in md
        assert "- A contested point." in md
        assert '"final_text"' not in md

    def test_prose_act_unchanged_in_transcript(self):
        md = build_markdown(self._data("CLOSE", "The debate closed on turn limit."))
        assert "The debate closed on turn limit." in md
