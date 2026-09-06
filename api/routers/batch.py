"""Batch import router — CSV → concurrent debate runs assigned to an experiment."""
from __future__ import annotations

import asyncio
import csv
import io

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from traceact import ActionTrace

from core import batch as _batch

router = APIRouter()

# Canonical CSV columns and their DebateConfig equivalents — the same field
# list experiment specs accept, kept in core/spec.py so the two input paths
# can't drift apart. All optional except topic.
from core.spec import SPEC_FIELDS as _COLUMNS

_INT_COLS   = {"max_turns", "max_time_minutes", "token_budget", "min_challenges", "min_concessions"}
_FLOAT_COLS = {"temperature_proposition", "temperature_opposition", "temperature_moderator", "aggression"}
_BOOL_COLS  = {"require_steelman", "require_full_resolution"}


import threading

# Serialises find-or-create so two simultaneous imports with the same new name
# cannot both miss the lookup and create duplicate experiments.
_experiment_create_lock = threading.Lock()


def _find_or_create_experiment(name: str) -> str:
    """Return the id of the experiment called `name`, creating it if absent."""
    import uuid
    from datetime import datetime
    from core import runs_db as _runs_db

    with _experiment_create_lock:
        conn = _runs_db.connect()
        try:
            _runs_db.init(conn)
            existing = _runs_db.find_experiment_by_name(conn, name)
            if existing:
                return existing["experiment_id"]
            eid = str(uuid.uuid4())
            _runs_db.create_experiment(
                conn, experiment_id=eid, name=name,
                description=None, created_at=datetime.utcnow().isoformat(),
            )
            return eid
        finally:
            conn.close()


@router.get("/api/batch/template")
async def download_template():
    """Return a CSV template the user can fill in and re-upload."""
    body = ",".join(_COLUMNS) + "\n"
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="agora-batch-template.csv"'})


@router.post("/api/batch")
async def create_batch(
    file: UploadFile = File(...),
    experiment_id: str = Form(default=""),
    experiment_name: str = Form(default=""),
    selected_rows: str = Form(default=""),
    budget_usd: str = Form(default=""),
):
    """Parse a CSV upload and enqueue a batch of debate runs."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")  # strip BOM if present (common in Excel exports)
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    fieldnames_lower = [f.strip().lower() for f in reader.fieldnames]
    if "topic" not in fieldnames_lower:
        raise HTTPException(status_code=400, detail="CSV must have a 'topic' column")

    rows: list[dict] = []
    errors: list[str] = []
    for i, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        topic = row.get("topic", "")
        if not topic:
            errors.append(f"Row {i}: topic is blank — skipped")
            continue

        config: dict = {"topic": topic}
        for col in _COLUMNS[1:]:
            val = row.get(col, "")
            if not val:
                continue
            try:
                if col in _INT_COLS:
                    config[col] = int(val)
                elif col in _FLOAT_COLS:
                    config[col] = float(val)
                elif col in _BOOL_COLS:
                    config[col] = val.lower() in ("1", "true", "yes")
                else:
                    config[col] = val
            except ValueError:
                errors.append(f"Row {i}: invalid value for '{col}' ({val!r}) — using default")
        rows.append(config)

    if not rows:
        raise HTTPException(status_code=400, detail="No valid rows found in CSV")

    # Restrict to the rows the user ticked, if a selection was sent.
    if selected_rows.strip():
        try:
            keep = {int(i) for i in selected_rows.split(",") if i.strip()}
        except ValueError:
            raise HTTPException(status_code=400, detail="selected_rows must be comma-separated integers")
        rows = [r for i, r in enumerate(rows) if i in keep]
        if not rows:
            raise HTTPException(status_code=400, detail="No rows selected")

    # An experiment can be named instead of pre-selected: find it or create it,
    # so importing a CSV is a single step rather than two.
    eid = experiment_id.strip() or None
    name = experiment_name.strip()
    if not eid and name:
        eid = await asyncio.get_running_loop().run_in_executor(
            None, _find_or_create_experiment, name,
        )

    budget: float | None = None
    if budget_usd.strip():
        try:
            budget = float(budget_usd)
            if budget <= 0:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="budget_usd must be a positive number")

    with ActionTrace.start(action="batch.import", kind="app", actor="user",
                           project="agora", correlation_id=eid) as t:
        t.input({"experiment_id": eid, "row_count": len(rows),
                 "skipped": len(errors), "budget_usd": budget})
        # Save the parsed rows as an explicit-rows spec so the CSV batch is
        # re-runnable from the experiment screen. Only when the experiment has
        # no spec yet: a stored factor grid must not be clobbered by a CSV.
        if eid:
            def _store_spec():
                import json as _json
                from core import runs_db as _rdb
                conn = _rdb.connect()
                try:
                    _rdb.init(conn)
                    exp = _rdb.get_experiment(conn, eid)
                    if exp is not None and not exp.get("spec"):
                        _rdb.set_experiment_spec(
                            conn, eid,
                            _json.dumps({"rows": rows, "replicates": 1}),
                        )
                finally:
                    conn.close()
            await asyncio.get_running_loop().run_in_executor(None, _store_spec)

        job_id = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _batch.create_job(eid, [dict(r) for r in rows], budget_usd=budget),
        )
        await _batch.enqueue(job_id)
        t.output({"job_id": job_id, "queued": len(rows)})

    return JSONResponse({
        "job_id":        job_id,
        "experiment_id": eid,
        "queued":   len(rows),
        "skipped":  len(errors),
        "warnings": errors,
    })


@router.get("/api/batch/{job_id}")
async def get_batch_status(job_id: str):
    """Poll the status of a batch job."""
    job = await asyncio.get_running_loop().run_in_executor(
        None, _batch.get_job, job_id,
    )
    if not job:
        raise HTTPException(status_code=404, detail="Batch job not found")
    return JSONResponse(job)


@router.post("/api/batch/{job_id}/retry")
async def retry_batch(job_id: str):
    """Re-run a job's failed, interrupted, and budget-skipped rows as a new job."""
    with ActionTrace.start(action="batch.retry", kind="app", actor="user",
                           project="agora", correlation_id=job_id) as t:
        t.input({"source_job_id": job_id})
        new_id = await asyncio.get_running_loop().run_in_executor(
            None, _batch.create_retry_job, job_id,
        )
        if new_id is None:
            t.output({"error": "nothing_to_retry"})
            raise HTTPException(
                status_code=400,
                detail="No rows to retry — the job doesn't exist or every row finished",
            )
        await _batch.enqueue(new_id)
        t.output({"job_id": new_id})
    return JSONResponse({"job_id": new_id})
