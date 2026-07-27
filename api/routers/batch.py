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

# Canonical CSV columns and their DebateConfig equivalents.
# All optional except topic.
_COLUMNS = [
    "topic",
    "debate_title",
    "proposition_model",
    "opposition_model",
    "moderator_model",
    "synth_model",
    "proposition_nickname",
    "opposition_nickname",
    "max_turns",
    "max_time_minutes",
    "token_budget",
    "temperature_proposition",
    "temperature_opposition",
    "temperature_moderator",
    "aggression",
    "min_challenges",
    "min_concessions",
    "require_steelman",
    "require_full_resolution",
]

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

    with ActionTrace.start(action="batch.import", kind="app", actor="user",
                           project="agora", correlation_id=eid) as t:
        t.input({"experiment_id": eid, "row_count": len(rows), "skipped": len(errors)})
        job = _batch.create_job(experiment_id=eid, rows_data=rows)
        await _batch.enqueue(job)
        t.output({"job_id": job.job_id, "queued": len(rows)})

    return JSONResponse({
        "job_id":        job.job_id,
        "experiment_id": eid,
        "queued":   len(rows),
        "skipped":  len(errors),
        "warnings": errors,
        "rows": [{"row_idx": r.row_idx, "topic": r.topic, "status": r.status}
                 for r in job.rows],
    })


@router.get("/api/batch/{job_id}")
async def get_batch_status(job_id: str):
    """Poll the status of a batch job."""
    job = _batch.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Batch job not found")
    return JSONResponse(job.to_dict())
