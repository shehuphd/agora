"""Debates router — create and retrieve debate runs."""
from __future__ import annotations
import asyncio
import json as _json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import Response
from traceact import ActionTrace

from api.models import DebateConfig
from api.routers.settings import _load_config as _load_agora_config
from core.config import DebateRunConfig
from core.export import build_markdown as _build_markdown, _n
from core.runpack import build_run_pack as _build_run_pack, build_pack_markdown as _build_pack_markdown
from core import runs_db as _runs_db

router = APIRouter()

# Per-run state — keyed by run_id.
_run_queues:         dict[str, asyncio.Queue] = {}
_pause_events:       dict[str, asyncio.Event] = {}   # set = running, cleared = paused
_force_close_events: dict[str, asyncio.Event] = {}   # set = user requested end
_overrides:          dict[str, dict]          = {}   # current effective overrides
_override_logs:      dict[str, list]          = {}   # ordered log of applied overrides

RUNS_DIR = Path(__file__).parent.parent.parent / "runs"
TRACES_DIR = Path(__file__).parent.parent.parent / "data" / "traces"


def get_queue(run_id: str) -> asyncio.Queue:
    if run_id not in _run_queues:
        raise HTTPException(status_code=404, detail="Run not found or debate not running")
    return _run_queues[run_id]


@router.post("/debates")
async def create_debate(config: DebateConfig, background_tasks: BackgroundTasks):
    """Start a new debate run from the submitted config."""
    import uuid
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=Path(".env").resolve(), override=True)

    # Resolve any unset model fields to the first available model from the DB.
    # This keeps zero hardcoded model names — the DB is always the source of truth.
    _agora_cfg = _load_agora_config()
    _provider_order = _agora_cfg.get("providers", {}).get("model_order")
    conn = _runs_db.connect()
    try:
        available = _runs_db.list_available_models(conn, provider_order=_provider_order)
    finally:
        conn.close()
    if not available:
        raise HTTPException(status_code=400, detail="No models available — test at least one API key in Settings first.")
    first_available = available[0]["model_id"]

    run_id = str(uuid.uuid4())
    run_cfg = DebateRunConfig.from_api(config, first_available=first_available)

    # Resolve every role's selection to a concrete (provider, model, endpoint)
    # here, once, and carry it in the run config. Agents are then handed a
    # routable address instead of re-deriving one per construction, so the run
    # records exactly which vendor served it and a later registry change cannot
    # retroactively point it somewhere else.
    run_cfg = _resolve_run_models(run_cfg)

    run_dir = RUNS_DIR / _make_run_dir_name(run_cfg.topic)

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    pause_event       = asyncio.Event()
    pause_event.set()  # not paused at start
    force_close_event = asyncio.Event()  # not set = still running
    overrides: dict = {}

    _run_queues[run_id]         = queue
    _pause_events[run_id]       = pause_event
    _force_close_events[run_id] = force_close_event
    _overrides[run_id]          = overrides
    _override_logs[run_id]      = []

    experiment_name = (getattr(config, "experiment_name", None) or "").strip() or None
    background_tasks.add_task(_run_debate_wrapper, run_id, run_cfg, run_dir, queue, pause_event, overrides, force_close_event, experiment_name=experiment_name)
    return {"run_id": run_id, "run_dir": run_dir.name}


def _cleanup_run(run_id: str) -> None:
    """Drop a finished run's in-memory registrations.

    Without this, every run created in a server session stays registered forever:
    batch_delete refuses to delete it ("running"), debate_alive reports it alive,
    and the queues/events/pool leak.
    """
    for d in (_run_queues, _pause_events, _force_close_events, _overrides, _override_logs):
        d.pop(run_id, None)
    from core.sources import discard_pool
    discard_pool(run_id)


async def _run_debate_wrapper(
    run_id: str, config: DebateRunConfig, run_dir: Path,
    queue: asyncio.Queue, pause_event: asyncio.Event, overrides: dict,
    force_close_event: asyncio.Event,
    experiment_name: str | None = None,
    **kwargs,
):
    from runners.debate import run_debate
    try:
        await run_debate(run_id, config, run_dir, queue, pause_event, overrides, force_close_event, experiment_name=experiment_name, **kwargs)
    except Exception as e:
        await queue.put({"type": "error", "message": str(e)})
        await queue.put(None)
    finally:
        # An SSE client already streaming holds its own reference to the queue,
        # so dropping the registration does not interrupt it.
        _cleanup_run(run_id)


