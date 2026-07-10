"""Debates router — create and retrieve debate sessions."""
import asyncio
import json as _json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import Response

from api.models import DebateConfig
from core.config import DebateRunConfig
from core.export import build_markdown as _build_markdown, _n

router = APIRouter()

# Per-session state — keyed by session_id.
_session_queues:     dict[str, asyncio.Queue] = {}
_pause_events:       dict[str, asyncio.Event] = {}   # set = running, cleared = paused
_force_close_events: dict[str, asyncio.Event] = {}   # set = user requested end
_overrides:          dict[str, dict]          = {}   # current effective overrides
_override_logs:      dict[str, list]          = {}   # ordered log of applied overrides

RUNS_DIR = Path(__file__).parent.parent.parent / "runs"


def get_queue(session_id: str) -> asyncio.Queue:
    if session_id not in _session_queues:
        raise HTTPException(status_code=404, detail="Session not found or debate not running")
    return _session_queues[session_id]


@router.post("/debates")
async def create_debate(config: DebateConfig, background_tasks: BackgroundTasks):
    """Start a new debate session from the submitted config."""
    import uuid
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=Path(".env").resolve(), override=True)

    session_id = str(uuid.uuid4())
    run_cfg = DebateRunConfig.from_api(config)
    run_dir = RUNS_DIR / _make_run_id(run_cfg.topic)

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    pause_event       = asyncio.Event()
    pause_event.set()  # not paused at start
    force_close_event = asyncio.Event()  # not set = still running
    overrides: dict = {}

    _session_queues[session_id]     = queue
    _pause_events[session_id]       = pause_event
    _force_close_events[session_id] = force_close_event
    _overrides[session_id]          = overrides
    _override_logs[session_id]      = []

    background_tasks.add_task(_run_debate_wrapper, session_id, run_cfg, run_dir, queue, pause_event, overrides, force_close_event)
    return {"session_id": session_id, "run_id": run_dir.name}


async def _run_debate_wrapper(
    session_id: str, config: DebateRunConfig, run_dir: Path,
    queue: asyncio.Queue, pause_event: asyncio.Event, overrides: dict,
    force_close_event: asyncio.Event,
):
    from runners.debate import run_debate
    try:
        await run_debate(session_id, config, run_dir, queue, pause_event, overrides, force_close_event)
    except Exception as e:
        await queue.put({"type": "error", "message": str(e)})
        await queue.put(None)


