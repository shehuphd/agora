"""Per-run metrics, computed at close from data the run already recorded.

Every value comes from the run's own files (debate.db, search_log.jsonl) and
its registry row — never from a fresh model call, so computing metrics costs
nothing and can be repeated safely. NULL means unknown (the run predates the
field that would answer it), not zero.

Wired in two places: runners/debate.py computes metrics right after a run
closes, and api/main.py backfills missing metrics for older closed runs in a
startup thread (see backfill_missing).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from core import runs_db as _runs_db

# Same URL shape agents/base.py uses for citation extraction. An act's URLs
# are pool-verified at generation time (fabricated ones are stripped before
# the act is recorded), so any URL present in a stored act is a pooled one.
_URL_RE = re.compile(r'https?://[^\s<>\]]+')

# Act types that argue and are expected to cite. CHALLENGE is excluded: a
# challenge may legitimately attack reasoning rather than bring evidence.
_CITABLE_TYPES = ("ASSERT", "DEFEND")


def compute(run_dir: Path, idx_row: dict) -> dict:
    """Metrics for one closed run. idx_row is the run's registry row (dict
    with at least status, closure_reason, turn, total_tokens, total_cost_usd,
    cost_partial). Missing files degrade to NULL fields, never raise."""
    m: dict = {
        "completed":       int(idx_row.get("status") == "closed"),
        "closure_reason":  idx_row.get("closure_reason"),
        "turns":           idx_row.get("turn") or 0,
        "total_tokens":    idx_row.get("total_tokens") or 0,
        "total_cost_usd":  idx_row.get("total_cost_usd"),
        "cost_partial":    int(bool(idx_row.get("cost_partial"))),
        "challenge_count": None, "challenge_types_used": None,
        "concession_count": None, "citable_acts": None, "cited_acts": None,
        "citation_coverage": None, "retries": None, "citation_repairs": None,
        "argument_map_ok": None,
        "search_calls": None, "search_tiers": None,
    }
    _add_act_metrics(run_dir / "debate.db", m)
    _add_search_metrics(run_dir / "search_log.jsonl", m)
    return m


def _add_act_metrics(db_path: Path, m: dict) -> None:
    if not db_path.exists():
        return
    try:
        src = sqlite3.connect(str(db_path), timeout=2)
        src.row_factory = sqlite3.Row
        cols = {r["name"] for r in src.execute("PRAGMA table_info(acts)")}
        has_ct  = "challenge_type" in cols
        has_ret = "retries" in cols
        has_rep = "citation_repairs" in cols
        acts = src.execute(
            f"""SELECT act_type, content,
                       {'challenge_type' if has_ct else 'NULL AS challenge_type'},
                       {'retries' if has_ret else 'NULL AS retries'},
                       {'citation_repairs' if has_rep else 'NULL AS citation_repairs'}
                FROM acts"""
        ).fetchall()
        src.close()
    except Exception:
        return

    challenge_count = concessions = citable = cited = 0
    types_seen: set[str] = set()
    retries_known = False
    retries_total = 0
    repairs_known = False
    repairs_total = 0
    map_ok = None
    for a in acts:
        t = a["act_type"]
        if t == "CHALLENGE":
            challenge_count += 1
            # "multi" is a bundle, not a taxonomy entry, so it doesn't count
            # toward distinct types used.
            if a["challenge_type"] and a["challenge_type"] != "multi":
                types_seen.add(a["challenge_type"])
        elif t == "CONCEDE":
            concessions += 1
        elif t == "ARGUMENT_MAP":
            map_ok = int(bool((a["content"] or "").strip()))
        if t in _CITABLE_TYPES:
            citable += 1
            if _URL_RE.search(a["content"] or ""):
                cited += 1
        if a["retries"] is not None:
            retries_known = True
            retries_total += a["retries"]
        if a["citation_repairs"] is not None:
            repairs_known = True
            repairs_total += a["citation_repairs"]

    m["challenge_count"]  = challenge_count
    m["concession_count"] = concessions
    m["citable_acts"]     = citable
    m["cited_acts"]       = cited
    m["citation_coverage"] = round(cited / citable, 3) if citable else None
    m["argument_map_ok"]  = map_ok
    m["retries"] = retries_total if retries_known else None
    m["citation_repairs"] = repairs_total if repairs_known else None
    # Distinct types are only knowable when challenge acts carry named
    # taxonomy labels: zero challenges means zero types with certainty, but
    # challenges recorded before the field existed, or labelled only "multi"
    # (a bundle that doesn't name its types), leave the count unknown.
    if challenge_count == 0:
        m["challenge_types_used"] = 0
    else:
        m["challenge_types_used"] = len(types_seen) if types_seen else None


def _add_search_metrics(log_path: Path, m: dict) -> None:
    if not log_path.exists():
        return
    calls = 0
    tiers: set[str] = set()
    try:
        with log_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                calls += 1
                if entry.get("tier"):
                    tiers.add(entry["tier"])
    except Exception:
        return
    m["search_calls"] = calls
    m["search_tiers"] = json.dumps(sorted(tiers)) if tiers else None


def compute_and_store(run_id: str, run_dir: Path) -> bool:
    """Compute metrics for one run and write them to the registry.

    Returns True when a metrics row was written. Never raises: metrics are a
    read-model over finished runs, and no failure here may disturb the close
    path that calls it.
    """
    try:
        conn = _runs_db.connect()
        try:
            _runs_db.init(conn)
            row = conn.execute(
                "SELECT status, closure_reason, turn, total_tokens, total_cost_usd, cost_partial "
                "FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or row["status"] == "running":
                return False
            metrics = compute(run_dir, dict(row))
            _runs_db.upsert_run_metrics(conn, run_id, metrics)
            return True
        finally:
            conn.close()
    except Exception as exc:
        print(f"[metrics] compute failed for {run_id}: {exc}", flush=True)
        return False


def backfill_missing(runs_dir: Path) -> int:
    """Compute metrics for every closed run that has none yet.

    Called from a startup thread in api/main.py: idempotent, incremental,
    and each run is a self-contained unit of work, so a crash mid-way costs
    nothing but the remainder of the list next boot. Returns the number of
    runs backfilled.
    """
    try:
        conn = _runs_db.connect()
        try:
            _runs_db.init(conn)
            todo = _runs_db.runs_missing_metrics(conn)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[metrics] backfill scan failed: {exc}", flush=True)
        return 0
    done = 0
    for item in todo:
        if not item.get("run_dir"):
            continue
        if compute_and_store(item["run_id"], runs_dir / item["run_dir"]):
            done += 1
    if done:
        print(f"[metrics] backfilled {done} run(s)", flush=True)
    return done
