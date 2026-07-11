"""Debates router — create and retrieve debate runs."""
import asyncio
import json as _json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import Response

from api.models import DebateConfig
from core.config import DebateRunConfig
from core.export import build_markdown as _build_markdown, _n

router = APIRouter()

# Per-run state — keyed by run_id.
_run_queues:         dict[str, asyncio.Queue] = {}
_pause_events:       dict[str, asyncio.Event] = {}   # set = running, cleared = paused
_force_close_events: dict[str, asyncio.Event] = {}   # set = user requested end
_overrides:          dict[str, dict]          = {}   # current effective overrides
_override_logs:      dict[str, list]          = {}   # ordered log of applied overrides

RUNS_DIR = Path(__file__).parent.parent.parent / "runs"


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

    run_id = str(uuid.uuid4())
    run_cfg = DebateRunConfig.from_api(config)
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

    background_tasks.add_task(_run_debate_wrapper, run_id, run_cfg, run_dir, queue, pause_event, overrides, force_close_event)
    return {"run_id": run_id, "run_dir": run_dir.name}


async def _run_debate_wrapper(
    run_id: str, config: DebateRunConfig, run_dir: Path,
    queue: asyncio.Queue, pause_event: asyncio.Event, overrides: dict,
    force_close_event: asyncio.Event,
    **kwargs,
):
    from runners.debate import run_debate
    try:
        await run_debate(run_id, config, run_dir, queue, pause_event, overrides, force_close_event, **kwargs)
    except Exception as e:
        await queue.put({"type": "error", "message": str(e)})
        await queue.put(None)


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
    ev.clear()
    return {"status": "paused"}


@router.post("/debates/{run_id}/resume")
async def resume_debate(run_id: str):
    ev = _pause_events.get(run_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Run not found or already closed")
    ev.set()
    return {"status": "resumed"}


@router.post("/debates/{run_id}/end")
async def end_debate(run_id: str):
    """Signal the runner to close after the current turn completes."""
    ev = _force_close_events.get(run_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Run not found or already closed")
    # Also unpause if paused, so the runner can reach the check point.
    pause_ev = _pause_events.get(run_id)
    if pause_ev and not pause_ev.is_set():
        pause_ev.set()
    ev.set()
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

    return {"status": "ok", "applied": applied, "overrides": dict(ov)}


@router.get("/debates")
async def list_debates(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    runs = []
    if RUNS_DIR.exists():
        for run_dir in RUNS_DIR.iterdir():
            db_path = run_dir / "debate.db"
            if not db_path.exists():
                continue
            try:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute(
                    "SELECT run_id, created_at, status, debate_title, topic, "
                    "closure_reason, config, steelman_mode "
                    "FROM runs LIMIT 1"
                ).fetchone()
                if not row:
                    conn.close()
                    continue
                turns_row = conn.execute(
                    "SELECT COALESCE(MAX(turn), 0) FROM acts"
                ).fetchone()
                tok_row = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) FROM acts"
                ).fetchone()
                conn.close()
                cfg = _json.loads(row[6]) if row[6] else {}
                runs.append({
                    "run_id":               row[0],
                    "created_at":           row[1],
                    "status":               row[2],
                    "debate_title":         row[3],
                    "topic":                row[4],
                    "closure_reason":       row[5],
                    "run_dir":              run_dir.name,
                    "turn":                 turns_row[0] if turns_row else 0,
                    "total_tokens":         tok_row[0]   if tok_row   else 0,
                    "steelman_mode":        bool(row[7]) or bool(cfg.get("steelman_mode", False)),
                    "proposition_nickname": cfg.get("proposition", {}).get("nickname", "P"),
                    "opposition_nickname":  cfg.get("opposition", {}).get("nickname", "O"),
                })
            except Exception:
                continue
    runs.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    total = len(runs)
    return {"total": total, "items": runs[offset : offset + limit]}


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

    return {
        "run_id":         run[0],
        "created_at":     run[1],
        "status":         status,
        "debate_title":   run[3],
        "topic":          run[4],
        "closure_reason": closure_reason,
        "config":         parsed_cfg,
        "continued_from": run[7],
        "token_offset":   token_offset,
        "is_continuable": is_continuable,
        "acts":    [dict(zip(act_cols, row)) for row in acts],
        "claims":  [dict(zip(claim_cols, row)) for row in claims],
        "override_log": _override_logs.get(run_id, []),
    }


@router.get("/debates/{run_id}/export")
async def export_debate(run_id: str, format: str = "json"):
    db_path = _find_db(run_id)
    if not db_path:
        raise HTTPException(status_code=404, detail="Debate not found")

    run_dir  = db_path.parent
    is_md    = format == "markdown"
    pre_file = run_dir / ("debate.md" if is_md else "debate.json")

    if pre_file.exists():
        # Serve the pre-generated file — fast path, no DB query needed for content.
        conn = sqlite3.connect(str(db_path))
        row  = conn.execute(
            "SELECT debate_title, topic FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        title = (row[0] or row[1] or run_id) if row else run_id
        slug  = re.sub(r"[^a-z0-9]+", "-", title[:40].lower()).strip("-")
        ext   = "md" if is_md else "json"
        media = "text/markdown; charset=utf-8" if is_md else "application/json"
        return Response(
            content=pre_file.read_bytes(),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="agora-{slug}-{run_id[:8]}.{ext}"'},
        )

    # Fallback: generate on the fly (pre-existing debates without auto-generated files).
    data = await get_debate(run_id)
    slug = re.sub(
        r"[^a-z0-9]+", "-",
        (data["debate_title"] or data["topic"] or run_id)[:40].lower()
    ).strip("-")
    if is_md:
        filename = f"agora-{slug}-{run_id[:8]}.md"
        return Response(
            content=_build_markdown(data),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    filename = f"agora-{slug}-{run_id[:8]}.json"
    return Response(
        content=_json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

    background_tasks.add_task(
        _run_debate_wrapper,
        new_run_id, original_cfg, new_run_dir,
        queue, pause_event, overrides, force_close,
        initial_state=original_state,
        turn_idx_start=turn_idx,
        continued_from=run_id,
    )

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
    ids = body.get("ids") or []
    deleted, skipped = [], []
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
    return {"status": "ok", "deleted": deleted, "skipped": skipped}


@router.post("/debates/export")
async def batch_export(body: dict):
    """Download multiple debates as JSON or Markdown. Body: {"ids": [...], "format": "json"|"markdown"}"""
    ids    = body.get("ids") or []
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

def _make_run_dir_name(topic: str) -> str:
    import string, random
    date_str = datetime.utcnow().strftime("%Y%m%d")
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower())[:30].strip("-")
    return f"{date_str}_{rand_str}_{slug}"


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
