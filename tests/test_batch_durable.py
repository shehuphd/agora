"""Durable batch state: persistence, interruption stamping, retry, budget."""
import pytest

from core import batch as _batch
from core import runs_db as _runs_db


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway database per test."""
    monkeypatch.setattr(_runs_db, "DATABASES_DIR", tmp_path)
    monkeypatch.setattr(_runs_db, "RUNS_DB_PATH", tmp_path / "runs.db")
    yield


def _job_rows():
    return [
        {"topic": "cats", "token_budget": 1000},
        {"topic": "dogs", "token_budget": 2000,
         "_condition": {"proposition_model": "gpt-4.1", "replicate": 1}},
    ]


class TestPersistence:
    def test_job_survives_a_fresh_connection(self):
        """The whole point of the rewrite: job state must live in SQLite,
        not in the memory of the process that created it."""
        job_id = _batch.create_job("exp-1", _job_rows(), budget_usd=5.0)
        job = _batch.get_job(job_id)
        assert job is not None
        assert job["experiment_id"] == "exp-1"
        assert job["budget_usd"] == 5.0
        assert job["total"] == 2
        assert job["pending"] == 2
        assert job["rows"][0]["topic"] == "cats"
        assert job["rows"][1]["condition"] == {"proposition_model": "gpt-4.1", "replicate": 1}
        # The condition never leaks into the stored config.
        assert "_condition" not in job["rows"][1]["config"]

    def test_missing_job_is_none(self):
        assert _batch.get_job("nope") is None

    def test_row_updates_persist(self):
        job_id = _batch.create_job(None, _job_rows())
        conn = _runs_db.connect()
        _runs_db.init(conn)
        _runs_db.update_batch_row(conn, job_id, 0, status="done", run_id="r1")
        conn.close()
        job = _batch.get_job(job_id)
        assert job["done"] == 1
        assert job["rows"][0]["run_id"] == "r1"


class TestInterruption:
    def test_startup_stamps_midflight_rows(self):
        job_id = _batch.create_job(None, _job_rows())
        conn = _runs_db.connect()
        _runs_db.init(conn)
        _runs_db.set_batch_job_status(conn, job_id, "running")
        _runs_db.update_batch_row(conn, job_id, 0, status="running")
        stamped = _runs_db.mark_interrupted_batches(conn)
        conn.close()
        assert stamped == 2  # the running row AND the still-pending one
        job = _batch.get_job(job_id)
        assert job["status"] == "interrupted"
        assert job["interrupted"] == 2
        assert "server stopped" in job["rows"][0]["error"]

    def test_finished_jobs_are_left_alone(self):
        job_id = _batch.create_job(None, _job_rows())
        conn = _runs_db.connect()
        _runs_db.init(conn)
        _runs_db.update_batch_row(conn, job_id, 0, status="done")
        _runs_db.update_batch_row(conn, job_id, 1, status="done")
        _runs_db.set_batch_job_status(conn, job_id, "done")
        stamped = _runs_db.mark_interrupted_batches(conn)
        conn.close()
        assert stamped == 0
        assert _batch.get_job(job_id)["status"] == "done"


class TestRetry:
    def _finished_job(self, statuses):
        job_id = _batch.create_job("exp-9", _job_rows(), budget_usd=3.0)
        conn = _runs_db.connect()
        _runs_db.init(conn)
        for idx, status in enumerate(statuses):
            _runs_db.update_batch_row(conn, job_id, idx, status=status)
        _runs_db.set_batch_job_status(conn, job_id, "failed")
        conn.close()
        return job_id

    def test_retry_covers_only_unfinished_rows(self):
        job_id = self._finished_job(["done", "failed"])
        new_id = _batch.create_retry_job(job_id)
        assert new_id is not None and new_id != job_id
        new = _batch.get_job(new_id)
        assert new["total"] == 1
        assert new["rows"][0]["topic"] == "dogs"
        # Experiment, budget, and condition all carry over.
        assert new["experiment_id"] == "exp-9"
        assert new["budget_usd"] == 3.0
        assert new["rows"][0]["condition"]["proposition_model"] == "gpt-4.1"

    def test_nothing_to_retry_returns_none(self):
        job_id = self._finished_job(["done", "done"])
        assert _batch.create_retry_job(job_id) is None

    def test_interrupted_and_skipped_are_retryable(self):
        job_id = self._finished_job(["interrupted", "skipped"])
        new = _batch.get_job(_batch.create_retry_job(job_id))
        assert new["total"] == 2


class TestBudget:
    def test_spend_sums_closed_runs_only(self):
        job_id = _batch.create_job(None, _job_rows(), budget_usd=1.0)
        conn = _runs_db.connect()
        _runs_db.init(conn)
        for rid, cost, status in (("r1", 0.4, "closed"), ("r2", 0.3, "running")):
            conn.execute(
                "INSERT INTO runs (run_id, status, total_cost_usd) VALUES (?,?,?)",
                (rid, status, cost),
            )
        _runs_db.update_batch_row(conn, job_id, 0, status="done", run_id="r1")
        _runs_db.update_batch_row(conn, job_id, 1, status="running", run_id="r2")
        spent, partial = _runs_db.batch_job_spent_usd(conn, job_id)
        conn.close()
        assert spent == pytest.approx(0.4)   # the running run is not counted yet
        assert partial is False

    def test_unpriced_run_makes_the_total_partial(self):
        """An unpriced run is unknown spend, not free — the ceiling check
        must know the total it compares is a minimum."""
        job_id = _batch.create_job(None, _job_rows())
        conn = _runs_db.connect()
        _runs_db.init(conn)
        conn.execute(
            "INSERT INTO runs (run_id, status, total_cost_usd) VALUES ('r1', 'closed', NULL)")
        _runs_db.update_batch_row(conn, job_id, 0, status="done", run_id="r1")
        spent, partial = _runs_db.batch_job_spent_usd(conn, job_id)
        conn.close()
        assert spent == 0.0
        assert partial is True


class TestConcurrencyConfig:
    def test_clamped_to_supported_range(self, monkeypatch, tmp_path):
        import yaml as _yaml
        for configured, expected in ((0, 1), (1, 1), (3, 3), (50, 3)):
            cfg_dir = tmp_path / f"c{configured}"
            (cfg_dir / "config").mkdir(parents=True)
            (cfg_dir / "config" / "defaults.yaml").write_text(
                _yaml.safe_dump({"batch": {"concurrency": configured}})
            )
            monkeypatch.setattr(
                "core.batch.__file__", str(cfg_dir / "core" / "batch.py"),
            )
            assert _batch._configured_concurrency() == expected

    def test_missing_section_falls_back_to_default(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "defaults.yaml").write_text("protocol:\n  max_turns: 100\n")
        monkeypatch.setattr("core.batch.__file__", str(tmp_path / "core" / "batch.py"))
        assert _batch._configured_concurrency() == 2

    def test_configured_value_reads_from_defaults_yaml(self):
        # The repo's own defaults.yaml carries the signed-off default of 2.
        assert _batch._configured_concurrency() == 2