@router.post("/debates/{session_id}/pause")
async def pause_debate(session_id: str):
    ev = _pause_events.get(session_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Session not found or already closed")
    ev.clear()
    return {"status": "paused"}


@router.post("/debates/{session_id}/resume")
async def resume_debate(session_id: str):
    ev = _pause_events.get(session_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Session not found or already closed")
    ev.set()
    return {"status": "resumed"}


@router.post("/debates/{session_id}/end")
async def end_debate(session_id: str):
    """Signal the runner to close after the current turn completes."""
    ev = _force_close_events.get(session_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Session not found or already closed")
    # Also unpause if paused, so the runner can reach the check point.
    pause_ev = _pause_events.get(session_id)
    if pause_ev and not pause_ev.is_set():
        pause_ev.set()
    ev.set()
    return {"status": "ending"}


@router.post("/debates/{session_id}/override")
async def apply_override(session_id: str, body: dict):
    """Apply a mid-run override. Supported fields: token_budget (int), token_budget_delta (int)."""
    ov  = _overrides.get(session_id)
    log = _override_logs.get(session_id)
    q   = _session_queues.get(session_id)

    if ov is None:
        raise HTTPException(status_code=404, detail="Session not found")

    applied: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    if "token_budget" in body or "token_budget_delta" in body:
        old_val = ov.get("token_budget", None)  # None means using original config value
        if "token_budget" in body:
            new_val = int(body["token_budget"])
        else:
            # Delta: look up base from DB config if not already overridden
            if old_val is None:
                db = _find_db(session_id)
                if db:
                    conn = sqlite3.connect(str(db))
                    row = conn.execute("SELECT config FROM sessions WHERE session_id=?", (session_id,)).fetchone()
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
        db_path = _find_db(session_id)
        if db_path:
            overrides_path = db_path.parent / "overrides.json"
            try:
                overrides_path.write_text(_json.dumps(log, indent=2), encoding="utf-8")
            except Exception:
                pass

    return {"status": "ok", "applied": applied, "overrides": dict(ov)}


@router.get("/debates")
async def list_debates():
    sessions = []
    if RUNS_DIR.exists():
        for run_dir in RUNS_DIR.iterdir():
            db_path = run_dir / "debate.db"
            if not db_path.exists():
                continue
            try:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute(
                    "SELECT session_id, created_at, status, debate_title, topic, closure_reason "
                    "FROM sessions LIMIT 1"
                ).fetchone()
                if not row:
                    conn.close()
                    continue
                # Turn count: highest turn number seen in debate acts.
                turns_row = conn.execute(
                    "SELECT COALESCE(MAX(turn), 0) FROM acts"
                ).fetchone()
                # Token total: sum across all acts.
                tok_row = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) FROM acts"
                ).fetchone()
                conn.close()
                sessions.append({
                    "session_id":   row[0],
                    "created_at":   row[1],
                    "status":       row[2],
                    "debate_title": row[3],
                    "topic":        row[4],
                    "closure_reason": row[5],
                    "run_dir":      run_dir.name,
                    "turn":         turns_row[0] if turns_row else 0,
                    "total_tokens": tok_row[0]   if tok_row   else 0,
                })
            except Exception:
                continue
    # Sort by actual start time (ISO string sorts lexicographically = chronologically).
    sessions.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return sessions


@router.get("/debates/{session_id}")
async def get_debate(session_id: str):
    db_path = _find_db(session_id)
    if not db_path:
        raise HTTPException(status_code=404, detail="Debate not found")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN config TEXT")
        conn.commit()
    except Exception:
        pass

    session = conn.execute(
        "SELECT session_id, created_at, status, debate_title, topic, closure_reason, config "
        "FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Session record not found")

    acts = conn.execute(
        "SELECT * FROM acts WHERE session_id=? ORDER BY turn, timestamp", (session_id,)
    ).fetchall()
    claims = conn.execute(
        "SELECT * FROM claims WHERE session_id=?", (session_id,)
    ).fetchall()
    conn.close()

    act_cols = [
        "act_id", "session_id", "turn", "agent", "agent_role", "act_type",
        "claim_id", "target_act_id", "content", "reason",
        "input_tokens", "output_tokens", "model_used", "timestamp",
    ]
    claim_cols = ["claim_id", "session_id", "author", "content", "status", "last_updated"]

    raw_cfg = session[6]
    parsed_cfg = _json.loads(raw_cfg) if raw_cfg else {}

    return {
        "session_id": session[0],
        "created_at": session[1],
        "status": session[2],
        "debate_title": session[3],
        "topic": session[4],
        "closure_reason": session[5],
        "config": parsed_cfg,
        "acts": [dict(zip(act_cols, row)) for row in acts],
        "claims": [dict(zip(claim_cols, row)) for row in claims],
        "override_log": _override_logs.get(session_id, []),
    }


@router.get("/debates/{session_id}/export")
async def export_debate(session_id: str, format: str = "json"):
    db_path = _find_db(session_id)
    if not db_path:
        raise HTTPException(status_code=404, detail="Debate not found")

    run_dir  = db_path.parent
    is_md    = format == "markdown"
    pre_file = run_dir / ("debate.md" if is_md else "debate.json")

    if pre_file.exists():
        # Serve the pre-generated file — fast path, no DB query needed for content.
        conn = sqlite3.connect(str(db_path))
        row  = conn.execute(
            "SELECT debate_title, topic FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        conn.close()
        title = (row[0] or row[1] or session_id) if row else session_id
        slug  = re.sub(r"[^a-z0-9]+", "-", title[:40].lower()).strip("-")
        ext   = "md" if is_md else "json"
        media = "text/markdown; charset=utf-8" if is_md else "application/json"
        return Response(
            content=pre_file.read_bytes(),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="agora-{slug}-{session_id[:8]}.{ext}"'},
        )

    # Fallback: generate on the fly (pre-existing debates without auto-generated files).
    data = await get_debate(session_id)
    slug = re.sub(
        r"[^a-z0-9]+", "-",
        (data["debate_title"] or data["topic"] or session_id)[:40].lower()
    ).strip("-")
    if is_md:
        filename = f"agora-{slug}-{session_id[:8]}.md"
        return Response(
            content=_build_markdown(data),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    filename = f"agora-{slug}-{session_id[:8]}.json"
    return Response(
        content=_json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/debates/export")
async def batch_export(body: dict):
    """Download multiple debates as JSON or Markdown. Body: {"ids": [...], "format": "json"|"markdown"}"""
    ids    = body.get("ids") or []
    fmt    = body.get("format", "json")
    results = []
    for sid in ids:
        try:
            results.append(await get_debate(sid))
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

def _make_run_id(topic: str) -> str:
    import string, random
    date_str = datetime.utcnow().strftime("%Y%m%d")
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower())[:30].strip("-")
    return f"{date_str}_{rand_str}_{slug}"


def _find_db(session_id: str) -> Path | None:
    if not RUNS_DIR.exists():
        return None
    for run_dir in RUNS_DIR.iterdir():
        db_path = run_dir / "debate.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                conn.close()
                if row:
                    return db_path
            except Exception:
                continue
    return None
