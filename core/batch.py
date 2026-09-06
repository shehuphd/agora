"""Batch job engine — runs multiple debates from a CSV import or a spec
expansion, a bounded number at a time.

Jobs and rows live in the registry database (core/runs_db.py), not in
process memory: a server restart mid-batch leaves an inspectable record,
and the worker stamps anything it finds mid-flight at startup as
'interrupted' so those rows can be retried. Concurrency is capped by
batch.concurrency in config/defaults.yaml (clamped to 1–3), and a job may
carry an optional spend ceiling checked between row launches against the
recorded cost of its closed runs.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from core import runs_db as _runs_db

_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 3
_DEFAULT_CONCURRENCY = 2

_queue: asyncio.Queue | None = None

# Row statuses that a retry job picks up: everything that did not finish.
_RETRYABLE = ("failed", "interrupted", "skipped")


def _configured_concurrency() -> int:
    """batch.concurrency from defaults.yaml, clamped to the supported range."""
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config" / "defaults.yaml"
        data = yaml.safe_load(cfg_path.read_text()) or {}
        value = int((data.get("batch") or {}).get("concurrency", _DEFAULT_CONCURRENCY))
    except Exception:
        value = _DEFAULT_CONCURRENCY
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, value))


def create_job(
    experiment_id: str | None,
    rows_data: list[dict],
    budget_usd: float | None = None,
) -> str:
    """Persist a new job and return its id.

    Each rows_data entry is a config dict; a "_condition" key, when present,
    is split out as the row's condition labels rather than stored as config.
    """
    job_id = str(uuid.uuid4())
    rows = []
    for r in rows_data:
        condition = r.pop("_condition", None)
        rows.append({"topic": r.get("topic", ""), "config": r, "condition": condition})
    conn = _runs_db.connect()
    try:
        _runs_db.init(conn)
        _runs_db.insert_batch_job(
            conn, job_id=job_id, experiment_id=experiment_id,
            rows=rows, budget_usd=budget_usd,
        )
    finally:
        conn.close()
    return job_id


def get_job(job_id: str) -> dict | None:
    conn = _runs_db.connect()
    try:
        _runs_db.init(conn)
        return _runs_db.get_batch_job(conn, job_id)
    finally:
        conn.close()


def create_retry_job(job_id: str) -> str | None:
    """New job covering the unfinished rows of an earlier one.

    Copies failed, interrupted, and budget-skipped rows into a fresh job
    under the same experiment and budget ceiling. Returns the new job id,
    or None when the source job doesn't exist or has nothing to retry.
    """
    job = get_job(job_id)
    if job is None:
        return None
    retry_rows = [r for r in job["rows"] if r["status"] in _RETRYABLE]
    if not retry_rows:
        return None
    rows_data = []
    for r in retry_rows:
        cfg = dict(r["config"])
        if r["condition"]:
            cfg["_condition"] = r["condition"]
        rows_data.append(cfg)
    return create_job(job["experiment_id"], rows_data, budget_usd=job["budget_usd"])


async def enqueue(job_id: str) -> None:
    if _queue is None:
        raise RuntimeError("Batch worker not started")
    await _queue.put(job_id)


async def start_worker() -> None:
    global _queue
    _queue = asyncio.Queue()
    # Anything mid-flight in the database belongs to a previous process.
    try:
        conn = _runs_db.connect()
        try:
            _runs_db.init(conn)
            stamped = _runs_db.mark_interrupted_batches(conn)
            if stamped:
                print(f"[batch] stamped {stamped} row(s) from a previous process as interrupted", flush=True)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[batch] interrupted-batch scan failed: {exc}", flush=True)
    asyncio.create_task(_worker(), name="batch-worker")


async def _worker() -> None:
    while True:
        job_id: str = await _queue.get()
        try:
            await _run_job(job_id)
        except Exception as exc:
            print(f"[batch] job {job_id} failed: {exc}", flush=True)
            _set_job_status(job_id, "failed")


def _set_job_status(job_id: str, status: str, finished: bool = False) -> None:
    conn = _runs_db.connect()
    try:
        _runs_db.init(conn)
        _runs_db.set_batch_job_status(
            conn, job_id, status,
            finished_at=datetime.utcnow().isoformat() if finished else None,
        )
    finally:
        conn.close()


def _update_row(job_id: str, row_idx: int, **fields) -> None:
    conn = _runs_db.connect()
    try:
        _runs_db.init(conn)
        _runs_db.update_batch_row(conn, job_id, row_idx, **fields)
    finally:
        conn.close()


def _over_budget(job: dict) -> tuple[bool, float]:
    """Whether the job's recorded spend has reached its ceiling."""
    if job.get("budget_usd") is None:
        return False, 0.0
    conn = _runs_db.connect()
    try:
        _runs_db.init(conn)
        spent, _partial = _runs_db.batch_job_spent_usd(conn, job["job_id"])
    finally:
        conn.close()
    return spent >= job["budget_usd"], spent