@router.get("/debates/{run_id}/alive")
async def debate_alive(run_id: str):
    """Return whether this run still has an active runner in memory.
    Used by the frontend to detect server restarts vs transient network drops."""
    return {"alive": run_id in _run_queues}


@router.post("/debates/{run_id}/pause")
async def pause_debate(run_id: str):
    ev = _pause_events.get(run_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Run not found or already closed")
    with ActionTrace.start(action="debate.pause", kind="app", actor="user",
                           project="agora", correlation_id=run_id) as t:
        t.input({"run_id": run_id})
        ev.clear()
        t.output({"status": "paused"})
    return {"status": "paused"}


@router.post("/debates/{run_id}/resume")
async def resume_debate(run_id: str):
    ev = _pause_events.get(run_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Run not found or already closed")
    with ActionTrace.start(action="debate.resume", kind="app", actor="user",
                           project="agora", correlation_id=run_id) as t:
        t.input({"run_id": run_id})
        ev.set()
        t.output({"status": "resumed"})
    return {"status": "resumed"}


@router.post("/debates/{run_id}/end")
async def end_debate(run_id: str):
    """Signal the runner to close after the current turn completes."""
    ev = _force_close_events.get(run_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Run not found or already closed")
    with ActionTrace.start(action="debate.end", kind="app", actor="user",
                           project="agora", correlation_id=run_id) as t:
        t.input({"run_id": run_id})
        pause_ev = _pause_events.get(run_id)
        if pause_ev and not pause_ev.is_set():
            pause_ev.set()
        ev.set()
        t.output({"status": "ending"})
    return {"status": "ending"}


@router.post("/debates/{run_id}/override")
async def apply_override(run_id: str, body: dict):
    """Apply a mid-run override. Supported fields: token_budget (int), token_budget_delta (int)."""
    ov  = _overrides.get(run_id)
    log = _override_logs.get(run_id)
    q   = _run_queues.get(run_id)

    if ov is None:
        raise HTTPException(status_code=404, detail="Run not found")

    applied: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    with ActionTrace.start(action="debate.override", kind="app", actor="user",
                           project="agora", correlation_id=run_id) as t:
        t.input({"run_id": run_id, **body})

        if "token_budget" in body or "token_budget_delta" in body:
            old_val = ov.get("token_budget", None)  # None means using original config value
            if "token_budget" in body:
                new_val = int(body["token_budget"])
            else:
                # Delta: look up base from DB config if not already overridden
                if old_val is None:
                    db = _find_db(run_id)
                    if db:
                        conn = sqlite3.connect(str(db))
                        row = conn.execute("SELECT config FROM runs WHERE run_id=?", (run_id,)).fetchone()
                        conn.close()
                        base = _json.loads(row[0]).get("token_budget", 40_000) if row and row[0] else 40_000
                    else:
                        base = 40_000
                else:
                    base = old_val
                new_val = max(1000, base + int(body["token_budget_delta"]))

            ov["token_budget"] = new_val
            entry = {"timestamp": now, "field": "token_budget", "old_value": old_val, "new_value": new_val}
            if log is not None:
                log.append(entry)
            applied.append(entry)

            if q:
                await q.put({
                    "type": "override",
                    "field": "token_budget",
                    "old_value": old_val,
                    "new_value": new_val,
                    "timestamp": now,
                })

        # Persist override log so debate.json stays current
        if log is not None:
            db_path = _find_db(run_id)
            if db_path:
                overrides_path = db_path.parent / "overrides.json"
                try:
                    overrides_path.write_text(_json.dumps(log, indent=2), encoding="utf-8")
                except Exception:
                    pass

        t.output({"applied": applied, "overrides": dict(ov)})

    return {"status": "ok", "applied": applied, "overrides": dict(ov)}


@router.get("/debates")
async def list_debates(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    def _query():
        conn = _runs_db.connect()
        result = _runs_db.list_runs(conn, limit=limit, offset=offset)
        conn.close()
        return result

    return await asyncio.to_thread(_query)


@router.get("/debates/{run_id}")
async def get_debate(run_id: str):
    db_path = _find_db(run_id)
    if not db_path:
        raise HTTPException(status_code=404, detail="Debate not found")

    conn = sqlite3.connect(str(db_path))
    for col_sql in (
        "ALTER TABLE runs ADD COLUMN config TEXT",
        "ALTER TABLE runs ADD COLUMN continued_from TEXT",
    ):
        try:
            conn.execute(col_sql)
            conn.commit()
        except Exception:
            pass

    run = conn.execute(
        "SELECT run_id, created_at, status, debate_title, topic, closure_reason, config, continued_from "
        "FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run record not found")

    acts = conn.execute(
        "SELECT * FROM acts WHERE run_id=? ORDER BY turn, timestamp", (run_id,)
    ).fetchall()
    claims = conn.execute(
        "SELECT * FROM claims WHERE run_id=?", (run_id,)
    ).fetchall()
    meta_row = conn.execute(
        "SELECT value FROM meta WHERE key='token_offset'"
    ).fetchone()
    conn.close()

    token_offset = None
    if meta_row:
        try:
            token_offset = _json.loads(meta_row[0])
        except Exception:
            pass

    act_cols = [
        "act_id", "run_id", "turn", "agent", "agent_role", "act_type",
        "claim_id", "target_act_id", "content", "reason",
        "input_tokens", "output_tokens", "model_used", "timestamp",
    ]
    claim_cols = ["claim_id", "run_id", "author", "content", "status", "last_updated"]

    raw_cfg = run[6]
    parsed_cfg = _json.loads(raw_cfg) if raw_cfg else {}

    # A run is continuable only if it didn't already issue a CLOSE act.
    # A CLOSE act means the debate concluded (even if the DB status wasn't updated yet).
    status, closure_reason = run[2], run[5]
    has_close_act = any(row[5] == "CLOSE" for row in acts)
    is_continuable = _is_resumable(status, closure_reason) and not has_close_act

    # Pull experiment assignment from the runs index (best-effort).
    experiment_id = experiment_name = None
    try:
        idx = _runs_db.connect()
        idx_row = idx.execute(
            """SELECT r.experiment_id, e.name
               FROM runs r LEFT JOIN experiments e ON e.experiment_id = r.experiment_id
               WHERE r.run_id=?""",
            (run_id,),
        ).fetchone()
        idx.close()
        if idx_row:
            experiment_id   = idx_row[0]
            experiment_name = idx_row[1]
    except Exception:
        pass

    return {
        "run_id":          run[0],
        "created_at":      run[1],
        "status":          status,
        "debate_title":    run[3],
        "topic":           run[4],
        "closure_reason":  closure_reason,
        "config":          parsed_cfg,
        "continued_from":  run[7],
        "token_offset":    token_offset,
        "is_continuable":  is_continuable,
        "experiment_id":   experiment_id,
        "experiment_name": experiment_name,
        "acts":    [dict(zip(act_cols, row)) for row in acts],
        "claims":  [dict(zip(claim_cols, row)) for row in claims],
        "override_log": _override_logs.get(run_id, []),
    }


@router.get("/debates/{run_id}/export")
async def export_debate(run_id: str, format: str = "json", raw_serp: bool = False):
    """Export one run.

    format: json | markdown       — the transcript
            pack | pack-markdown  — the full audit record: every call, query,
                                    source, excerpt, and citation check
    raw_serp: pack formats only — embed the unabridged SERP responses.
    """
    with ActionTrace.start(action="run.export", kind="app", actor="user",
                           project="agora", correlation_id=run_id) as t:
        t.input({"run_id": run_id, "format": format, "batch": False})
    db_path = _find_db(run_id)
    if not db_path:
        raise HTTPException(status_code=404, detail="Debate not found")

    run_dir = db_path.parent

    if format in ("pack", "pack-markdown"):
        data = await get_debate(run_id)
        slug = _slugify(data.get("debate_title") or data.get("topic") or run_id)
        pack = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _build_run_pack(data, run_dir, TRACES_DIR, include_raw_serp=raw_serp),
        )
        if format == "pack-markdown":
            return Response(
                content=_build_pack_markdown(pack),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition":
                         f'attachment; filename="agora-pack-{slug}-{run_id[:8]}.md"'},
            )
        return Response(
            content=_json.dumps(pack, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="agora-pack-{slug}-{run_id[:8]}.json"'},
        )

    is_md = format == "markdown"

    # debate.json is data, so the copy on disk is served as-is. Markdown is
    # *rendered* data, and the pre-generated debate.md was written by whichever
    # renderer existed when the run closed — serving it would freeze every
    # historical run at that version. Rendering here instead means a fix to
    # build_markdown reaches runs that closed before the fix existed.
    pre_json = run_dir / "debate.json"
    if pre_json.exists():
        data = _load_json_file(pre_json)
        if data is None:
            data = await get_debate(run_id)
    else:
        data = await get_debate(run_id)

    slug = _slugify(data.get("debate_title") or data.get("topic") or run_id)
    if is_md:
        return Response(
            content=_build_markdown(data),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="agora-{slug}-{run_id[:8]}.md"'},
        )
    return Response(
        content=_json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="agora-{slug}-{run_id[:8]}.json"'},
    )


_NON_RESUMABLE_KEYWORDS = [
    "max_turns",
    "max_time_minutes",
    "token_budget",
    "user_requested_end",
    "quota_exhausted",
    "propose met with concede",
    "repetition detected",
    "challenge_rate_floor",
]


def _is_resumable(status: str, closure_reason: str | None) -> bool:
    if status in ("running", "error"):
        return True
    if not closure_reason:
        return True
    cr = closure_reason.lower()
    return not any(kw in cr for kw in _NON_RESUMABLE_KEYWORDS)


@router.post("/debates/{run_id}/continue")
async def continue_debate(run_id: str, background_tasks: BackgroundTasks):
    """Continue an interrupted debate from where it left off, under the same experimental settings."""
    import uuid
    from dotenv import load_dotenv as _load_dotenv
    from core.config import DebateRunConfig
    from core.checkpoint import load_state, debate_turn_idx

    _load_dotenv(dotenv_path=Path(".env").resolve(), override=True)

    db_path = _find_db(run_id)
    if not db_path:
        raise HTTPException(status_code=404, detail="Debate not found")

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status, closure_reason, config FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")

    status, closure_reason, config_json = row

    if not _is_resumable(status, closure_reason):
        conn.close()
        raise HTTPException(
            status_code=409,
            detail=(
                f"This debate ended naturally ({closure_reason}) and cannot be continued. "
                "Start a new debate with the same settings instead."
            ),
        )

    if not config_json:
        conn.close()
        raise HTTPException(status_code=422, detail="Original run has no stored config; cannot continue.")

    original_state = load_state(conn, run_id)
    turn_idx       = debate_turn_idx(original_state.acts)
    conn.close()

    # Guard: if a CLOSE act was already written, the debate concluded — not continuable.
    if any(a.act_type == "CLOSE" for a in original_state.acts):
        raise HTTPException(
            status_code=409,
            detail="This debate already issued a closing act and cannot be continued.",
        )

    original_cfg = DebateRunConfig.from_dict(_json.loads(config_json))

    # Always derive steelman_mode from config — the DB column may be 0 on older runs.
    original_state.steelman_mode = original_cfg.steelman_mode

    new_run_id  = str(uuid.uuid4())
    new_run_dir = RUNS_DIR / _make_run_dir_name(original_cfg.topic)

    queue           = asyncio.Queue(maxsize=200)
    pause_event     = asyncio.Event()
    pause_event.set()
    force_close     = asyncio.Event()
    overrides: dict = {}

    _run_queues[new_run_id]         = queue
    _pause_events[new_run_id]       = pause_event
    _force_close_events[new_run_id] = force_close
    _overrides[new_run_id]          = overrides
    _override_logs[new_run_id]      = []

    with ActionTrace.start(action="debate.continue", kind="app", actor="user",
                           project="agora", correlation_id=new_run_id,
                           meta={"continued_from": run_id}) as t:
        t.input({"original_run_id": run_id, "new_run_id": new_run_id,
                 "turn_start": turn_idx, "topic": original_cfg.topic})
        background_tasks.add_task(
            _run_debate_wrapper,
            new_run_id, original_cfg, new_run_dir,
            queue, pause_event, overrides, force_close,
            initial_state=original_state,
            turn_idx_start=turn_idx,
            continued_from=run_id,
        )
        t.output({"new_run_id": new_run_id, "continued_from": run_id})

    return {
        "run_id":         new_run_id,
        "run_dir":        new_run_dir.name,
        "continued_from": run_id,
    }


@router.post("/debates/delete")
async def batch_delete(body: dict):
    """Delete multiple debate run directories. Body: {"ids": [...]}
    Refuses to delete any run that is currently running."""
    import shutil
    from core import runs_db as _runs_db
    ids = body.get("ids") or []
    deleted, skipped = [], []
    with ActionTrace.start(action="run.delete", kind="app", actor="user",
                           project="agora") as t:
        t.input({"ids": ids, "count": len(ids)})
        for rid in ids:
            if rid in _run_queues:
                skipped.append({"id": rid, "reason": "running"})
                continue
            db_path = _find_db(rid)
            if not db_path:
                skipped.append({"id": rid, "reason": "not_found"})
                continue
            try:
                shutil.rmtree(db_path.parent)
                deleted.append(rid)
            except Exception as exc:
                skipped.append({"id": rid, "reason": str(exc)})
        if deleted:
            try:
                idx = _runs_db.connect()
                _runs_db.delete_runs(idx, deleted)
                idx.close()
            except Exception:
                pass
        t.output({"deleted_count": len(deleted), "skipped_count": len(skipped),
                  "deleted": deleted, "skipped": skipped})
    return {"status": "ok", "deleted": deleted, "skipped": skipped}


@router.post("/debates/export")
async def batch_export(body: dict):
    """Download multiple debates as JSON or Markdown. Body: {"ids": [...], "format": "json"|"markdown"}"""
    ids    = body.get("ids") or []
    with ActionTrace.start(action="run.export", kind="app", actor="user",
                           project="agora") as t:
        t.input({"ids": ids, "count": len(ids), "format": body.get("format", "json"), "batch": True})
    fmt    = body.get("format", "json")
    results = []
    for rid in ids:
        try:
            results.append(await get_debate(rid))
        except HTTPException:
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "markdown":
        parts = [_build_markdown(d) for d in results]
        content = "\n\n---\n\n".join(parts)
        filename = f"agora-export-{stamp}.md"
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    filename = f"agora-export-{stamp}.json"
    return Response(
        content=_json.dumps(results, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_AGENT_ROLES = ("proposition", "opposition", "moderator", "synthesiser")


def _resolve_run_models(run_cfg: "DebateRunConfig") -> "DebateRunConfig":
    """Pin every role to one registry entry, or fail with a 400 naming the role.

    Runs before the run exists, so an unroutable selection is rejected up front
    instead of surfacing three turns in as an error from whichever vendor the
    call was misdirected to.
    """
    import dataclasses

    conn = _runs_db.connect()
    try:
        resolved, problems = {}, []
        for role in _AGENT_ROLES:
            agent = getattr(run_cfg, role)
            try:
                entry = _runs_db.resolve_model(conn, agent.model, agent.provider)
            except _runs_db.ModelNotRoutable as exc:
                problems.append(f"{role} — {exc}")
                continue
            resolved[role] = dataclasses.replace(
                agent, provider=entry["provider"],
                endpoint_type=entry["endpoint_type"],
            )
    finally:
        conn.close()

    if problems:
        raise HTTPException(status_code=400, detail=" | ".join(problems))
    return dataclasses.replace(run_cfg, **resolved)


def _slugify(text: str, limit: int = 40) -> str:
    """Filename-safe slug for export attachments."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "")[:limit].lower()).strip("-")


def _load_json_file(path: Path):
    """Read a JSON file, or None if it is missing or malformed."""
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _make_run_dir_name(topic: str) -> str:
    import string, random
    date_str = datetime.utcnow().strftime("%Y%m%d")
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{date_str}_{rand_str}_{_slugify(topic, 30)}"


def _find_db(run_id: str) -> Path | None:
    if not RUNS_DIR.exists():
        return None
    for run_dir in RUNS_DIR.iterdir():
        db_path = run_dir / "debate.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute(
                    "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                conn.close()
                if row:
                    return db_path
            except Exception:
                continue
    return None
