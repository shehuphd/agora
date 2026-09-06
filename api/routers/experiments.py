"""Experiments router — CRUD for experiment groups, run assignment, specs,
comparison, and dataset export."""
import asyncio
import csv
import io
import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from core import runs_db as _runs_db
from core import spec as _spec
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
    # spec and manifest are stored as JSON text; hand the client objects.
    for key in ("spec", "manifest"):
        if exp.get(key):
            try:
                exp[key] = json.loads(exp[key])
            except (TypeError, ValueError):
                exp[key] = None
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
        try:
            result = _runs_db.list_experiment_runs(conn, experiment_id, RUNS_DIR)
            metrics = _runs_db.get_run_metrics_map(conn, [r["run_id"] for r in result])
        finally:
            conn.close()
        for r in result:
            r["metrics"] = metrics.get(r["run_id"])
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


def _get_exp_or_404(experiment_id: str) -> dict:
    conn = _idx()
    try:
        exp = _runs_db.get_experiment(conn, experiment_id)
    finally:
        conn.close()
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return exp


@router.put("/experiments/{experiment_id}/spec")
async def save_spec(experiment_id: str, payload: dict):
    """Store (or clear, with {"spec": null}) the experiment's design."""
    spec = payload.get("spec")
    if spec is not None:
        try:
            _spec.validate(spec)
        except _spec.SpecError as e:
            raise HTTPException(status_code=400, detail=str(e))

    with ActionTrace.start(action="experiment.save_spec", kind="app", actor="user",
                           project="agora", correlation_id=experiment_id) as t:
        t.input({"experiment_id": experiment_id, "cleared": spec is None})
        def _run():
            _get_exp_or_404(experiment_id)
            conn = _idx()
            try:
                _runs_db.set_experiment_spec(
                    conn, experiment_id, json.dumps(spec) if spec is not None else None,
                )
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _run)
        t.output({"status": "saved"})
    return {"status": "ok"}


@router.post("/experiments/{experiment_id}/spec/preview")
async def preview_spec(experiment_id: str, payload: dict):
    """Deterministic expansion of a spec (from the payload, or the stored
    one) into the rows a launch would enqueue — nothing is enqueued. The
    frontend prices each row through the existing estimate endpoint."""
    def _run():
        exp = _get_exp_or_404(experiment_id)
        spec = payload.get("spec") or (json.loads(exp["spec"]) if exp.get("spec") else None)
        if spec is None:
            raise HTTPException(status_code=400, detail="No spec provided or stored")
        try:
            rows = _spec.expand(spec)
        except _spec.SpecError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return [
            {"config": {k: v for k, v in r.items() if k != "_condition"},
             "condition": r["_condition"]}
            for r in rows
        ]
    rows = await asyncio.get_running_loop().run_in_executor(None, _run)
    return {"rows": rows, "count": len(rows)}


def _build_manifest() -> dict:
    """Environment record for a spec launch: package versions, price
    snapshot, git commit. Every field is best-effort — an unknown value is
    recorded as null rather than blocking the launch."""
    manifest: dict = {"created_at": datetime.utcnow().isoformat()}
    from importlib.metadata import version, PackageNotFoundError
    for pkg in ("keycall", "traceact", "rates"):
        try:
            manifest[pkg] = version(pkg)
        except PackageNotFoundError:
            manifest[pkg] = None
    try:
        from core import cost as _cost
        manifest["prices_as_of"] = _cost.snapshot_date()
    except Exception:
        manifest["prices_as_of"] = None
    try:
        import subprocess
        manifest["git_commit"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).parent.parent.parent),
        ).stdout.strip() or None
    except Exception:
        manifest["git_commit"] = None
    return manifest


def _max_replicate(conn, experiment_id: str) -> int:
    """Highest replicate number stamped on any of the experiment's runs, so
    an extension continues numbering instead of reusing it."""
    rows = conn.execute(
        "SELECT condition FROM runs WHERE experiment_id=? AND condition IS NOT NULL",
        (experiment_id,),
    ).fetchall()
    highest = 0
    for r in rows:
        try:
            rep = int(json.loads(r["condition"]).get("replicate", 0))
            highest = max(highest, rep)
        except Exception:
            continue
    return highest