async def _run_job(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    _set_job_status(job_id, "running")

    concurrency = _configured_concurrency()
    sem = asyncio.Semaphore(concurrency)
    pending = [r for r in job["rows"] if r["status"] == "pending"]

    async def _guarded(row: dict) -> None:
        async with sem:
            # Budget check between launches, not mid-debate: a running row is
            # spend already committed, and killing it would waste the dollars
            # the ceiling is there to protect.
            over, spent = _over_budget(job)
            if over:
                _update_row(
                    job_id, row["row_idx"], status="skipped",
                    error=(f"budget ceiling reached: ${spent:.2f} of "
                           f"${job['budget_usd']:.2f} recorded before this row launched"),
                    finished_at=datetime.utcnow().isoformat(),
                )
                return
            await _run_row(job, row)

    await asyncio.gather(*(_guarded(r) for r in pending))

    # Budget-skipped rows are the ceiling working as designed, not a failure;
    # the row counts carry the distinction to the UI either way.
    final = get_job(job_id)
    if final and final["failed"] == 0 and final["interrupted"] == 0:
        _set_job_status(job_id, "done", finished=True)
    else:
        _set_job_status(job_id, "failed", finished=True)


async def _run_row(job: dict, row: dict) -> None:
    job_id, row_idx = job["job_id"], row["row_idx"]
    _update_row(job_id, row_idx, status="running",
                started_at=datetime.utcnow().isoformat())
    try:
        import uuid as _uuid
        from api.models import DebateConfig
        from api.routers.debates import (
            _run_debate_wrapper, _run_queues, _pause_events,
            _force_close_events, _overrides, _override_logs,
            _resolve_run_models, RUNS_DIR, _make_run_dir_name,
        )
        from api.routers.settings import _load_config as _load_agora_config
        from core import runs_db as _rdb

        config = DebateConfig(**row["config"])

        agora_cfg = _load_agora_config()
        provider_order = agora_cfg.get("providers", {}).get("model_order")
        conn = _rdb.connect()
        try:
            available = _rdb.list_available_models(conn, provider_order=provider_order)
            exp_name: str | None = None
            if job["experiment_id"]:
                exp = _rdb.get_experiment(conn, job["experiment_id"])
                exp_name = exp["name"] if exp else None
        finally:
            conn.close()

        if not available:
            raise ValueError("No models available — configure an API key in Settings first")

        first_available = available[0]["model_id"]

        from core.config import DebateRunConfig
        run_id = str(_uuid.uuid4())
        _update_row(job_id, row_idx, status="running", run_id=run_id)
        run_cfg = DebateRunConfig.from_api(config, first_available=first_available)
        # Same resolution the API path uses, so a CSV row naming an unroutable
        # or ambiguous model fails as a row error with a readable reason
        # instead of constructing an agent with nowhere to send its calls.
        run_cfg = _resolve_run_models(run_cfg)
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
            condition=json.dumps(row["condition"]) if row["condition"] else None,
            # No human watches a batch row, so recoverable failures must fail
            # the row (retryable via the batch retry button) instead of
            # pausing it — a paused row would hold its semaphore slot forever.
            unattended=True,
        )
        _update_row(job_id, row_idx, status="done",
                    finished_at=datetime.utcnow().isoformat())
    except Exception as exc:
        _update_row(job_id, row_idx, status="failed", error=str(exc),
                    finished_at=datetime.utcnow().isoformat())
