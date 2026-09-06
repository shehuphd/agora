"""Judge scoring: model-graded quality scores a run's recorded data can't
compute mechanically.

Three scores, each 0-1 with per-item verdicts kept for audit:

- citation_fidelity — for each mechanically verified citation, does the citing
  sentence claim what the quote says, about the same referent? The quote
  contract shrank this job to comparing two adjacent sentences, which is what
  makes a small judge viable.
- map_quality — the argument map judged against the turn cards: claims
  represented, statuses right, summary faithful.
- challenge_resolution — sampled challenge → defence → concession chains,
  judged on whether the concession was earned.

Judging bills per run, so nothing here runs implicitly: callers invoke
judge_run explicitly with a JudgeConfig. Every call is traced with its full
prompt, priced at call time, and temperature-0. Verdicts are strict JSON with
one repair retry. A run can be judged more than once (different configs);
each judgement is its own row in run_judgements.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from traceact import ActionTrace

from core import cost as _cost
from core.citations import sentences_citing
from core.sources import normalise_url

# Verdict calls are short; the cap covers reasoning-model overhead too.
_JUDGE_MAX_TOKENS = 400


@dataclass
class JudgeConfig:
    """One judgement's configuration. Providers are resolved by the caller
    (runs_db.resolve_model) so a judgement records the precise vendor used."""
    fidelity_model: str
    fidelity_provider: str
    map_model: str
    map_provider: str
    votes: int = 1              # 1 or 3; 3 = majority for booleans, median for scores
    max_chains: int = 5

    def label(self) -> str:
        return (
            f"fidelity={self.fidelity_model} map={self.map_model} "
            f"votes={self.votes}"
        )

    def to_dict(self) -> dict:
        return {
            "fidelity_model": self.fidelity_model,
            "fidelity_provider": self.fidelity_provider,
            "map_model": self.map_model,
            "map_provider": self.map_provider,
            "votes": self.votes,
            "max_chains": self.max_chains,
        }


@dataclass
class _Ledger:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_partial: bool = False
    errors: list = field(default_factory=list)


def _call_judge(provider: str, model: str, system: str, user: str,
                action: str, ledger: _Ledger, run_id: str) -> dict | None:
    """One traced, priced judge call returning parsed JSON, or None on failure.

    Failures never raise: a judgement with holes reports them in its payload
    rather than dying partway through a billed pass.
    """
    from providers import generate as _generate, get_key_env

    with ActionTrace.start(
        action=action, kind="model", actor="judge", project="agora",
        correlation_id=run_id, meta={"model": model, "provider": provider},
    ) as trace:
        trace.input({"system": system, "user": user})
        raw = ""
        for attempt in (1, 2):
            try:
                key = os.environ[get_key_env(provider)]
                raw, in_tok, out_tok = _generate(
                    provider, key, model, system, user, 0.0, _JUDGE_MAX_TOKENS,
                )
            except Exception as exc:
                trace.step(f"call failed: {type(exc).__name__}")
                trace.output({"error": str(exc)})
                ledger.errors.append(f"{action}: {exc}")
                return None
            ledger.calls += 1
            ledger.input_tokens += in_tok
            ledger.output_tokens += out_tok
            call_cost = _cost.cost_usd(provider, model, in_tok, out_tok)
            if call_cost is None:
                ledger.cost_partial = True
            else:
                ledger.cost_usd += call_cost
            trace.model(operation="completion", target=model, provider=provider,
                        tokens_in=in_tok, tokens_out=out_tok)
            try:
                verdict = _strip_and_parse(raw)
                trace.output({"verdict": verdict, "response_raw": raw})
                return verdict
            except Exception:
                if attempt == 1:
                    trace.step("verdict not valid JSON — one repair retry")
                    user = (
                        f"{user}\n\n---\nCORRECTION: your previous reply was not "
                        f"valid JSON:\n{raw[:500]}\nReturn ONLY the JSON object."
                    )
        trace.output({"error": "verdict never parsed", "response_raw": raw[:500]})
        ledger.errors.append(f"{action}: verdict never parsed")
        return None


def _strip_and_parse(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner)
    return json.loads(text)


def _majority(verdicts: list[bool]) -> bool:
    return sum(verdicts) * 2 > len(verdicts)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


# ---------------------------------------------------------------------------
# Run-record loading
# ---------------------------------------------------------------------------

def _load_acts(run_dir: Path) -> list[dict]:
    conn = sqlite3.connect(str(run_dir / "debate.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM acts ORDER BY turn, timestamp").fetchall()
    conn.close()
    acts = []
    for r in rows:
        d = dict(r)
        if d.get("citations"):
            try:
                d["citations"] = json.loads(d["citations"])
            except Exception:
                d["citations"] = None
        acts.append(d)
    return acts


def _turn_cards(acts: list[dict]) -> str:
    cards = []
    for a in acts:
        target = f"→{a['claim_id']}" if a.get("claim_id") else ""
        cards.append(
            f"T{a['turn']} {a['agent_role']} {a['act_type']}{target}: "
            f"{(a.get('content') or '')[:80]}"
        )
    return "\n".join(cards)


_TAG_RE = re.compile(r"</?[a-z_]+[^>]{0,80}>", re.IGNORECASE)


def _clean(text: str) -> str:
    return _TAG_RE.sub("", text or "")


# ---------------------------------------------------------------------------
# The three scores
# ---------------------------------------------------------------------------

_FIDELITY_SYSTEM = (
    "You judge citation fidelity in a debate transcript. You are given a "
    "verbatim quote from a source and the sentence that cites it. Judge one "
    "thing: does the sentence claim what the quote says, about the same "
    "referent? A figure attached to a different subject, measure, population, "
    "or direction than the quote states is unfaithful, even when the number "
    "itself is correct. Reasonable summarising is faithful. Reply with ONLY "
    'this JSON object: {"faithful": true|false, "reason": "one sentence"}'
)


def _judge_fidelity(acts: list[dict], config: JudgeConfig, ledger: _Ledger,
                    run_id: str) -> dict:
    items = []
    for a in acts:
        content = a.get("content") or ""
        cited = sentences_citing(content)
        for c in a.get("citations") or []:
            if c.get("status") != "verified" or not c.get("quote"):
                continue
            key = normalise_url(c.get("url") or "")
            sentence = next(
                (s for s, urls in cited
                 if any(normalise_url(u) == key for u in urls)),
                None,
            )
            if sentence is None and not any(
                normalise_url(u) == key for _, urls in cited for u in urls
            ):
                # Array-only citer: the URL appears in no sentence, so the
                # whole act is the citing text. Without this, models that
                # cite only through the structured array (observed live:
                # claude-sonnet-5, gpt-4.1) escape fidelity judging entirely.
                sentence = content[:1500]
            if sentence:
                items.append({
                    "act_id": a["act_id"], "turn": a["turn"], "url": c["url"],
                    "quote": c["quote"], "sentence": sentence,
                })

    verdicts = []
    for item in items:
        user = (
            f"SOURCE QUOTE (verbatim from {item['url']}):\n\"{_clean(item['quote'])}\"\n\n"
            f"CITING SENTENCE:\n\"{_clean(item['sentence'])}\"\n\n"
            "Is the sentence faithful to the quote?"
        )
        votes = []
        for _ in range(config.votes):
            v = _call_judge(config.fidelity_provider, config.fidelity_model,
                            _FIDELITY_SYSTEM, user, "judge.fidelity", ledger, run_id)
            if v is not None and isinstance(v.get("faithful"), bool):
                votes.append(v)
        if not votes:
            continue
        verdicts.append({
            "turn": item["turn"], "url": item["url"],
            "faithful": _majority([v["faithful"] for v in votes]),
            "votes": [{"faithful": v["faithful"], "reason": str(v.get("reason", ""))[:200]}
                      for v in votes],
        })

    judged = len(verdicts)
    faithful = sum(1 for v in verdicts if v["faithful"])
    return {
        "score": (faithful / judged) if judged else None,
        "eligible": len(items), "judged": judged, "faithful": faithful,
        "verdicts": verdicts,
    }


_MAP_SYSTEM = (
    "You judge the quality of a debate's argument map against the debate's "
    "own turn record. Score three things equally: coverage (every claim in "
    "the record appears in the map), status accuracy (survived, revised, and "
    "contested labels match what the record shows), and summary faithfulness "
    "(the prose describes moves that happened, and no invented ones). Reply "
    'with ONLY this JSON object: {"score": <number 0.0-1.0>, '
    '"reason": "two sentences at most"}'
)


def _judge_map(acts: list[dict], config: JudgeConfig, ledger: _Ledger,
               run_id: str) -> dict:
    map_act = next((a for a in reversed(acts) if a["act_type"] == "ARGUMENT_MAP"), None)
    if map_act is None or not (map_act.get("content") or "").strip():
        return {"score": None, "votes": [], "reason": "no argument map recorded"}
    user = (
        f"TURN RECORD:\n{_clean(_turn_cards(acts))}\n\n"
        f"ARGUMENT MAP (JSON):\n{_clean(map_act['content'])[:6000]}\n\n"
        "Score the map."
    )
    votes = []
    for _ in range(config.votes):
        v = _call_judge(config.map_provider, config.map_model,
                        _MAP_SYSTEM, user, "judge.map", ledger, run_id)
        if v is not None and isinstance(v.get("score"), (int, float)):
            votes.append({"score": max(0.0, min(1.0, float(v["score"]))),
                          "reason": str(v.get("reason", ""))[:300]})
    return {
        "score": _median([v["score"] for v in votes]) if votes else None,
        "votes": votes,
    }


_CHAIN_SYSTEM = (
    "You judge whether a debate concession was earned. You see a challenge, "
    "the defences that answered it, and the concession. Earned means the "
    "defences addressed the specific objection with new evidence or "
    "reasoning; unearned means the opposition yielded to repetition, "
    "restatement, or nothing at all. Reply with ONLY this JSON object: "
    '{"earned": true|false, "reason": "one sentence"}'
)


def _judge_chains(acts: list[dict], config: JudgeConfig, ledger: _Ledger,
                  run_id: str) -> dict:
    by_id = {a["act_id"]: a for a in acts}
    concessions = [a for a in acts if a["act_type"] == "CONCEDE"]
    chains = []
    for con in concessions[:config.max_chains]:
        ch = by_id.get(con.get("target_act_id") or "")
        if ch is None:
            ch = next(
                (a for a in reversed(acts)
                 if a["act_type"] == "CHALLENGE" and a["turn"] < con["turn"]
                 and (not con.get("claim_id") or a.get("claim_id") == con.get("claim_id"))),
                None,
            )
        if ch is None:
            continue
        defences = [
            a for a in acts
            if a["act_type"] in ("DEFEND", "REVISE")
            and ch["turn"] < a["turn"] < con["turn"]
            and (a.get("target_act_id") == ch["act_id"]
                 or (ch.get("claim_id") and a.get("claim_id") == ch.get("claim_id")))
        ]
        chains.append((ch, defences, con))

    verdicts = []
    for ch, defences, con in chains:
        defence_text = "\n\n".join(
            f"DEFENCE (turn {d['turn']}):\n{_clean(d['content'])[:1200]}" for d in defences
        ) or "(no defence recorded)"
        user = (
            f"CHALLENGE (turn {ch['turn']}):\n{_clean(ch['content'])[:1200]}\n\n"
            f"{defence_text}\n\n"
            f"CONCESSION (turn {con['turn']}):\n{_clean(con['content'])[:800]}\n\n"
            "Was the concession earned?"
        )
        votes = []
        for _ in range(config.votes):
            v = _call_judge(config.fidelity_provider, config.fidelity_model,
                            _CHAIN_SYSTEM, user, "judge.chain", ledger, run_id)
            if v is not None and isinstance(v.get("earned"), bool):
                votes.append(v)
        if not votes:
            continue
        verdicts.append({
            "concession_turn": con["turn"],
            "earned": _majority([v["earned"] for v in votes]),
            "votes": [{"earned": v["earned"], "reason": str(v.get("reason", ""))[:200]}
                      for v in votes],
        })

    judged = len(verdicts)
    earned = sum(1 for v in verdicts if v["earned"])
    return {
        "score": (earned / judged) if judged else None,
        "eligible": len(concessions), "judged": judged, "earned": earned,
        "verdicts": verdicts,
    }


# ---------------------------------------------------------------------------
# Defaults and estimate
# ---------------------------------------------------------------------------

def default_config(conn: sqlite3.Connection) -> JudgeConfig:
    """Build the default JudgeConfig from config/defaults.yaml, resolving each
    model's provider through the registry. Raises ModelNotRoutable when a
    judge model's provider key isn't set up — at invocation time, never
    mid-judgement."""
    from api.routers.settings import _load_config
    from core.runs_db import resolve_model

    block = (_load_config().get("judge") or {})
    fidelity_model = str(block.get("fidelity_model") or "gemini-flash-lite-latest")
    map_model = str(block.get("map_model") or "gpt-4.1")
    votes = int(block.get("votes") or 1)
    fid = resolve_model(conn, fidelity_model)
    mp = resolve_model(conn, map_model)
    return JudgeConfig(
        fidelity_model=fid["model_id"], fidelity_provider=fid["provider"],
        map_model=mp["model_id"], map_provider=mp["provider"],
        votes=1 if votes < 2 else 3,
    )


# Per-call token assumptions for the estimate, from the 2026-09-05 A/B run's
# recorded ledgers (means, rounded up).
_EST_FIDELITY_TOKENS = (500, 60)   # input, output per fidelity call
_EST_CHAIN_TOKENS = (1200, 60)
_EST_MAP_TOKENS = (3500, 150)


def estimate_run(run_dir: Path, config: JudgeConfig) -> dict:
    """Price a judgement before running it: counts the billable items in the
    run's record and applies per-call token assumptions at current rates.
    Unpriceable models yield a None estimate (unknown, never zero)."""
    acts = _load_acts(Path(run_dir))
    verified = sum(
        1 for a in acts for c in (a.get("citations") or [])
        if c.get("status") == "verified"
    )
    chains = min(sum(1 for a in acts if a["act_type"] == "CONCEDE"), config.max_chains)
    has_map = any(a["act_type"] == "ARGUMENT_MAP" and (a.get("content") or "").strip()
                  for a in acts)
    calls = (verified + chains) * config.votes + (config.votes if has_map else 0)

    def _price(provider, model, n, tokens):
        c = _cost.cost_usd(provider, model, tokens[0], tokens[1])
        return None if c is None else c * n

    parts = [
        _price(config.fidelity_provider, config.fidelity_model,
               verified * config.votes, _EST_FIDELITY_TOKENS),
        _price(config.fidelity_provider, config.fidelity_model,
               chains * config.votes, _EST_CHAIN_TOKENS),
        _price(config.map_provider, config.map_model,
               (config.votes if has_map else 0), _EST_MAP_TOKENS),
    ]
    total = None if any(p is None for p in parts) else round(sum(parts), 4)
    return {
        "verified_citations": verified, "chains": chains, "map": has_map,
        "calls": calls, "estimate_usd": total, "config": config.to_dict(),
        "prices_as_of": _cost.snapshot_date(),
    }


# ---------------------------------------------------------------------------
# Entry point and storage
# ---------------------------------------------------------------------------

def judge_run(run_dir: Path, run_id: str, config: JudgeConfig) -> dict:
    """Judge one closed run. Returns the full judgement payload; the caller
    decides whether to store it (store_judgement)."""
    acts = _load_acts(Path(run_dir))
    ledger = _Ledger()
    payload = {
        "run_id": run_id,
        "judged_at": datetime.now(timezone.utc).isoformat(),
        "config": config.to_dict(),
        "citation_fidelity": _judge_fidelity(acts, config, ledger, run_id),
        "map_quality": _judge_map(acts, config, ledger, run_id),
        "challenge_resolution": _judge_chains(acts, config, ledger, run_id),
    }
    payload["ledger"] = {
        "calls": ledger.calls,
        "input_tokens": ledger.input_tokens,
        "output_tokens": ledger.output_tokens,
        "cost_usd": ledger.cost_usd,
        "cost_partial": ledger.cost_partial,
        "errors": ledger.errors,
    }
    return payload


_JUDGEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS run_judgements (
    run_id    TEXT,
    judged_at TEXT,
    config    TEXT,
    scores    TEXT,
    cost_usd  REAL,
    PRIMARY KEY (run_id, judged_at)
);
"""


def store_judgement(conn: sqlite3.Connection, payload: dict) -> None:
    """Persist one judgement in the registry. Several judgements per run are
    legitimate (different configs); each keeps its own row."""
    conn.executescript(_JUDGEMENTS_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO run_judgements (run_id, judged_at, config, scores, cost_usd) "
        "VALUES (?,?,?,?,?)",
        (
            payload["run_id"], payload["judged_at"],
            json.dumps(payload["config"]),
            json.dumps({
                k: payload[k] for k in
                ("citation_fidelity", "map_quality", "challenge_resolution", "ledger")
            }),
            payload["ledger"]["cost_usd"],
        ),
    )
    conn.commit()


def list_judgements(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    conn.executescript(_JUDGEMENTS_DDL)
    rows = conn.execute(
        "SELECT judged_at, config, scores, cost_usd FROM run_judgements "
        "WHERE run_id=? ORDER BY judged_at",
        (run_id,),
    ).fetchall()
    return [
        {"judged_at": r[0], "config": json.loads(r[1]),
         "scores": json.loads(r[2]), "cost_usd": r[3]}
        for r in rows
    ]
