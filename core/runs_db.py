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
    -- Vestigial as of the keycall migration (2026-08-09): keycall resolves
    -- OpenAI's Responses-vs-Chat-Completions routing internally per model,
    -- so this stopped being read anywhere. Left in the DDL rather than
    -- dropped so existing databases don't need a live ALTER TABLE; new rows
    -- just take the column default and nothing reads it back out.
    endpoint_type TEXT DEFAULT 'default',
    is_active     INTEGER DEFAULT 1,
    -- Set when the provider's inference endpoint rejected the model as
    -- unknown, even though its own listing advertises it. Distinct from
    -- is_active, which tracks whether the listing still contains the model:
    -- a re-sync must not resurrect something we have proof cannot be called.
    unservable    INTEGER DEFAULT 0,
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
    -- Dollar cost of total_tokens, priced by core/cost.py (backed by the
    -- `rates` registry) at run-close time. NULL when no role's model could
    -- be priced at all. cost_partial=1 means total_cost_usd sums only the
    -- roles that could be priced — some roles' cost is unknown,
    -- not zero, so the sum understates the true total.
    total_cost_usd       REAL DEFAULT NULL,
    cost_partial         INTEGER DEFAULT 0,
    experiment_id        TEXT,
    continued_from       TEXT,
    config               TEXT,
    score                TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    created_at    TEXT NOT NULL,
    -- Stored experiment design: base config + factors + replicates (or an
    -- explicit row list, for CSV imports). NULL for bucket-style experiments
    -- that only group hand-assigned runs.
    spec          TEXT,
    -- Environment record from the spec's first launch: package versions,
    -- price snapshot date, git commit. JSON; NULL until a launch happens.
    manifest      TEXT
);

