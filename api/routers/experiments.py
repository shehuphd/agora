"""Experiments router — CRUD for experiment groups and run assignment."""
import asyncio
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from core import runs_db as _runs_db
from traceact import ActionTrace

RUNS_DIR = Path(__file__).parent.parent.parent / "runs"
router = APIRouter()


def _idx():
    conn = _runs_db.connect()
    _runs_db.init(conn)
    return conn


@router.get("/experiments")
async def list_experiments():
    def _run():
        conn = _idx()
        result = _runs_db.list_experiments(conn)
        conn.close()
        return result
    return await asyncio.get_running_loop().run_in_executor(None, _run)


@router.post("/experiments")
async def create_experiment(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    description = (payload.get("description") or "").strip() or None
    eid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    with ActionTrace.start(action="experiment.create", kind="app", actor="user",
                           project="agora", correlation_id=eid) as t:
        t.input({"name": name, "description": description})
        def _run():
            conn = _idx()
            _runs_db.create_experiment(conn, experiment_id=eid, name=name, description=description, created_at=now)
            conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _run)
        t.output({"experiment_id": eid, "name": name})
    return {"experiment_id": eid, "name": name, "description": description, "created_at": now, "run_count": 0}


# Specific path must be defined BEFORE {experiment_id} wildcard
@router.get("/experiments/unassigned-runs")
async def list_unassigned_runs():
    def _run():
        conn = _idx()
        result = _runs_db.list_unassigned_runs(conn)
        conn.close()
        return result
    return await asyncio.get_running_loop().run_in_executor(None, _run)


@router.get("/experiments/{experiment_id}")
async def get_experiment(experiment_id: str):
    def _run():
        conn = _idx()
        result = _runs_db.get_experiment(conn, experiment_id)
        conn.close()
        return result
    exp = await asyncio.get_running_loop().run_in_executor(None, _run)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return exp


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(experiment_id: str):
    def _run():
        conn = _idx()
        exp = _runs_db.get_experiment(conn, experiment_id)
        if not exp:
            conn.close()
            return None
        _runs_db.delete_experiment(conn, experiment_id)
        conn.close()
        return exp
    with ActionTrace.start(action="experiment.delete", kind="app", actor="user",
                           project="agora", correlation_id=experiment_id) as t:
        t.input({"experiment_id": experiment_id})
        exp = await asyncio.get_running_loop().run_in_executor(None, _run)
        if exp is None:
            t.output({"error": "not_found"})
            raise HTTPException(status_code=404, detail="Experiment not found")
        t.output({"experiment_id": experiment_id, "name": exp.get("name")})
    return {"status": "ok"}


@router.get("/experiments/{experiment_id}/runs")
async def list_experiment_runs(experiment_id: str):
    def _run():
        conn = _idx()
        result = _runs_db.list_experiment_runs(conn, experiment_id, RUNS_DIR)
        conn.close()
        return result
    return await asyncio.get_running_loop().run_in_executor(None, _run)


@router.post("/experiments/{experiment_id}/runs")
async def assign_run_to_experiment(experiment_id: str, payload: dict):
    run_id = (payload.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id is required")

    with ActionTrace.start(action="experiment.assign_run", kind="app", actor="user",
                           project="agora", correlation_id=experiment_id) as t:
        t.input({"experiment_id": experiment_id, "run_id": run_id})
        def _run():
            conn = _idx()
            exp = _runs_db.get_experiment(conn, experiment_id)
            if not exp:
                conn.close()
                return False
            _runs_db.assign_run(conn, run_id, experiment_id)
            conn.close()
            return True
        found = await asyncio.get_running_loop().run_in_executor(None, _run)
        if not found:
            t.output({"error": "experiment_not_found"})
            raise HTTPException(status_code=404, detail="Experiment not found")
        t.output({"status": "assigned", "run_id": run_id, "experiment_id": experiment_id})
    return {"status": "ok", "run_id": run_id, "experiment_id": experiment_id}


@router.delete("/experiments/{experiment_id}/runs/{run_id}")
async def unassign_run_from_experiment(experiment_id: str, run_id: str):
    with ActionTrace.start(action="experiment.unassign_run", kind="app", actor="user",
                           project="agora", correlation_id=experiment_id) as t:
        t.input({"experiment_id": experiment_id, "run_id": run_id})
        def _run():
            conn = _idx()
            _runs_db.unassign_run(conn, run_id)
            conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _run)
        t.output({"status": "unassigned", "run_id": run_id})
    return {"status": "ok"}
