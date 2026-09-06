"""Condition comparison grouping and condition threading through the index."""
import json

import pytest

from api.routers.experiments import _comparison_groups
from core import runs_db as _runs_db


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(_runs_db, "DATABASES_DIR", tmp_path / "databases")
    monkeypatch.setattr(_runs_db, "RUNS_DB_PATH", tmp_path / "databases" / "runs.db")
    yield


def _run(run_id, cond, status="closed", tokens=1000, cost=0.5, turn=5):
    return {"run_id": run_id, "condition": cond, "status": status,
            "total_tokens": tokens, "total_cost_usd": cost, "turn": turn}


class TestGrouping:
    def test_replicates_group_and_aggregate(self):
        runs = [
            _run("r1", {"proposition_model": "a", "replicate": 1}, tokens=1000, cost=0.4),
            _run("r2", {"proposition_model": "a", "replicate": 2}, tokens=3000, cost=0.6),
            _run("r3", {"proposition_model": "b", "replicate": 1}, status="error"),
        ]
        metrics = {
            "r1": {"citation_coverage": 0.5, "retries": 0},
            "r2": {"citation_coverage": 1.0, "retries": 2},
        }
        groups = _comparison_groups(runs, metrics)
        assert len(groups) == 2
        a = next(g for g in groups if g["condition"].get("proposition_model") == "a")
        assert a["n"] == 2
        assert a["completed"] == 2
        assert a["tokens"]["mean"] == 2000
        assert a["tokens"]["min"] == 1000 and a["tokens"]["max"] == 3000
        assert a["cost_usd"]["mean"] == pytest.approx(0.5)
        assert a["citation_coverage"]["mean"] == pytest.approx(0.75)
        b = next(g for g in groups if g["condition"].get("proposition_model") == "b")
        assert b["completed"] == 0

    def test_unpriced_values_are_excluded_not_zeroed(self):
        runs = [
            _run("r1", {"m": "x", "replicate": 1}, cost=None),
            _run("r2", {"m": "x", "replicate": 2}, cost=0.8),
        ]
        g = _comparison_groups(runs, {})[0]
        # One priced run: the mean is over what's known, and n says so.
        assert g["cost_usd"]["mean"] == pytest.approx(0.8)
        assert g["cost_usd"]["n"] == 1

    def test_all_unknown_metric_is_none(self):
        runs = [_run("r1", {"m": "x", "replicate": 1}, cost=None)]
        assert _comparison_groups(runs, {})[0]["cost_usd"] is None

    def test_unlabelled_runs_form_their_own_group(self):
        runs = [_run("r1", None), _run("r2", {"m": "x", "replicate": 1})]
        groups = _comparison_groups(runs, {})
        assert len(groups) == 2


class TestConditionThreading:
    def test_condition_survives_insert_and_listing(self, tmp_path):
        conn = _runs_db.connect()
        _runs_db.init(conn)
        _runs_db.create_experiment(conn, experiment_id="e1", name="exp",
                                   description=None, created_at="2026-09-02T00:00:00")
        cond = json.dumps({"synth_model": "sonar", "replicate": 2})
        _runs_db.insert_run(
            conn, run_id="r1", run_dir="d1", created_at="2026-09-02T00:00:00",
            debate_title="t", topic="t", steelman_mode=False,
            proposition_nickname="P", opposition_nickname="O",
            condition=cond,
        )
        _runs_db.assign_run(conn, "r1", "e1")
        runs = _runs_db.list_experiment_runs(conn, "e1", tmp_path)
        conn.close()
        assert runs[0]["condition"] == {"synth_model": "sonar", "replicate": 2}

    def test_spec_and_manifest_roundtrip(self, tmp_path):
        conn = _runs_db.connect()
        _runs_db.init(conn)
        _runs_db.create_experiment(conn, experiment_id="e2", name="exp2",
                                   description=None, created_at="2026-09-02T00:00:00")
        _runs_db.set_experiment_spec(conn, "e2", json.dumps({"rows": [{"topic": "x"}]}))
        _runs_db.set_experiment_manifest(conn, "e2", json.dumps({"rates": "0.0.4"}))
        exp = _runs_db.get_experiment(conn, "e2")
        conn.close()
        assert json.loads(exp["spec"])["rows"][0]["topic"] == "x"
        assert json.loads(exp["manifest"])["rates"] == "0.0.4"
