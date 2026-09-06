"""Judge scoring: model-graded fidelity, map quality, and chain resolution.

All provider calls are faked; the tests pin item selection (only verified
citations with a locatable citing sentence), vote aggregation (majority for
booleans, median for scores), chain assembly, JSON repair, failure holes, and
storage round-trips.
"""
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from core import judge as _judge
from core.checkpoint import init_db, write_act_to_db
from core.judge import JudgeConfig, judge_run, list_judgements, store_judgement
from core.state import Act

_URL = "https://example.org/study"


def _act(turn, role, act_type, content="c", citations=None, claim_id=None,
         target_act_id=None):
    return Act(
        act_id=f"{act_type.lower()}-{turn}", run_id="run-j", turn=turn,
        agent=role.title(), agent_role=role, act_type=act_type,
        claim_id=claim_id, target_act_id=target_act_id, content=content,
        reason=None, input_tokens=1, output_tokens=1, model_used="m",
        timestamp=datetime.utcnow().isoformat(), citations=citations,
    )


@pytest.fixture()
def run_dir(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "debate.db"))
    init_db(conn)
    acts = [
        _act(0, "proposition", "ASSERT",
             content=(
                 f"Libraries raise literacy by 12% ([s]({_URL})). "
                 "Walled evidence agrees ([w](https://walled.org/x))."
             ),
             citations=[{"url": _URL, "quote": "literacy rose by 12%",
                         "status": "verified", "ungrounded_numbers": []},
                        {"url": "https://walled.org/x", "quote": "q",
                         "status": "unverifiable", "ungrounded_numbers": []}],
             claim_id="claim-1"),
        _act(1, "opposition", "CHALLENGE", content="[premise] Objection.",
             claim_id="claim-1"),
        _act(2, "proposition", "DEFEND", content="Defence with evidence.",
             claim_id="claim-1", target_act_id="challenge-1"),
        _act(3, "opposition", "CONCEDE", content="Conceded.",
             claim_id="claim-1", target_act_id="challenge-1"),
        _act(4, "synthesiser", "ARGUMENT_MAP",
             content=json.dumps({"surviving_claims": [{"claim_id": "claim-1"}]})),
    ]
    for a in acts:
        write_act_to_db(conn, a)
    conn.close()
    return tmp_path


def _config(votes=1):
    return JudgeConfig(
        fidelity_model="judge-small", fidelity_provider="openai",
        map_model="judge-map", map_provider="openai", votes=votes,
    )


class _FakeCalls:
    """Scripted replacement for _call_judge, recording every invocation."""

    def __init__(self, responses):
        self.responses = responses    # action -> list of dicts (cycled)
        self.calls = []               # (action, model)

    def __call__(self, provider, model, system, user, action, ledger, run_id):
        self.calls.append((action, model, user))
        ledger.calls += 1
        seq = self.responses.get(action)
        if not seq:
            return None
        return seq[(len([c for c in self.calls if c[0] == action]) - 1) % len(seq)]


def _run(run_dir, monkeypatch, responses, votes=1):
    fake = _FakeCalls(responses)
    monkeypatch.setattr(_judge, "_call_judge", fake)
    payload = judge_run(run_dir, "run-j", _config(votes=votes))
    return payload, fake


class TestFidelity:
    def test_only_verified_citations_are_judged(self, run_dir, monkeypatch):
        payload, fake = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True, "reason": "ok"}],
            "judge.map": [{"score": 1.0}],
            "judge.chain": [{"earned": True}],
        })
        fid = payload["citation_fidelity"]
        assert fid["eligible"] == 1          # the unverifiable one is excluded
        assert fid["judged"] == 1
        assert fid["score"] == 1.0
        # The judged sentence is the one citing the URL, not the whole act.
        fidelity_users = [u for a, m, u in fake.calls if a == "judge.fidelity"]
        assert "Libraries raise literacy" in fidelity_users[0]
        assert "Second point" not in fidelity_users[0]

    def test_array_only_citation_is_judged_against_the_whole_act(self, tmp_path, monkeypatch):
        # A citation whose URL never appears inline (observed live: models
        # citing only through the structured array) must still be judged —
        # with the act's whole content as the citing text.
        conn = sqlite3.connect(str(tmp_path / "debate.db"))
        init_db(conn)
        write_act_to_db(conn, _act(
            0, "proposition", "ASSERT",
            content="Literacy rose by 12 percent across the cohort.",
            citations=[{"url": _URL, "quote": "literacy rose by 12%",
                        "status": "verified", "ungrounded_numbers": []}],
        ))
        conn.close()
        payload, fake = _run(tmp_path, monkeypatch, {
            "judge.fidelity": [{"faithful": True, "reason": "ok"}],
        })
        fid = payload["citation_fidelity"]
        assert fid["eligible"] == 1 and fid["judged"] == 1
        user = [u for a, m, u in fake.calls if a == "judge.fidelity"][0]
        assert "Literacy rose by 12 percent" in user

    def test_unfaithful_verdict_lowers_the_score(self, run_dir, monkeypatch):
        payload, _ = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": False, "reason": "referent moved"}],
            "judge.map": [{"score": 1.0}],
            "judge.chain": [{"earned": True}],
        })
        assert payload["citation_fidelity"]["score"] == 0.0

    def test_three_votes_take_the_majority(self, run_dir, monkeypatch):
        payload, fake = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}, {"faithful": False},
                               {"faithful": True}],
            "judge.map": [{"score": 0.5}],
            "judge.chain": [{"earned": False}, {"earned": True},
                            {"earned": False}],
        }, votes=3)
        fid = payload["citation_fidelity"]
        assert fid["verdicts"][0]["faithful"] is True     # 2 of 3
        assert len(fid["verdicts"][0]["votes"]) == 3
        assert payload["challenge_resolution"]["verdicts"][0]["earned"] is False


