"""Run pack — the complete, auditable record of one debate.

The transcript export answers "what was said". The pack answers "how did it get
there": which query each agent wrote, which search tier answered it, what pages
came back, what text was extracted from them, which URLs each act cited, which
were fabricated and stripped, and what every model call cost.

Four sources are stitched together, joined on (turn, agent_role):
  debate.db / state   — the acts and claims
  sources.json        — the evidence pool, with trafilatura excerpts
  search_log.jsonl    — raw SERP responses, the evidence lockfile
  traces.jsonl        — traceact spans: steps, timings, token counts

Nothing here is recomputed. If a number appears in the pack it was recorded at
the time it happened, which is what makes the pack evidence rather than a
summary.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.export import _n, format_act_content

PACK_VERSION = "1.0"

# Steps traceact records as "cite.check: N pooled, M fabricated".
_CITE_CHECK_RE = re.compile(r"cite\.check:\s*(\d+)\s*pooled,\s*(\d+)\s*fabricated")
_RETRIEVE_RE = re.compile(r"retrieve:\s*(\d+)\+(\d+)\s*tokens,\s*pool=(\d+)")


# ---------------------------------------------------------------------------
# Loaders — every one is best-effort; a missing file degrades the pack, never
# fails it. A pack for a run whose search log was deleted is still worth having.
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_jsonl(path: Path) -> list[dict]:
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception:
        return []


def load_traces_for_run(run_id: str, traces_dir: Path) -> list[dict]:
    """Every trace whose correlation_id is this run, oldest first.

    Reads the folder rather than traces.jsonl alone so rotated segments still
    resolve — a run older than the 50MB rotation point would otherwise vanish
    from its own pack.
    """
    if not traces_dir.exists():
        return []
    try:
        from traceact import TraceLog
        log = TraceLog(str(traces_dir), max_lines_scanned=200_000)
        result = log.filter(correlation_id=run_id).query(2000)
        traces = result.get("traces", [])
    except Exception:
        traces = [
            t for p in sorted(traces_dir.glob("*.jsonl"))
            for t in _load_jsonl(p)
            if t.get("correlation_id") == run_id
        ]
    return sorted(traces, key=lambda t: t.get("started_at") or "")


# ---------------------------------------------------------------------------
# Trace readers
# ---------------------------------------------------------------------------

def _step_labels(trace: dict) -> list[str]:
    """Step labels as plain strings — traceact 0.8 records dicts, older ones str."""
    out = []
    for s in trace.get("steps") or []:
        label = s.get("label") if isinstance(s, dict) else s
        if label:
            out.append(str(label))
    return out


def _trace_tokens(trace: dict) -> tuple[int, int]:
    tin = tout = 0
    for ev in trace.get("events") or []:
        tin += ev.get("tokens_in") or 0
        tout += ev.get("tokens_out") or 0
    return tin, tout


def _summarise_trace(trace: dict) -> dict:
    """The parts of a span worth keeping in a turn record."""
    tin, tout = _trace_tokens(trace)
    outputs = trace.get("outputs") or {}
    steps = _step_labels(trace)

    pooled = fabricated = None
    retrieval_tokens = None
    for label in steps:
        m = _CITE_CHECK_RE.search(label)
        if m:
            pooled, fabricated = int(m.group(1)), int(m.group(2))
        m = _RETRIEVE_RE.search(label)
        if m:
            retrieval_tokens = {
                "input": int(m.group(1)),
                "output": int(m.group(2)),
                "pool_size_at_call": int(m.group(3)),
            }

    return {
        "trace_id": trace.get("trace_id"),
        "action": trace.get("action"),
        "actor": trace.get("actor"),
        "status": trace.get("status"),
        "started_at": trace.get("started_at"),
        "duration_ms": trace.get("duration_ms"),
        "model": (trace.get("meta") or {}).get("model"),
        "tokens_in": tin,
        "tokens_out": tout,
        "retrieval_tokens": retrieval_tokens,
        "citations_pooled": pooled,
        "citations_fabricated": fabricated,
        "urls_found": outputs.get("urls_found") or [],
        "fabricated_urls": outputs.get("fabricated_urls") or [],
        "steps": steps,
        "errors": trace.get("errors") or [],
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _index_sources_by_turn(sources: list[dict]) -> dict[tuple[int, str], list[dict]]:
    idx: dict[tuple[int, str], list[dict]] = {}
    for s in sources:
        key = (int(s.get("turn") or 0), s.get("harvested_by") or "")
        idx.setdefault(key, []).append(s)
    return idx


def _index_searches_by_query(searches: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for entry in searches:
        idx.setdefault(entry.get("query") or "", []).append(entry)
    return idx


def _generate_traces_by_turn(traces: list[dict]) -> dict[tuple[int, str], dict]:
    """agent.generate spans keyed by (turn, actor).

    A turn can hold two spans for one actor when the JSON-repair path fires;
    the first is kept, since that is the call the act actually came from.
    """
    idx: dict[tuple[int, str], dict] = {}
    for t in traces:
        if t.get("action") != "agent.generate":
            continue
        turn = (t.get("meta") or {}).get("turn")
        actor = t.get("actor") or ""
        if turn is None:
            continue
        idx.setdefault((int(turn), actor), t)
    return idx


def _source_digest(s: dict, include_excerpt: bool = True) -> dict:
    """One pool entry. `excerpt_chars` is always reported; the body is optional.

    Truncation belongs to the renderer, not here — the JSON pack carries the
    excerpt whole so a reader can reproduce what the agent actually saw.
    """
    excerpt = s.get("excerpt") or ""
    out = {
        "url": s.get("url"),
        "title": s.get("title"),
        "snippet": s.get("snippet"),
        "published": s.get("published"),
        "provider": s.get("provider"),
        "harvested_by": s.get("harvested_by"),
        "query": s.get("query"),
        "turn": s.get("turn"),
        "harvested_at": s.get("harvested_at"),
        "excerpt_chars": len(excerpt),
        "enriched": bool(excerpt),
    }
    if include_excerpt and excerpt:
        out["excerpt"] = excerpt
    return out


def build_run_pack(
    data: dict,
    run_dir: Path,
    traces_dir: Path,
    include_raw_serp: bool = False,
) -> dict:
    """Assemble the full pack for one run.

    `data` is the /debates/{id} response shape. `include_raw_serp` keeps the
    unabridged SERP JSON from the lockfile — exact, large, and mostly of use
    when reproducing a run rather than reading one.
    """
    run_id = data.get("run_id") or ""
    acts = data.get("acts") or []

    pool_payload = _load_json(run_dir / "sources.json", {})
    sources = pool_payload.get("sources") or []
    searches = _load_jsonl(run_dir / "search_log.jsonl")
    traces = load_traces_for_run(run_id, traces_dir)

    sources_by_turn = _index_sources_by_turn(sources)
    searches_by_query = _index_searches_by_query(searches)
    gen_traces = _generate_traces_by_turn(traces)

    # ---- per-turn records: act joined to its retrieval, citations, and span
    turns_out = []
    for act in acts:
        turn = int(act.get("turn") or 0)
        role = act.get("agent_role") or ""
        trace = gen_traces.get((turn, role))
        trace_summary = _summarise_trace(trace) if trace else None

        turn_sources = sources_by_turn.get((turn, role), [])
        query = next((s.get("query") for s in turn_sources if s.get("query")), None)

        retrieval = None
        if turn_sources or query:
            log_entries = searches_by_query.get(query or "", [])
            entry = log_entries[0] if log_entries else {}
            enriched = [s for s in turn_sources if s.get("excerpt")]
            retrieval = {
                "query": query,
                "tier": entry.get("tier") or (turn_sources[0].get("provider") if turn_sources else None),
                "searched_at": entry.get("at"),
                "results_returned": entry.get("result_count"),
                "sources_pooled": len(turn_sources),
                "pages_enriched": len(enriched),
                "sources": [_source_digest(s) for s in turn_sources],
            }
            if trace_summary and trace_summary.get("retrieval_tokens"):
                retrieval["query_write_tokens"] = trace_summary["retrieval_tokens"]

        citations = None
        if trace_summary:
            citations = {
                "cited": trace_summary["urls_found"],
                "fabricated": trace_summary["fabricated_urls"],
                "pooled_count": trace_summary["citations_pooled"],
                "fabricated_count": trace_summary["citations_fabricated"],
            }

        turns_out.append({
            "turn": turn,
            "agent": act.get("agent"),
            "agent_role": role,
            "act_type": act.get("act_type"),
            "act_id": act.get("act_id"),
            "claim_id": act.get("claim_id"),
            "target_act_id": act.get("target_act_id"),
            "model": act.get("model_used"),
            "timestamp": act.get("timestamp"),
            "tokens": {
                "input": act.get("input_tokens") or 0,
                "output": act.get("output_tokens") or 0,
            },
            "content": act.get("content"),
            "reason": act.get("reason"),
            "retrieval": retrieval,
            "citations": citations,
            "trace": trace_summary,
        })

    # ---- integrity: the numbers that say whether the run can be trusted
    total_cited = sum(len((t.get("citations") or {}).get("cited") or []) for t in turns_out)
    total_fabricated = sum(len((t.get("citations") or {}).get("fabricated") or []) for t in turns_out)
    enriched_sources = [s for s in sources if s.get("excerpt")]
    tiers = sorted({e.get("tier") for e in searches if e.get("tier")})

    integrity = {
        "sources_pooled": len(sources),
        "sources_enriched": len(enriched_sources),
        "enrichment_rate": round(len(enriched_sources) / len(sources), 3) if sources else 0.0,
        "searches_run": len(searches),
        "search_tiers_used": tiers,
        "urls_cited": total_cited,
        "urls_fabricated": total_fabricated,
        "fabrication_rate": round(total_fabricated / total_cited, 3) if total_cited else 0.0,
        "traces_captured": len(traces),
        "trace_errors": sum(1 for t in traces if t.get("errors")),
    }

    # ---- token usage by role, from the acts themselves
    usage: dict[str, dict[str, int]] = {}
    for act in acts:
        role = act.get("agent_role") or "unknown"
        u = usage.setdefault(role, {"input": 0, "output": 0, "calls": 0})
        u["input"] += act.get("input_tokens") or 0
        u["output"] += act.get("output_tokens") or 0
        u["calls"] += 1
    for u in usage.values():
        u["total"] = u["input"] + u["output"]
    grand_total = sum(u["total"] for u in usage.values())
    for role, u in usage.items():
        u["share"] = round(u["total"] / grand_total, 3) if grand_total else 0.0

    searches_out = []
    for e in searches:
        entry = {
            "at": e.get("at"),
            "tier": e.get("tier"),
            "query": e.get("query"),
            "result_count": e.get("result_count"),
        }
        if include_raw_serp:
            entry["raw"] = e.get("raw")
        searches_out.append(entry)

    return {
        "pack_version": PACK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": {
            "run_id": run_id,
            "debate_title": data.get("debate_title"),
            "topic": data.get("topic"),
            "status": data.get("status"),
            "created_at": data.get("created_at"),
            "closure_reason": data.get("closure_reason"),
            "continued_from": data.get("continued_from"),
            "experiment_id": data.get("experiment_id"),
            "experiment_name": data.get("experiment_name"),
        },
        "config": data.get("config") or {},
        "token_usage": {"by_role": usage, "total": grand_total},
        "integrity": integrity,
        "turns": turns_out,
        "claims": data.get("claims") or [],
        "evidence_pool": [_source_digest(s) for s in sources],
        "searches": searches_out,
        "traces": traces,
        "override_log": data.get("override_log") or [],
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _md_table(headers: list[str], rows: list[list[str]], align_right: set[int] | None = None) -> list[str]:
    align_right = align_right or set()
    sep = ["---:" if i in align_right else "---" for i in range(len(headers))]
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(sep) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def _trunc(s, n: int) -> str:
    s = str(s or "")
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def build_pack_markdown(pack: dict, excerpt_chars: int = 800) -> str:
    """Render a pack as a readable audit document."""
    run = pack.get("run") or {}
    cfg = pack.get("config") or {}
    integ = pack.get("integrity") or {}
    usage = (pack.get("token_usage") or {}).get("by_role") or {}
    turns = pack.get("turns") or []

    title = run.get("debate_title") or run.get("topic") or run.get("run_id") or "Debate"
    L: list[str] = [
        f"# {title} — run pack",
        "",
        "> Complete audit record: every call, query, source, and citation.",
        f"> Generated by [Agora](https://github.com/shehuphd/agora) · pack v{pack.get('pack_version')}",
        "",
        "## Run",
        "",
    ]
    L += _md_table(["Field", "Value"], [
        ["Run ID", f"`{run.get('run_id', '')}`"],
        ["Topic", _trunc(run.get("topic"), 200)],
        ["Status", run.get("status") or "—"],
        ["Created", str(run.get("created_at") or "")[:19].replace("T", " ")],
        ["Closure reason", run.get("closure_reason") or "—"],
        ["Continued from", f"`{run['continued_from']}`" if run.get("continued_from") else "—"],
        ["Experiment", run.get("experiment_name") or "—"],
        ["Packed at", str(pack.get("generated_at") or "")[:19].replace("T", " ")],
    ])

    # ---- integrity first: it is the reason the pack exists
    L += ["", "## Integrity", "", ""]
    fab = integ.get("urls_fabricated", 0)
    L[-1] = (
        f"**{fab} fabricated citation(s)** out of {integ.get('urls_cited', 0)} URLs cited."
        if fab else
        f"**No fabricated citations.** All {integ.get('urls_cited', 0)} cited URLs came from the evidence pool."
    )
    L += [""]
    L += _md_table(["Metric", "Value"], [
        ["Sources pooled", _n(integ.get("sources_pooled", 0))],
        ["Sources with page text", f"{_n(integ.get('sources_enriched', 0))} ({integ.get('enrichment_rate', 0):.0%})"],
        ["Searches run", _n(integ.get("searches_run", 0))],
        ["Search tiers used", ", ".join(integ.get("search_tiers_used") or []) or "—"],
        ["URLs cited", _n(integ.get("urls_cited", 0))],
        ["URLs fabricated (stripped)", _n(fab)],
        ["Fabrication rate", f"{integ.get('fabrication_rate', 0):.1%}"],
        ["Traces captured", _n(integ.get("traces_captured", 0))],
        ["Traces with errors", _n(integ.get("trace_errors", 0))],
    ], align_right={1})

    # ---- config
    L += ["", "## Configuration", ""]
    cfg_rows = [
        ["Proposition", f"{cfg.get('proposition_nickname', '—')} · `{cfg.get('proposition_model', '—')}` · temp {_n(cfg.get('temperature_proposition'))}"],
        ["Opposition", f"{cfg.get('opposition_nickname', '—')} · `{cfg.get('opposition_model', '—')}` · temp {_n(cfg.get('temperature_opposition'))}"],
        ["Moderator", f"`{cfg.get('moderator_model', '—')}`"],
        ["Synthesiser", f"`{cfg.get('synthesiser_model', '—')}`"],
        ["Max turns", _n(cfg.get("max_turns"))],
        ["Token budget", _n(cfg.get("token_budget"))],
        ["Steelman mode", "Yes" if cfg.get("steelman_mode") else "No"],
    ]
    L += _md_table(["Setting", "Value"], cfg_rows)

    # ---- token usage
    L += ["", "## Token usage", ""]
    rows = []
    for role in ("proposition", "opposition", "moderator", "synthesiser"):
        u = usage.get(role)
        if not u:
            continue
        rows.append([
            role, _n(u["calls"]), _n(u["input"]), _n(u["output"]),
            _n(u["total"]), f"{u['share']:.0%}",
        ])
    total = (pack.get("token_usage") or {}).get("total", 0)
    rows.append(["**total**", "", "", "", f"**{_n(total)}**", "100%"])
    L += _md_table(["Role", "Calls", "In", "Out", "Total", "Share"], rows,
                   align_right={1, 2, 3, 4, 5})

    # ---- retrieval ledger: one row per search
    searches = pack.get("searches") or []
    if searches:
        L += ["", "## Retrieval ledger", ""]
        L += _md_table(
            ["#", "Time", "Tier", "Query", "Results"],
            [[i + 1, str(s.get("at") or "")[11:19], s.get("tier") or "—",
              _trunc(s.get("query"), 70), _n(s.get("result_count") or 0)]
             for i, s in enumerate(searches)],
            align_right={4},
        )

    # ---- turn by turn: the body of the pack
    L += ["", "## Turn-by-turn record", ""]
    for t in turns:
        head = f"### Turn {t.get('turn')} · {t.get('agent') or t.get('agent_role')} · {t.get('act_type')}"
        L += [head, ""]

        meta = [f"model `{t.get('model') or '—'}`"]
        tk = t.get("tokens") or {}
        meta.append(f"{_n(tk.get('input', 0))} in / {_n(tk.get('output', 0))} out")
        tr = t.get("trace") or {}
        if tr.get("duration_ms"):
            meta.append(f"{tr['duration_ms'] / 1000:.1f}s")
        if t.get("claim_id"):
            meta.append(f"claim `{t['claim_id']}`")
        L += [f"*{' · '.join(meta)}*", ""]

        r = t.get("retrieval")
        if r:
            L += [f"**Retrieval** — `{r.get('tier') or '—'}` · "
                  f"{_n(r.get('sources_pooled', 0))} pooled · "
                  f"{_n(r.get('pages_enriched', 0))} with page text", ""]
            if r.get("query"):
                L += [f"> Query: `{_trunc(r['query'], 160)}`", ""]
            for s in r.get("sources") or []:
                mark = "◆" if s.get("enriched") else "◇"
                L += [f"- {mark} [{_trunc(s.get('title') or s.get('url'), 90)}]({s.get('url')})"]
                if s.get("snippet"):
                    L += [f"  - snippet: {_trunc(s.get('snippet'), 220)}"]
                if s.get("excerpt"):
                    body = str(s["excerpt"])[:excerpt_chars].strip()
                    L += ["", "    ```", *[f"    {ln}" for ln in body.splitlines()[:14]], "    ```", ""]
            L += [""]

        c = t.get("citations") or {}
        if c.get("cited") or c.get("fabricated"):
            L += ["**Citations**", ""]
            for u in c.get("cited") or []:
                L += [f"- ✓ pooled — {u}"]
            for u in c.get("fabricated") or []:
                L += [f"- ✗ **fabricated, stripped** — {u}"]
            L += [""]

        if t.get("content"):
            L += ["**Output**", "",
                  format_act_content(t.get("act_type"), t["content"]), ""]
        if t.get("reason"):
            L += [f"*Reason: {t['reason']}*", ""]

        steps = tr.get("steps") or []
        if steps:
            L += ["<details><summary>trace steps</summary>", ""]
            L += [f"- `{s}`" for s in steps]
            L += ["", "</details>", ""]

    # ---- full pool, including sources no act ever cited
    pool = pack.get("evidence_pool") or []
    if pool:
        L += ["", "## Evidence pool", "",
              f"{len(pool)} source(s). ◆ = page text extracted, ◇ = search snippet only.", ""]
        L += _md_table(
            ["", "Turn", "By", "Title", "URL", "Text"],
            [["◆" if s.get("enriched") else "◇", s.get("turn"), s.get("harvested_by"),
              _trunc(s.get("title"), 60), _trunc(s.get("url"), 60),
              f"{_n(s.get('excerpt_chars', 0))}c" if s.get("enriched") else "—"]
             for s in pool],
            align_right={5},
        )

    claims = pack.get("claims") or []
    if claims:
        L += ["", "## Claims", ""]
        L += _md_table(
            ["ID", "Author", "Status", "Content"],
            [[f"`{c.get('claim_id', '')[:8]}`", c.get("author"), c.get("status"),
              _trunc(c.get("content"), 110)] for c in claims],
        )

    return "\n".join(L) + "\n"
