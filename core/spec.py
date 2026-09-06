"""Experiment specs — a stored design that expands into a run matrix.

A spec is either a factor grid:

    {"base": {"topic": "...", "token_budget": 50000},
     "factors": [{"field": "proposition_model", "levels": ["kimi-k3", "gpt-4.1"]}],
     "replicates": 3}

or an explicit row list (how CSV imports are stored, so they become
re-runnable and self-describing):

    {"rows": [{"topic": "...", ...}, ...], "replicates": 1}

expand() turns either form into batch rows deterministically: the cartesian
product of factor levels, times the replicate count, each row carrying both
its full config and its condition labels ({field: level, "replicate": n}).
"""
from __future__ import annotations

from itertools import product

# Config fields a spec may set. Also the canonical CSV column list — the
# batch import router reads it from here, so the two input paths accept the
# same fields by construction.
SPEC_FIELDS = [
    "topic",
    "debate_title",
    "proposition_model",
    "opposition_model",
    "moderator_model",
    "synth_model",
    "proposition_nickname",
    "opposition_nickname",
    "max_turns",
    "max_time_minutes",
    "token_budget",
    "temperature_proposition",
    "temperature_opposition",
    "temperature_moderator",
    "aggression",
    "min_challenges",
    "min_concessions",
    "require_steelman",
    "require_full_resolution",
]

# Refuse expansions past this size: a mistyped level list should fail the
# preview, not enqueue hundreds of billable debates.
MAX_ROWS = 200


class SpecError(ValueError):
    """The spec is malformed or expands to something unrunnable."""


def validate(spec: dict) -> None:
    """Raise SpecError with a readable reason for anything expand() can't take."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")

    replicates = spec.get("replicates", 1)
    if not isinstance(replicates, int) or replicates < 1:
        raise SpecError("replicates must be a whole number of at least 1")

    if "rows" in spec:
        rows = spec["rows"]
        if not isinstance(rows, list) or not rows:
            raise SpecError("rows must be a non-empty list")
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not str(row.get("topic", "")).strip():
                raise SpecError(f"row {i + 1} has no topic")
        return

    base = spec.get("base")
    if not isinstance(base, dict):
        raise SpecError("spec needs either a base config or an explicit rows list")
    factors = spec.get("factors", [])
    if not isinstance(factors, list):
        raise SpecError("factors must be a list")
    seen_fields: set[str] = set()
    for f in factors:
        field = (f or {}).get("field")
        levels = (f or {}).get("levels")
        if field not in SPEC_FIELDS:
            raise SpecError(f"unknown factor field {field!r} — allowed: {', '.join(SPEC_FIELDS)}")
        if field in seen_fields:
            raise SpecError(f"factor field {field!r} appears twice")
        seen_fields.add(field)
        if not isinstance(levels, list) or not levels:
            raise SpecError(f"factor {field!r} needs at least one level")
    for key in base:
        if key not in SPEC_FIELDS:
            raise SpecError(f"unknown base config field {key!r}")
    if "topic" not in seen_fields and not str(base.get("topic", "")).strip():
        raise SpecError("base config needs a topic (or make topic a factor)")

    n = replicates
    for f in factors:
        n *= len(f["levels"])
    if n > MAX_ROWS:
        raise SpecError(f"spec expands to {n} runs — the ceiling is {MAX_ROWS}")


def expand(spec: dict, replicates: int | None = None, replicate_start: int = 1) -> list[dict]:
    """Expand a validated spec into batch rows.

    Each returned dict is a config with a "_condition" key holding the
    factor levels plus the replicate number — the shape core/batch.py's
    create_job splits apart. replicates overrides the spec's own count;
    replicate_start lets an extension continue numbering where the last
    launch stopped.
    """
    validate(spec)
    reps = replicates if replicates is not None else spec.get("replicates", 1)

    if "rows" in spec:
        cells = [(dict(row), {"row": i + 1}) for i, row in enumerate(spec["rows"])]
    else:
        base = spec["base"]
        factors = spec.get("factors", [])
        level_sets = [f["levels"] for f in factors]
        fields = [f["field"] for f in factors]
        cells = []
        for combo in product(*level_sets) if factors else [()]:
            cfg = dict(base)
            condition = {}
            for field, level in zip(fields, combo):
                cfg[field] = level
                condition[field] = level
            cells.append((cfg, condition))

    rows: list[dict] = []
    for rep in range(replicate_start, replicate_start + reps):
        for cfg, condition in cells:
            row = dict(cfg)
            row["_condition"] = {**condition, "replicate": rep}
            rows.append(row)
    if len(rows) > MAX_ROWS:
        raise SpecError(f"expansion produced {len(rows)} runs — the ceiling is {MAX_ROWS}")
    return rows


def condition_key(condition: dict | None) -> str:
    """Stable grouping key for a run's condition, ignoring the replicate
    number — replicates of one condition group together."""
    if not condition:
        return ""
    items = sorted((k, str(v)) for k, v in condition.items() if k != "replicate")
    return "|".join(f"{k}={v}" for k, v in items)