-- Batch execution state. Persisted so a server restart mid-batch leaves an
-- inspectable record instead of orphaning every in-flight row: on startup,
-- rows still marked running are stamped 'interrupted' and can be retried.
CREATE TABLE IF NOT EXISTS batch_jobs (
    job_id        TEXT PRIMARY KEY,
    experiment_id TEXT,
    status        TEXT DEFAULT 'queued',
    -- Optional spend ceiling for the whole job, checked between row launches
    -- against the recorded cost of the job's closed runs. NULL = no ceiling.
    budget_usd    REAL,
    created_at    TEXT,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS batch_rows (
    job_id      TEXT NOT NULL,
    row_idx     INTEGER NOT NULL,
    topic       TEXT,
    config      TEXT,
    -- Factor levels this row represents when the job came from a spec
    -- expansion, e.g. {"proposition_model": "kimi-k3", "replicate": 2}.
    -- JSON; NULL for CSV rows without a condition.
    condition   TEXT,
    -- pending | running | done | failed | interrupted | skipped
    status      TEXT DEFAULT 'pending',
    run_id      TEXT,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT,
    PRIMARY KEY (job_id, row_idx)
);

-- Per-run metrics computed at close (and backfilled for older closed runs)
-- from data the run already recorded — acts, sources.json, search_log.jsonl.
-- Never from a fresh model call: every value here is priced at zero.
-- NULL means unknown (the source data predates the field), not zero.
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id               TEXT PRIMARY KEY,
    computed_at          TEXT,
    completed            INTEGER,
    closure_reason       TEXT,
    turns                INTEGER,
    total_tokens         INTEGER,
    total_cost_usd       REAL,
    cost_partial         INTEGER,
    challenge_count      INTEGER,
    challenge_types_used INTEGER,
    concession_count     INTEGER,
    citable_acts         INTEGER,
    cited_acts           INTEGER,
    citation_coverage    REAL,
    retries              INTEGER,
    argument_map_ok      INTEGER,
    search_calls         INTEGER,
    search_tiers         TEXT,
    citation_repairs     INTEGER
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
    # Additive migrations for databases created before a column existed.
    for stmt in (
        "ALTER TABLE provider_models ADD COLUMN unservable INTEGER DEFAULT 0",
        "ALTER TABLE runs ADD COLUMN total_cost_usd REAL DEFAULT NULL",
        "ALTER TABLE runs ADD COLUMN cost_partial INTEGER DEFAULT 0",
        "ALTER TABLE runs ADD COLUMN condition TEXT",
        "ALTER TABLE experiments ADD COLUMN spec TEXT",
        "ALTER TABLE experiments ADD COLUMN manifest TEXT",
        "ALTER TABLE run_metrics ADD COLUMN citation_repairs INTEGER",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # already present
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
    condition: str | None = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO runs
           (run_id, run_dir, created_at, status, debate_title, topic,
            steelman_mode, proposition_nickname, opposition_nickname,
            continued_from, config, condition)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, run_dir, created_at, "running", debate_title, topic,
         int(steelman_mode), proposition_nickname, opposition_nickname,
         continued_from, config_json, condition),
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
    total_cost_usd: float | None = None,
    cost_partial: bool = False,
) -> None:
    conn.execute(
        """UPDATE runs
           SET status=?, closure_reason=?, debate_title=?, turn=?, total_tokens=?,
               total_cost_usd=?, cost_partial=?
           WHERE run_id=?""",
        (status, closure_reason, debate_title, turn, total_tokens,
         total_cost_usd, int(cost_partial), run_id),
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
                  r.turn, r.total_tokens, r.total_cost_usd, r.cost_partial,
                  r.experiment_id, r.continued_from,
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
            "total_cost_usd":       r["total_cost_usd"],
            "cost_partial":         bool(r["cost_partial"]),
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
        "SELECT experiment_id, name, description, created_at, spec, manifest "
        "FROM experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    return dict(row) if row else None


def set_experiment_spec(conn: sqlite3.Connection, experiment_id: str, spec_json: str | None) -> None:
    conn.execute("UPDATE experiments SET spec=? WHERE experiment_id=?", (spec_json, experiment_id))
    conn.commit()


def set_experiment_manifest(conn: sqlite3.Connection, experiment_id: str, manifest_json: str) -> None:
    conn.execute("UPDATE experiments SET manifest=? WHERE experiment_id=?", (manifest_json, experiment_id))
    conn.commit()


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
                  turn, total_tokens, total_cost_usd, cost_partial, continued_from, condition
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
            "total_cost_usd":       r["total_cost_usd"],
            "cost_partial":         bool(r["cost_partial"]),
            "continued_from":       r["continued_from"],
            "condition":            json.loads(r["condition"]) if r["condition"] else None,
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
# Batch jobs — durable state for core/batch.py
# ------------------------------------------------------------------

def insert_batch_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    experiment_id: str | None,
    rows: list[dict],
    budget_usd: float | None = None,
    created_at: str | None = None,
) -> None:
    """Create a job and its rows in one transaction.

    Each entry in `rows` is {"topic": str, "config": dict, "condition": dict|None}.
    """
    created = created_at or datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO batch_jobs (job_id, experiment_id, status, budget_usd, created_at) "
        "VALUES (?,?,?,?,?)",
        (job_id, experiment_id, "queued", budget_usd, created),
    )
    conn.executemany(
        "INSERT INTO batch_rows (job_id, row_idx, topic, config, condition, status) "
        "VALUES (?,?,?,?,?,?)",
        [
            (job_id, i, r.get("topic", ""), json.dumps(r.get("config") or {}),
             json.dumps(r["condition"]) if r.get("condition") else None, "pending")
            for i, r in enumerate(rows)
        ],
    )
    conn.commit()


