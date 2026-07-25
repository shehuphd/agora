"""Lightweight runs index — fast metadata queries without per-run DB traversal.

databases/runs.db holds one row per debate run with enough metadata to power
the history list at any scale. The per-run debate.db files remain authoritative
for act/claim detail; this file is the fast-path index.

Also stores provider_models — the list of inference-capable models per provider key,
refreshed whenever a key is tested. This is the single source of truth for the model
picker; the frontend never reads a hardcoded model list.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DATABASES_DIR = Path(__file__).parent.parent / "databases"
RUNS_DB_PATH  = DATABASES_DIR / "runs.db"

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS provider_models (
    provider      TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    display_name  TEXT,
    endpoint_type TEXT DEFAULT 'default',
    is_active     INTEGER DEFAULT 1,
    last_updated  TEXT,
    PRIMARY KEY (provider, model_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id               TEXT PRIMARY KEY,
    run_dir              TEXT,
    created_at           TEXT,
    status               TEXT DEFAULT 'running',
    debate_title         TEXT,
    topic                TEXT,
    closure_reason       TEXT,
    steelman_mode        INTEGER DEFAULT 0,
    proposition_nickname TEXT,
    opposition_nickname  TEXT,
    turn                 INTEGER DEFAULT 0,
    total_tokens         INTEGER DEFAULT 0,
    experiment_id        TEXT,
    continued_from       TEXT,
    config               TEXT,
    score                TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_experiment  ON runs(experiment_id);
CREATE INDEX IF NOT EXISTS idx_runs_status      ON runs(status);
"""


def connect() -> sqlite3.Connection:
    DATABASES_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(RUNS_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    run_dir: str,
    created_at: str,
    debate_title: str,
    topic: str,
    steelman_mode: bool,
    proposition_nickname: str,
    opposition_nickname: str,
    continued_from: str | None = None,
    config_json: str = "",
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO runs
           (run_id, run_dir, created_at, status, debate_title, topic,
            steelman_mode, proposition_nickname, opposition_nickname,
            continued_from, config)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, run_dir, created_at, "running", debate_title, topic,
         int(steelman_mode), proposition_nickname, opposition_nickname,
         continued_from, config_json),
    )
    conn.commit()


def update_on_close(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    closure_reason: str | None,
    debate_title: str,
    turn: int,
    total_tokens: int,
) -> None:
    conn.execute(
        """UPDATE runs
           SET status=?, closure_reason=?, debate_title=?, turn=?, total_tokens=?
           WHERE run_id=?""",
        (status, closure_reason, debate_title, turn, total_tokens, run_id),
    )
    conn.commit()


def delete_runs(conn: sqlite3.Connection, run_ids: list[str]) -> None:
    conn.executemany("DELETE FROM runs WHERE run_id=?", [(r,) for r in run_ids])
    conn.commit()


