"""Judge endpoints: estimate, start, poll — defaults from config, closed runs
only, no double-starts, judgements stored and listed."""
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routers import debates as _debates
from core import judge as _judge_mod
from core import runs_db as _runs_db
from core.checkpoint import init_db, write_act_to_db
from core.judge import JudgeConfig
from core.state import Act

_URL = "https://example.org/study"


def _client() -> TestClient:
    """Plain client, no lifespan: these endpoints need no app startup, and
    the startup's backfill thread would leak into later tests under random
    ordering."""
    return TestClient(app)


def _act(turn, role, act_type, content="c", citations=None):
    return Act(
        act_id=str(uuid.uuid4()), run_id="run-e", turn=turn, agent=role.title(),
        agent_role=role, act_type=act_type, claim_id=None, target_act_id=None,
        content=content, reason=None, input_tokens=1, output_tokens=1,
        model_used="m", timestamp=datetime.utcnow().isoformat(),
        citations=citations,
    )


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """A closed run on disk plus a tmp registry, with the endpoints pointed
    at both and provider resolution faked."""
    run_dir = tmp_path / "runs" / "20260905_test_run"
    run_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(run_dir / "debate.db"))
    init_db(conn)
    conn.execute(
        "INSERT INTO runs (run_id, created_at, status) VALUES (?,?,?)",
        ("run-e", datetime.utcnow().isoformat(), "closed"),
    )
    write_act_to_db(conn, _act(
        0, "proposition", "ASSERT",
        content=f"Claim with 12% ([s]({_URL})).",
        citations=[{"url": _URL, "quote": "rose by 12%", "status": "verified",
                    "ungrounded_numbers": []}],
    ))
    write_act_to_db(conn, _act(1, "synthesiser", "ARGUMENT_MAP",
                               content=json.dumps({"surviving_claims": []})))
    conn.commit()
    conn.close()

    monkeypatch.setattr(_debates, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(_runs_db, "DATABASES_DIR", tmp_path / "databases")
    monkeypatch.setattr(_runs_db, "RUNS_DB_PATH", tmp_path / "databases" / "runs.db")
    monkeypatch.setattr(_judge_mod, "default_config", lambda conn: JudgeConfig(
        fidelity_model="fid-m", fidelity_provider="openai",
        map_model="map-m", map_provider="openai", votes=1,
    ))
    _debates._judging.discard("run-e")
    return run_dir


class TestEstimate:
    def test_estimate_counts_and_prices(self, wired, monkeypatch):
        monkeypatch.setattr(_judge_mod._cost, "cost_usd", lambda *a: 0.001)
        monkeypatch.setattr(_judge_mod._cost, "snapshot_date", lambda: "2026-09-01")
        client = _client()
        r = client.get("/debates/run-e/judge/estimate")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verified_citations"] == 1
        assert body["map"] is True
        assert body["estimate_usd"] is not None
        assert body["prices_as_of"] == "2026-09-01"

    def test_unknown_run_404s(self, wired):
        client = _client()
        r = client.get("/debates/nope/judge/estimate")
        assert r.status_code == 404

    def test_running_debate_refused(self, wired):
        conn = sqlite3.connect(str(wired / "debate.db"))
        conn.execute("UPDATE runs SET status='running' WHERE run_id='run-e'")
        conn.commit(); conn.close()
        client = _client()
        r = client.get("/debates/run-e/judge/estimate")
        assert r.status_code == 409
        assert "still running" in r.json()["detail"]


class TestJudgeStart:
    def test_judge_runs_and_stores(self, wired, monkeypatch):
        def fake_judge_run(run_dir, run_id, config):
            return {
                "run_id": run_id,
                "judged_at": datetime.utcnow().isoformat(),
                "config": config.to_dict(),
                "citation_fidelity": {"score": 1.0}, "map_quality": {"score": 0.9},
                "challenge_resolution": {"score": None},
                "ledger": {"calls": 2, "input_tokens": 10, "output_tokens": 5,
                           "cost_usd": 0.01, "cost_partial": False, "errors": []},
            }

        monkeypatch.setattr(_judge_mod, "judge_run", fake_judge_run)
        client = _client()
        r = client.post("/debates/run-e/judge")
        assert r.status_code == 200, r.text
        assert r.json()["started"] is True
        # TestClient runs background tasks before returning, so the
        # judgement is stored by the time the next request arrives.
        r2 = client.get("/debates/run-e/judgements")
        body = r2.json()
        assert body["in_progress"] is False
        assert len(body["judgements"]) == 1
        assert body["judgements"][0]["scores"]["citation_fidelity"]["score"] == 1.0

    def test_double_start_refused(self, wired, monkeypatch):
        monkeypatch.setattr(_judge_mod, "judge_run",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
        _debates._judging.add("run-e")
        try:
            client = _client()
            r = client.post("/debates/run-e/judge")
            assert r.status_code == 409
            assert "already in progress" in r.json()["detail"]
        finally:
            _debates._judging.discard("run-e")

    def test_failed_judgement_clears_the_in_flight_marker(self, wired, monkeypatch):
        def boom(*a):
            raise RuntimeError("provider down")

        monkeypatch.setattr(_judge_mod, "judge_run", boom)
        client = _client()
        r = client.post("/debates/run-e/judge")
        assert r.status_code == 200
        r2 = client.get("/debates/run-e/judgements")
        assert r2.json()["in_progress"] is False
        assert r2.json()["judgements"] == []
