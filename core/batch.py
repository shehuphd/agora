"""Batch job queue — runs multiple debates sequentially from a CSV import."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

RowStatus  = Literal["pending", "running", "done", "failed"]
JobStatus  = Literal["queued", "running", "done", "failed"]


@dataclass
class BatchRow:
    row_idx:     int
    topic:       str
    config:      dict
    status:      RowStatus = "pending"
    run_id:      str | None = None
    error:       str | None = None
    started_at:  str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "row_idx":     self.row_idx,
            "topic":       self.topic,
            "status":      self.status,
            "run_id":      self.run_id,
            "error":       self.error,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class BatchJob:
    job_id:        str
    experiment_id: str | None
    rows:          list[BatchRow]
    status:        JobStatus = "queued"
    created_at:    str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        done    = sum(1 for r in self.rows if r.status == "done")
        failed  = sum(1 for r in self.rows if r.status == "failed")
        running = sum(1 for r in self.rows if r.status == "running")
        pending = sum(1 for r in self.rows if r.status == "pending")
        return {
            "job_id":        self.job_id,
            "experiment_id": self.experiment_id,
            "status":        self.status,
            "created_at":    self.created_at,
            "total":         len(self.rows),
            "done":          done,
            "failed":        failed,
            "running":       running,
            "pending":       pending,
            "rows":          [r.to_dict() for r in self.rows],
        }


_jobs:  dict[str, BatchJob] = {}
_queue: asyncio.Queue | None = None


def create_job(experiment_id: str | None, rows_data: list[dict]) -> BatchJob:
    job_id = str(uuid.uuid4())
    rows = [
        BatchRow(row_idx=i, topic=r.get("topic", ""), config=r)
        for i, r in enumerate(rows_data)
    ]
    job = BatchJob(job_id=job_id, experiment_id=experiment_id, rows=rows)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> BatchJob | None:
    return _jobs.get(job_id)


async def enqueue(job: BatchJob) -> None:
    if _queue is None:
        raise RuntimeError("Batch worker not started")
    await _queue.put(job.job_id)


async def start_worker() -> None:
    global _queue
    _queue = asyncio.Queue()
    asyncio.create_task(_worker(), name="batch-worker")


async def _worker() -> None:
    while True:
        job_id: str = await _queue.get()
        job = _jobs.get(job_id)
        if job is None:
            continue
        job.status = "running"
        # Run every pending row concurrently — each debate is independent.
        pending = [r for r in job.rows if r.status == "pending"]
        await asyncio.gather(*(_run_row(job, r) for r in pending))
        job.status = "done" if all(r.status == "done" for r in job.rows) else "failed"
        _prune_jobs()


_KEEP_FINISHED_JOBS = 20


def _prune_jobs() -> None:
    """Drop all but the most recent finished jobs so _jobs cannot grow unbounded."""
    finished = [j for j in _jobs.values() if j.status in ("done", "failed")]
    finished.sort(key=lambda j: j.created_at)
    for job in finished[:-_KEEP_FINISHED_JOBS] if len(finished) > _KEEP_FINISHED_JOBS else []:
        _jobs.pop(job.job_id, None)


async def _run_row(job: BatchJob, row: BatchRow) -> None:
    row.status = "running"
    row.started_at = datetime.utcnow().isoformat()
    try:
        import uuid as _uuid
        from pathlib import Path
        from api.models import DebateConfig
        from api.routers.debates import (
            _run_debate_wrapper, _run_queues, _pause_events,
            _force_close_events, _overrides, _override_logs,
            RUNS_DIR, _make_run_dir_name,
        )
        from api.routers.settings import _load_config as _load_agora_config
        from core import runs_db as _runs_db

        config = DebateConfig(**row.config)

        agora_cfg = _load_agora_config()
        provider_order = agora_cfg.get("providers", {}).get("model_order")
        conn = _runs_db.connect()
        try:
            available = _runs_db.list_available_models(conn, provider_order=provider_order)
            exp_name: str | None = None
            if job.experiment_id:
                exp = _runs_db.get_experiment(conn, job.experiment_id)
                exp_name = exp["name"] if exp else None
        finally:
            conn.close()

        if not available:
            raise ValueError("No models available — configure an API key in Settings first")

        first_available = available[0]["model_id"]

        from core.config import DebateRunConfig
        run_id = str(_uuid.uuid4())
        row.run_id = run_id
        run_cfg = DebateRunConfig.from_api(config, first_available=first_available)
        run_dir = RUNS_DIR / _make_run_dir_name(run_cfg.topic)

        # Unbounded: no browser SSE client is guaranteed to drain a batch run's
        # queue, and the runner's puts block once a bounded queue fills — a
        # debate emitting >200 events would deadlock the whole batch. Events are
        # small dicts and the queue is dropped by _cleanup_run when the run ends,
        # and a user clicking through mid-run still gets the full backlog live.
        q: asyncio.Queue = asyncio.Queue()
        pause_event = asyncio.Event()
        pause_event.set()
        force_close_event = asyncio.Event()
        overrides: dict = {}

        _run_queues[run_id]         = q
        _pause_events[run_id]       = pause_event
        _force_close_events[run_id] = force_close_event
        _overrides[run_id]          = overrides
        _override_logs[run_id]      = []

        await _run_debate_wrapper(
            run_id, run_cfg, run_dir, q,
            pause_event, overrides, force_close_event,
            experiment_name=exp_name,
        )
        row.status = "done"
    except Exception as exc:
        row.status = "failed"
        row.error = str(exc)
    finally:
        row.finished_at = datetime.utcnow().isoformat()