def list_runs(conn: sqlite3.Connection, limit: int = 50, offset: int = 0) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    rows = conn.execute(
        """SELECT r.run_id, r.run_dir, r.created_at, r.status, r.debate_title, r.topic,
                  r.closure_reason, r.steelman_mode, r.proposition_nickname, r.opposition_nickname,
                  r.turn, r.total_tokens, r.experiment_id, r.continued_from,
                  e.name AS experiment_name
           FROM runs r
           LEFT JOIN experiments e ON e.experiment_id = r.experiment_id
           ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    items = [
        {
            "run_id":               r["run_id"],
            "run_dir":              r["run_dir"],
            "created_at":           r["created_at"],
            "status":               r["status"],
            "debate_title":         r["debate_title"],
            "topic":                r["topic"],
            "closure_reason":       r["closure_reason"],
            "steelman_mode":        bool(r["steelman_mode"]),
            "proposition_nickname": r["proposition_nickname"] or "P",
            "opposition_nickname":  r["opposition_nickname"]  or "O",
            "turn":                 r["turn"]         or 0,
            "total_tokens":         r["total_tokens"] or 0,
            "experiment_id":        r["experiment_id"],
            "experiment_name":      r["experiment_name"],
            "continued_from":       r["continued_from"],
        }
        for r in rows
    ]
    return {"total": total, "items": items}


# ------------------------------------------------------------------
# Experiment CRUD
# ------------------------------------------------------------------

def create_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    name: str,
    description: str | None,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO experiments (experiment_id, name, description, created_at) VALUES (?,?,?,?)",
        (experiment_id, name, description, created_at),
    )
    conn.commit()


def delete_experiment(conn: sqlite3.Connection, experiment_id: str) -> None:
    conn.execute("UPDATE runs SET experiment_id=NULL WHERE experiment_id=?", (experiment_id,))
    conn.execute("DELETE FROM experiments WHERE experiment_id=?", (experiment_id,))
    conn.commit()


def list_experiments(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """SELECT e.experiment_id, e.name, e.description, e.created_at,
                  COUNT(r.run_id) AS run_count
           FROM experiments e
           LEFT JOIN runs r ON r.experiment_id = e.experiment_id
           GROUP BY e.experiment_id
           ORDER BY e.created_at DESC"""
    ).fetchall()
    return [
        {
            "experiment_id": r["experiment_id"],
            "name":          r["name"],
            "description":   r["description"],
            "created_at":    r["created_at"],
            "run_count":     r["run_count"],
        }
        for r in rows
    ]


def get_experiment(conn: sqlite3.Connection, experiment_id: str) -> dict | None:
    row = conn.execute(
        "SELECT experiment_id, name, description, created_at FROM experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    return dict(row) if row else None


def find_experiment_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT experiment_id, name, description, created_at FROM experiments WHERE name=? LIMIT 1",
        (name,),
    ).fetchone()
    return dict(row) if row else None


def assign_run(conn: sqlite3.Connection, run_id: str, experiment_id: str) -> None:
    conn.execute("UPDATE runs SET experiment_id=? WHERE run_id=?", (experiment_id, run_id))
    conn.commit()


def unassign_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("UPDATE runs SET experiment_id=NULL WHERE run_id=?", (run_id,))
    conn.commit()


def list_experiment_runs(conn: sqlite3.Connection, experiment_id: str, runs_dir: Path) -> list:
    rows = conn.execute(
        """SELECT run_id, run_dir, created_at, status, debate_title, topic,
                  closure_reason, steelman_mode, proposition_nickname, opposition_nickname,
                  turn, total_tokens, continued_from
           FROM runs WHERE experiment_id=? ORDER BY created_at DESC""",
        (experiment_id,),
    ).fetchall()
    items = []
    for r in rows:
        orphaned = not (runs_dir / r["run_dir"]).exists() if r["run_dir"] else True
        items.append({
            "run_id":               r["run_id"],
            "run_dir":              r["run_dir"],
            "created_at":           r["created_at"],
            "status":               r["status"],
            "debate_title":         r["debate_title"],
            "topic":                r["topic"],
            "closure_reason":       r["closure_reason"],
            "steelman_mode":        bool(r["steelman_mode"]),
            "proposition_nickname": r["proposition_nickname"] or "P",
            "opposition_nickname":  r["opposition_nickname"]  or "O",
            "turn":                 r["turn"]         or 0,
            "total_tokens":         r["total_tokens"] or 0,
            "continued_from":       r["continued_from"],
            "orphaned":             orphaned,
        })
    return items


def list_unassigned_runs(conn: sqlite3.Connection, limit: int = 100) -> list:
    rows = conn.execute(
        """SELECT run_id, debate_title, topic, created_at, status
           FROM runs WHERE experiment_id IS NULL
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id":        r["run_id"],
            "debate_title":  r["debate_title"],
            "topic":         r["topic"],
            "created_at":    r["created_at"],
            "status":        r["status"],
        }
        for r in rows
    ]


# ------------------------------------------------------------------
# Provider model registry CRUD
# ------------------------------------------------------------------