@router.post("/experiments/{experiment_id}/launch")
async def launch_spec(experiment_id: str, payload: dict):
    """Expand the stored spec and enqueue the runs as one batch job.

    payload.mode: "run" (default) launches the spec's full replicate count
    starting at replicate 1 higher than anything already stamped;
    "extend" launches payload.replicates additional replicates per condition.
    payload.budget_usd optionally caps the job's spend.
    """
    from core import batch as _batch

    mode = payload.get("mode", "run")
    budget = payload.get("budget_usd")
    if budget is not None:
        try:
            budget = float(budget)
            if budget <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="budget_usd must be a positive number")

    def _prepare():
        exp = _get_exp_or_404(experiment_id)
        if not exp.get("spec"):
            raise HTTPException(status_code=400, detail="This experiment has no stored spec")
        spec = json.loads(exp["spec"])
        conn = _idx()
        try:
            start = _max_replicate(conn, experiment_id) + 1
            if mode == "extend":
                extra = payload.get("replicates")
                if not isinstance(extra, int) or extra < 1:
                    raise HTTPException(status_code=400,
                                        detail="extend needs replicates >= 1")
                rows = _spec.expand(spec, replicates=extra, replicate_start=start)
            else:
                rows = _spec.expand(spec, replicate_start=start)
            if exp.get("manifest") is None:
                _runs_db.set_experiment_manifest(
                    conn, experiment_id, json.dumps(_build_manifest()),
                )
        finally:
            conn.close()
        return rows

    with ActionTrace.start(action="experiment.launch", kind="app", actor="user",
                           project="agora", correlation_id=experiment_id) as t:
        t.input({"experiment_id": experiment_id, "mode": mode, "budget_usd": budget})
        try:
            rows = await asyncio.get_running_loop().run_in_executor(None, _prepare)
        except _spec.SpecError as e:
            t.output({"error": str(e)})
            raise HTTPException(status_code=400, detail=str(e))
        job_id = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _batch.create_job(experiment_id, rows, budget_usd=budget),
        )
        await _batch.enqueue(job_id)
        t.output({"job_id": job_id, "queued": len(rows)})
    return {"job_id": job_id, "queued": len(rows)}


def _comparison_groups(runs: list[dict], metrics: dict[str, dict]) -> list[dict]:
    """Group runs by condition (replicate excluded) and aggregate.

    Means and ranges only: at the replicate counts specs run at, a
    significance test would overstate what the data supports."""
    groups: dict[str, dict] = {}
    for r in runs:
        key = _spec.condition_key(r.get("condition"))
        g = groups.setdefault(key, {
            "condition": {k: v for k, v in (r.get("condition") or {}).items()
                          if k != "replicate"},
            "runs": [],
        })
        g["runs"].append(r)

    def _stats(values: list) -> dict | None:
        vals = [v for v in values if v is not None]
        if not vals:
            return None
        return {"mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals),
                "n": len(vals)}

    out = []
    for key, g in sorted(groups.items()):
        rs = g["runs"]
        ms = [metrics.get(r["run_id"], {}) for r in rs]
        out.append({
            "condition": g["condition"],
            "n": len(rs),
            "completed": sum(1 for r in rs if r["status"] == "closed"),
            "tokens": _stats([r.get("total_tokens") for r in rs]),
            "cost_usd": _stats([r.get("total_cost_usd") for r in rs]),
            "turns": _stats([r.get("turn") for r in rs]),
            "citation_coverage": _stats([m.get("citation_coverage") for m in ms]),
            "retries": _stats([m.get("retries") for m in ms]),
            "citation_repairs": _stats([m.get("citation_repairs") for m in ms]),
            "run_ids": [r["run_id"] for r in rs],
        })
    return out


@router.get("/experiments/{experiment_id}/comparison")
async def comparison(experiment_id: str):
    def _run():
        _get_exp_or_404(experiment_id)
        conn = _idx()
        try:
            runs = _runs_db.list_experiment_runs(conn, experiment_id, RUNS_DIR)
            metrics = _runs_db.get_run_metrics_map(conn, [r["run_id"] for r in runs])
        finally:
            conn.close()
        return _comparison_groups(runs, metrics)
    groups = await asyncio.get_running_loop().run_in_executor(None, _run)
    return {"groups": groups}


@router.get("/experiments/{experiment_id}/dataset.csv")
async def dataset_csv(experiment_id: str):
    """Tidy dataset: one run per row, condition factors and metrics as
    columns, manifest values repeated as trailing constant columns."""
    def _run():
        exp = _get_exp_or_404(experiment_id)
        conn = _idx()
        try:
            runs = _runs_db.list_experiment_runs(conn, experiment_id, RUNS_DIR)
            metrics = _runs_db.get_run_metrics_map(conn, [r["run_id"] for r in runs])
        finally:
            conn.close()
        manifest = json.loads(exp["manifest"]) if exp.get("manifest") else {}

        factor_keys = sorted({
            k for r in runs for k in (r.get("condition") or {}) if k != "replicate"
        })
        metric_keys = list(_runs_db._METRIC_FIELDS)
        manifest_keys = [k for k in ("prices_as_of", "git_commit", "keycall",
                                     "traceact", "rates") if manifest.get(k)]

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["run_id", "created_at", "status", *factor_keys, "replicate",
                    *metric_keys, *manifest_keys])
        for r in runs:
            cond = r.get("condition") or {}
            m = metrics.get(r["run_id"], {})
            w.writerow([
                r["run_id"], r["created_at"], r["status"],
                *[cond.get(k, "") for k in factor_keys],
                cond.get("replicate", ""),
                *[m.get(k, "") if m.get(k) is not None else "" for k in metric_keys],
                *[manifest[k] for k in manifest_keys],
            ])
        return exp["name"], buf.getvalue()

    name, body = await asyncio.get_running_loop().run_in_executor(None, _run)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:60] or "experiment"
    return Response(
        content=body, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe}-dataset.csv"'},
    )


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