class TestMap:
    def test_map_score_is_the_median_of_votes(self, run_dir, monkeypatch):
        payload, _ = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}],
            "judge.map": [{"score": 0.2}, {"score": 0.9}, {"score": 0.8}],
            "judge.chain": [{"earned": True}],
        }, votes=3)
        assert payload["map_quality"]["score"] == 0.8

    def test_missing_map_scores_none(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "debate.db"))
        init_db(conn)
        write_act_to_db(conn, _act(0, "proposition", "ASSERT", content="c"))
        conn.close()
        payload, fake = _run(tmp_path, monkeypatch, {})
        assert payload["map_quality"]["score"] is None
        assert not any(a == "judge.map" for a, m, u in fake.calls)


class TestChains:
    def test_chain_carries_challenge_defence_and_concession(self, run_dir, monkeypatch):
        payload, fake = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}],
            "judge.map": [{"score": 1.0}],
            "judge.chain": [{"earned": True, "reason": "ok"}],
        })
        chain_users = [u for a, m, u in fake.calls if a == "judge.chain"]
        assert len(chain_users) == 1
        assert "Objection" in chain_users[0]
        assert "Defence with evidence" in chain_users[0]
        assert "Conceded" in chain_users[0]
        assert payload["challenge_resolution"]["score"] == 1.0

    def test_failed_votes_leave_a_hole_not_a_crash(self, run_dir, monkeypatch):
        payload, _ = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}],
            "judge.map": [{"score": 1.0}],
            # judge.chain absent: every chain vote returns None
        })
        chains = payload["challenge_resolution"]
        assert chains["eligible"] == 1
        assert chains["judged"] == 0
        assert chains["score"] is None


class TestCallPlumbing:
    def test_invalid_json_gets_one_repair_then_parses(self, monkeypatch):
        calls = []

        def fake_generate(provider, key, model, system, user, temp, max_tokens):
            calls.append(user)
            if len(calls) == 1:
                return "not json at all", 10, 5
            return '{"faithful": true}', 10, 5

        import providers as _providers
        monkeypatch.setattr(_providers, "generate", fake_generate)
        monkeypatch.setattr(_providers, "get_key_env", lambda p: "PATH")
        ledger = _judge._Ledger()
        v = _judge._call_judge("openai", "m", "sys", "usr", "judge.fidelity",
                               ledger, "run-x")
        assert v == {"faithful": True}
        assert len(calls) == 2
        assert "CORRECTION" in calls[1]
        assert ledger.calls == 2

    def test_provider_failure_records_the_error(self, monkeypatch):
        import providers as _providers

        def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(_providers, "generate", boom)
        monkeypatch.setattr(_providers, "get_key_env", lambda p: "PATH")
        ledger = _judge._Ledger()
        assert _judge._call_judge("openai", "m", "s", "u", "judge.map",
                                  ledger, "run-x") is None
        assert ledger.errors and "provider down" in ledger.errors[0]


class TestStorage:
    def test_store_and_list_round_trip(self, run_dir, monkeypatch, tmp_path):
        payload, _ = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}],
            "judge.map": [{"score": 0.9}],
            "judge.chain": [{"earned": True}],
        })
        conn = sqlite3.connect(str(tmp_path / "registry.db"))
        store_judgement(conn, payload)
        got = list_judgements(conn, "run-j")
        assert len(got) == 1
        assert got[0]["scores"]["citation_fidelity"]["score"] == 1.0
        assert got[0]["config"]["votes"] == 1
        conn.close()

    def test_two_configs_keep_two_rows(self, run_dir, monkeypatch, tmp_path):
        p1, _ = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}],
            "judge.map": [{"score": 0.9}], "judge.chain": [{"earned": True}],
        })
        p2, _ = _run(run_dir, monkeypatch, {
            "judge.fidelity": [{"faithful": True}],
            "judge.map": [{"score": 0.4}], "judge.chain": [{"earned": True}],
        }, votes=3)
        p2["judged_at"] = p1["judged_at"] + "x"   # distinct key
        conn = sqlite3.connect(str(tmp_path / "registry.db"))
        store_judgement(conn, p1)
        store_judgement(conn, p2)
        assert len(list_judgements(conn, "run-j")) == 2
        conn.close()
