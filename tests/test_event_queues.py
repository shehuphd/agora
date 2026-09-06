"""A debate's event queue must never be able to wedge the run.

The runner awaits every event put, and no SSE client is guaranteed to be
attached — a run created over the API and never opened in a browser has no
consumer at all. Two live 100-turn runs (2026-09-05) froze at precisely ~200
events, mid-turn, status "running", no error anywhere: the create endpoint's
queue was bounded at 200. Every queue a runner writes to must be unbounded;
the batch path already was.
"""
import asyncio

from fastapi.testclient import TestClient

from api.main import app
from api.routers import debates as _debates


def test_created_debates_get_an_unbounded_event_queue(monkeypatch, tmp_path):
    async def fake_wrapper(*args, **kwargs):
        return None

    monkeypatch.setattr(_debates, "_run_debate_wrapper", fake_wrapper)
    monkeypatch.setattr(_debates, "_resolve_run_models", lambda cfg: cfg)
    monkeypatch.setattr(_debates, "RUNS_DIR", tmp_path)
    from core import runs_db as _runs_db
    monkeypatch.setattr(
        _runs_db, "list_available_models",
        lambda conn, provider_order=None: [{"model_id": "fake-model", "provider": "openai"}],
    )
    monkeypatch.setattr(_runs_db, "DATABASES_DIR", tmp_path / "databases")
    monkeypatch.setattr(_runs_db, "RUNS_DB_PATH", tmp_path / "databases" / "runs.db")

    # Plain client, no lifespan: the endpoint needs no app startup, and the
    # startup's backfill thread would leak into later tests under random
    # ordering.
    client = TestClient(app)
    resp = client.post("/debates", json={
        "topic": "Queue bounds must not wedge runs",
        "proposition_model": "fake-model",
        "opposition_model": "fake-model",
        "moderator_model": "fake-model",
        "synthesiser_model": "fake-model",
    })
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    queue = _debates._run_queues[run_id]
    assert isinstance(queue, asyncio.Queue)
    assert queue.maxsize == 0, (
        "bounded event queue: with no SSE consumer the runner's puts block "
        "once it fills, freezing the debate mid-turn"
    )
    _debates._cleanup_run(run_id)
