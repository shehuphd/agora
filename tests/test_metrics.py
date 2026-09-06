"""Run metrics computed from recorded data — core/metrics.py."""
import json
import sqlite3

import pytest

from core import metrics as _metrics
from core import runs_db as _runs_db


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(_runs_db, "DATABASES_DIR", tmp_path / "databases")
    monkeypatch.setattr(_runs_db, "RUNS_DB_PATH", tmp_path / "databases" / "runs.db")
    yield


def _make_run_dir(tmp_path, acts, search_lines=None, with_new_cols=True):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(run_dir / "debate.db"))
    cols = ("act_id TEXT, act_type TEXT, content TEXT"
            + (", challenge_type TEXT, retries INTEGER" if with_new_cols else ""))
    conn.execute(f"CREATE TABLE acts ({cols})")
    for i, a in enumerate(acts):
        if with_new_cols:
            conn.execute("INSERT INTO acts VALUES (?,?,?,?,?)",
                         (f"a{i}", a["type"], a.get("content", ""),
                          a.get("challenge_type"), a.get("retries")))
        else:
            conn.execute("INSERT INTO acts VALUES (?,?,?)",
                         (f"a{i}", a["type"], a.get("content", "")))
    conn.commit()
    conn.close()
    if search_lines is not None:
        (run_dir / "search_log.jsonl").write_text(
            "\n".join(json.dumps(l) for l in search_lines))
    return run_dir


_IDX_ROW = {"status": "closed", "closure_reason": "token_budget", "turn": 7,
            "total_tokens": 42000, "total_cost_usd": 1.25, "cost_partial": 0}


class TestCompute:
    def test_full_run(self, tmp_path):
        acts = [
            {"type": "ASSERT",  "content": "claim with https://example.org/a source", "retries": 0},
            {"type": "CHALLENGE", "challenge_type": "sourcing", "retries": 1},
            {"type": "CHALLENGE", "challenge_type": "premise", "retries": 0},
            {"type": "CHALLENGE", "challenge_type": "multi", "retries": 0},
            {"type": "DEFEND",  "content": "no source here", "retries": 0},
            {"type": "CONCEDE", "retries": 0},
            {"type": "ARGUMENT_MAP", "content": "{\"map\": true}", "retries": 2},
        ]
        run_dir = _make_run_dir(tmp_path, acts, search_lines=[
            {"tier": "searxng", "query": "q1"},
            {"tier": "serper",  "query": "q2"},
            {"tier": "serper",  "query": "q3"},
        ])
        m = _metrics.compute(run_dir, _IDX_ROW)
        assert m["completed"] == 1
        assert m["closure_reason"] == "token_budget"
        assert m["turns"] == 7
        assert m["challenge_count"] == 3
        # "multi" is a bundle, not a taxonomy entry.
        assert m["challenge_types_used"] == 2
        assert m["concession_count"] == 1
        assert m["citable_acts"] == 2
        assert m["cited_acts"] == 1
        assert m["citation_coverage"] == 0.5
        assert m["retries"] == 3
        assert m["argument_map_ok"] == 1
        assert m["search_calls"] == 3
        assert json.loads(m["search_tiers"]) == ["searxng", "serper"]

    def test_old_schema_degrades_to_unknown_not_zero(self, tmp_path):
        """Acts recorded before challenge_type/retries existed: those metrics
        are unknown, and reporting 0 would misread the history."""
        acts = [{"type": "CHALLENGE"}, {"type": "ASSERT", "content": "x"}]
        run_dir = _make_run_dir(tmp_path, acts, with_new_cols=False)
        m = _metrics.compute(run_dir, _IDX_ROW)
        assert m["challenge_count"] == 1
        assert m["challenge_types_used"] is None
        assert m["retries"] is None

    def test_zero_challenges_is_a_known_zero(self, tmp_path):
        run_dir = _make_run_dir(tmp_path, [{"type": "ASSERT", "content": "x"}])
        m = _metrics.compute(run_dir, _IDX_ROW)
        assert m["challenge_types_used"] == 0

    def test_empty_argument_map_is_not_ok(self, tmp_path):
        run_dir = _make_run_dir(tmp_path, [{"type": "ARGUMENT_MAP", "content": "  "}])
        assert _metrics.compute(run_dir, _IDX_ROW)["argument_map_ok"] == 0

    def test_missing_files_degrade_to_none(self, tmp_path):
        empty = tmp_path / "nothing"
        empty.mkdir()
        m = _metrics.compute(empty, _IDX_ROW)
        assert m["challenge_count"] is None
        assert m["search_calls"] is None
        assert m["completed"] == 1  # index-row fields still work

    def test_error_status_is_not_completed(self, tmp_path):
        run_dir = _make_run_dir(tmp_path, [])
        m = _metrics.compute(run_dir, {**_IDX_ROW, "status": "error"})
        assert m["completed"] == 0


class TestStoreAndBackfill:
    def _index_run(self, run_id, run_dir_name, status="closed"):
        conn = _runs_db.connect()
        _runs_db.init(conn)
        conn.execute(
            "INSERT INTO runs (run_id, run_dir, status, turn, total_tokens) VALUES (?,?,?,?,?)",
            (run_id, run_dir_name, status, 3, 1000),
        )
        conn.commit()
        conn.close()

    def test_compute_and_store_roundtrip(self, tmp_path):
        run_dir = _make_run_dir(tmp_path, [{"type": "ASSERT", "content": "https://x.org y"}])
        self._index_run("r1", "run")
        assert _metrics.compute_and_store("r1", run_dir) is True
        conn = _runs_db.connect()
        got = _runs_db.get_run_metrics_map(conn, ["r1"])
        conn.close()
        assert got["r1"]["citation_coverage"] == 1.0

    def test_running_runs_are_not_measured(self, tmp_path):
        run_dir = _make_run_dir(tmp_path, [])
        self._index_run("r2", "run", status="running")
        assert _metrics.compute_and_store("r2", run_dir) is False

    def test_backfill_covers_only_missing(self, tmp_path):
        _make_run_dir(tmp_path, [{"type": "ASSERT", "content": "x"}])
        self._index_run("r3", "run")
        self._index_run("r4", "run")
        # r3 already has metrics; only r4 should be backfilled.
        conn = _runs_db.connect()
        _runs_db.upsert_run_metrics(conn, "r3", {"completed": 1})
        missing = [m["run_id"] for m in _runs_db.runs_missing_metrics(conn)]
        conn.close()
        assert missing == ["r4"]
        assert _metrics.backfill_missing(tmp_path) == 1
