"""Debates router — create and retrieve debate sessions."""
import asyncio
import json as _json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import Response

from api.models import DebateConfig
from core.config import DebateRunConfig

router = APIRouter()

# Per-session event queues — maxsize prevents unbounded growth if a consumer is slow.
_session_queues: dict[str, asyncio.Queue] = {}

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
    from runners.debate import run_debate

    session_id = str(uuid.uuid4())
    run_cfg = DebateRunConfig.from_api(config)
    run_dir = RUNS_DIR / _make_run_id(run_cfg.topic)

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _session_queues[session_id] = queue

    background_tasks.add_task(_run_debate_wrapper, session_id, run_cfg, run_dir, queue)
    return {"session_id": session_id, "run_id": run_dir.name}


async def _run_debate_wrapper(session_id: str, config: DebateRunConfig, run_dir: Path, queue: asyncio.Queue):
    from runners.debate import run_debate
    try:
        await run_debate(session_id, config, run_dir, queue)
    except Exception as e:
        await queue.put({"type": "error", "message": str(e)})
        await queue.put(None)


@router.get("/debates")
async def list_debates():
    sessions = []
    for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True) if RUNS_DIR.exists() else []:
        db_path = run_dir / "debate.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT session_id, created_at, status, debate_title, topic, closure_reason "
                "FROM sessions LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                sessions.append({
                    "session_id": row[0],
                    "created_at": row[1],
                    "status": row[2],
                    "debate_title": row[3],
                    "topic": row[4],
                    "closure_reason": row[5],
                    "run_dir": run_dir.name,
                })
        except Exception:
            continue
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
    }


@router.get("/debates/{session_id}/export")
async def export_debate(session_id: str):
    data = await get_debate(session_id)
    slug = re.sub(
        r"[^a-z0-9]+", "-",
        (data["debate_title"] or data["topic"] or session_id)[:40].lower()
    ).strip("-")
    filename = f"agora-{slug}-{session_id[:8]}.json"
    return Response(
        content=_json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/debates/export")
async def batch_export(body: dict):
    """Download multiple debates as a single JSON array. Body: {"ids": ["id1", ...]}"""
    ids = body.get("ids") or []
    results = []
    for sid in ids:
        try:
            results.append(await get_debate(sid))
        except HTTPException:
            pass
    filename = f"agora-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
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