def get_batch_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    job = conn.execute(
        "SELECT job_id, experiment_id, status, budget_usd, created_at, finished_at "
        "FROM batch_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not job:
        return None
    rows = conn.execute(
        "SELECT row_idx, topic, config, condition, status, run_id, error, started_at, finished_at "
        "FROM batch_rows WHERE job_id=? ORDER BY row_idx",
        (job_id,),
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "job_id":        job["job_id"],
        "experiment_id": job["experiment_id"],
        "status":        job["status"],
        "budget_usd":    job["budget_usd"],
        "created_at":    job["created_at"],
        "finished_at":   job["finished_at"],
        "total":         len(rows),
        "done":          counts.get("done", 0),
        "failed":        counts.get("failed", 0),
        "running":       counts.get("running", 0),
        "pending":       counts.get("pending", 0),
        "interrupted":   counts.get("interrupted", 0),
        "skipped":       counts.get("skipped", 0),
        "rows": [
            {
                "row_idx":     r["row_idx"],
                "topic":       r["topic"],
                "config":      json.loads(r["config"]) if r["config"] else {},
                "condition":   json.loads(r["condition"]) if r["condition"] else None,
                "status":      r["status"],
                "run_id":      r["run_id"],
                "error":       r["error"],
                "started_at":  r["started_at"],
                "finished_at": r["finished_at"],
            }
            for r in rows
        ],
    }


def update_batch_row(
    conn: sqlite3.Connection, job_id: str, row_idx: int, *,
    status: str,
    run_id: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    conn.execute(
        """UPDATE batch_rows
           SET status=?,
               run_id=COALESCE(?, run_id),
               error=COALESCE(?, error),
               started_at=COALESCE(?, started_at),
               finished_at=COALESCE(?, finished_at)
           WHERE job_id=? AND row_idx=?""",
        (status, run_id, error, started_at, finished_at, job_id, row_idx),
    )
    conn.commit()


def set_batch_job_status(
    conn: sqlite3.Connection, job_id: str, status: str,
    finished_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE batch_jobs SET status=?, finished_at=COALESCE(?, finished_at) WHERE job_id=?",
        (status, finished_at, job_id),
    )
    conn.commit()


def mark_interrupted_batches(conn: sqlite3.Connection) -> int:
    """Stamp rows and jobs a dead server left mid-flight.

    Called once at worker startup, before anything is dequeued: a row still
    marked running belongs to a process that no longer exists. Returns the
    number of rows stamped.
    """
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "UPDATE batch_rows SET status='interrupted', "
        "error='server stopped while this row was running', finished_at=? "
        "WHERE status IN ('running', 'pending') AND job_id IN "
        "(SELECT job_id FROM batch_jobs WHERE status IN ('queued', 'running'))",
        (now,),
    )
    conn.execute(
        "UPDATE batch_jobs SET status='interrupted', finished_at=? "
        "WHERE status IN ('queued', 'running')",
        (now,),
    )
    conn.commit()
    return cur.rowcount


def batch_job_spent_usd(conn: sqlite3.Connection, job_id: str) -> tuple[float, bool]:
    """Recorded spend of the job's closed runs, for the budget ceiling check.

    Returns (total, partial): partial=True when any counted run was itself
    cost-partial or any finished row's run has no recorded cost — the total
    is then a minimum, which is the safe direction for a ceiling.
    """
    rows = conn.execute(
        """SELECT r.total_cost_usd, r.cost_partial
           FROM batch_rows b JOIN runs r ON r.run_id = b.run_id
           WHERE b.job_id=? AND b.run_id IS NOT NULL AND r.status != 'running'""",
        (job_id,),
    ).fetchall()
    total = 0.0
    partial = False
    for r in rows:
        if r["total_cost_usd"] is None:
            partial = True
        else:
            total += r["total_cost_usd"]
            partial = partial or bool(r["cost_partial"])
    return total, partial


# ------------------------------------------------------------------
# Run metrics — written by core/metrics.py
# ------------------------------------------------------------------

_METRIC_FIELDS = (
    "completed", "closure_reason", "turns", "total_tokens", "total_cost_usd",
    "cost_partial", "challenge_count", "challenge_types_used", "concession_count",
    "citable_acts", "cited_acts", "citation_coverage", "retries",
    "argument_map_ok", "search_calls", "search_tiers", "citation_repairs",
)


def upsert_run_metrics(conn: sqlite3.Connection, run_id: str, metrics: dict) -> None:
    values = [metrics.get(f) for f in _METRIC_FIELDS]
    placeholders = ",".join("?" for _ in _METRIC_FIELDS)
    conn.execute(
        f"INSERT OR REPLACE INTO run_metrics (run_id, computed_at, {','.join(_METRIC_FIELDS)}) "
        f"VALUES (?,?,{placeholders})",
        [run_id, datetime.utcnow().isoformat(), *values],
    )
    conn.commit()


def get_run_metrics_map(conn: sqlite3.Connection, run_ids: list[str]) -> dict[str, dict]:
    """Metrics keyed by run_id for the given runs; absent ids are omitted."""
    if not run_ids:
        return {}
    marks = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT run_id, computed_at, {','.join(_METRIC_FIELDS)} "
        f"FROM run_metrics WHERE run_id IN ({marks})",
        run_ids,
    ).fetchall()
    return {r["run_id"]: dict(r) for r in rows}