def upsert_provider_models(conn: sqlite3.Connection, provider: str, models: list[dict]) -> None:
    """Replace the active model list for a provider.

    All existing rows for this provider are marked inactive first, then the
    new list is upserted so models that reappear become active again.
    """
    now = datetime.utcnow().isoformat()
    conn.execute("UPDATE provider_models SET is_active=0 WHERE provider=?", (provider,))
    for m in models:
        conn.execute(
            """INSERT INTO provider_models (provider, model_id, display_name, endpoint_type, is_active, last_updated)
               VALUES (?,?,?,?,1,?)
               ON CONFLICT(provider, model_id) DO UPDATE SET
                 display_name   = excluded.display_name,
                 endpoint_type  = excluded.endpoint_type,
                 is_active      = 1,
                 last_updated   = excluded.last_updated""",
            (provider, m["model_id"], m.get("display_name", m["model_id"]),
             m.get("endpoint_type", "default"), now),
        )
    conn.commit()


def deactivate_provider_models(conn: sqlite3.Connection, provider: str) -> None:
    """Mark all models for a provider inactive (key failed validation)."""
    conn.execute("UPDATE provider_models SET is_active=0 WHERE provider=?", (provider,))
    conn.commit()


def list_available_models(
    conn: sqlite3.Connection,
    provider_order: list[str] | None = None,
) -> list[dict]:
    """Return all active models across all providers.

    provider_order controls which provider's models appear first in the list.
    Providers not in the list sort after those that are. Within each provider
    models are sorted alphabetically by model_id.
    """
    rows = conn.execute(
        """SELECT provider, model_id, display_name, endpoint_type
           FROM provider_models WHERE is_active=1
           ORDER BY model_id"""
    ).fetchall()
    models = [dict(r) for r in rows]
    if provider_order:
        idx = {p: i for i, p in enumerate(provider_order)}
        models.sort(key=lambda m: (idx.get(m["provider"], len(provider_order)), m["model_id"]))
    return models


def backfill(runs_dir: Path) -> None:
    """Scan runs/ and index any run not yet in runs.db.

    Called once on startup. Safe to call repeatedly — uses INSERT OR IGNORE.
    """
    if not runs_dir.exists():
        return
    conn = connect()
    init(conn)
    for run_dir in runs_dir.iterdir():
        db_path = run_dir / "debate.db"
        if not db_path.exists():
            continue
        try:
            src = sqlite3.connect(str(db_path), timeout=0.5)
            row = src.execute(
                "SELECT run_id, created_at, status, debate_title, topic, "
                "closure_reason, steelman_mode, config, continued_from FROM runs LIMIT 1"
            ).fetchone()
            if not row:
                src.close()
                continue

            run_id = row[0]
            if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                src.close()
                continue

            turns_row = src.execute("SELECT COALESCE(MAX(turn), 0) FROM acts").fetchone()
            tok_row   = src.execute(
                "SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) FROM acts"
            ).fetchone()
            src.close()

            cfg       = json.loads(row[7]) if row[7] else {}
            prop_nick = (cfg.get("proposition") or {}).get("nickname") or cfg.get("proposition_nickname", "P")
            opp_nick  = (cfg.get("opposition")  or {}).get("nickname") or cfg.get("opposition_nickname",  "O")

            conn.execute(
                """INSERT OR IGNORE INTO runs
                   (run_id, run_dir, created_at, status, debate_title, topic,
                    closure_reason, steelman_mode, proposition_nickname, opposition_nickname,
                    turn, total_tokens, continued_from, config)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, run_dir.name, row[1], row[2] or "closed",
                 row[3], row[4], row[5], int(bool(row[6])),
                 prop_nick, opp_nick,
                 turns_row[0] if turns_row else 0,
                 tok_row[0]   if tok_row   else 0,
                 row[8] if len(row) > 8 else None,
                 row[7]),
            )
            conn.commit()
        except Exception as exc:
            print(f"[runs_db] backfill skip {run_dir.name}: {exc}", flush=True)
            continue
    conn.close()