def runs_missing_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Closed runs with no metrics row yet — the backfill work list."""
    rows = conn.execute(
        """SELECT r.run_id, r.run_dir FROM runs r
           LEFT JOIN run_metrics m ON m.run_id = r.run_id
           WHERE r.status != 'running' AND m.run_id IS NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


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
            """INSERT INTO provider_models (provider, model_id, display_name, is_active, last_updated)
               VALUES (?,?,?,1,?)
               ON CONFLICT(provider, model_id) DO UPDATE SET
                 display_name   = excluded.display_name,
                 -- A model proven uncallable stays retired however many times
                 -- the provider keeps advertising it.
                 is_active      = CASE WHEN provider_models.unservable=1 THEN 0 ELSE 1 END,
                 last_updated   = excluded.last_updated""",
            (provider, m["model_id"], m.get("display_name", m["model_id"]), now),
        )
    conn.commit()


def deactivate_provider_models(conn: sqlite3.Connection, provider: str) -> None:
    """Mark all models for a provider inactive (key failed validation)."""
    conn.execute("UPDATE provider_models SET is_active=0 WHERE provider=?", (provider,))
    conn.commit()


def mark_model_unservable(conn: sqlite3.Connection, provider: str, model_id: str) -> None:
    """Retire a model whose provider rejected it as unknown at inference time.

    A provider's model listing is not always its inference catalogue: Perplexity
    lists everything on its platform, but /chat/completions serves only some of
    them. Rather than encode which those are — a hardcoded list would rot as the
    vendor changes — the model is retired the first time the provider itself
    says it does not exist.

    The flag is separate from is_active so the next model sync, which reactivates
    everything the listing still contains, cannot resurrect it.
    """
    conn.execute(
        "UPDATE provider_models SET is_active=0, unservable=1 "
        "WHERE provider=? AND model_id=?",
        (provider, model_id),
    )
    conn.commit()


class ModelNotRoutable(ValueError):
    """A model selection could not be resolved to a single registry entry."""


def resolve_model(conn: sqlite3.Connection, model_id: str,
                  provider: str | None = None) -> dict:
    """Resolve a selection to the one entry that says how to call it.

    Returns {"provider", "model_id"}.

    The registry is the only credible source, and it is not an inference: rows
    are written by whichever adapter enumerated the model, using that provider's
    own key, so the serving provider is recorded as fact. A model's *name* says
    nothing — Perplexity resells kimi-* and glm-*, which look like nobody's
    first-party models — and no listing field helps either: `owned_by` reports
    who built the model, so Perplexity lists anthropic/claude-opus-5 as
    owned_by=anthropic.

    Pass `provider` to pin a specific vendor. It is required whenever more than
    one serves the id, which is a legitimate case, supported deliberately:
    kimi-k3 direct from Moonshot and the same model resold by Perplexity are
    different endpoints, keys, and prices, and a debate may legitimately want
    one on each side.
    """
    if provider:
        rows = conn.execute(
            "SELECT provider, model_id FROM provider_models "
            "WHERE model_id=? AND provider=? AND is_active=1",
            (model_id, provider),
        ).fetchall()
        if not rows:
            raise ModelNotRoutable(
                f"'{provider}' does not serve model '{model_id}'. Test that "
                f"provider's API key in Settings to refresh its model list."
            )
        return dict(rows[0])

    rows = conn.execute(
        "SELECT provider, model_id FROM provider_models "
        "WHERE model_id=? AND is_active=1 ORDER BY provider",
        (model_id,),
    ).fetchall()
    if not rows:
        raise ModelNotRoutable(
            f"No provider is registered for model '{model_id}'. Test the "
            f"relevant API key in Settings to refresh the model list, or pick "
            f"a different model."
        )
    if len(rows) > 1:
        served_by = ", ".join(r["provider"] for r in rows)
        raise ModelNotRoutable(
            f"Model '{model_id}' is served by more than one provider "
            f"({served_by}). Say which one to use — the same model from two "
            f"vendors means different endpoints, keys, and prices."
        )
    return dict(rows[0])


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
        """SELECT provider, model_id, display_name
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
