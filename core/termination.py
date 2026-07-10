"""Termination checks — hard and soft stop conditions for debate closure."""
from __future__ import annotations
import hashlib
import re
from datetime import datetime
from core.state import DialogueState, TERMINAL_STATUSES


def check_termination(state: DialogueState, config: dict) -> tuple[bool, str | None]:
    """Return (should_close, reason) after evaluating all hard and soft stop conditions."""
    proto = config.get("protocol", {})

    # --- Hard stops ---

    # Hard stop: maximum turn count reached
    max_turns = proto.get("max_turns", 8)
    if state.turn >= max_turns:
        return True, f"max_turns reached ({state.turn}/{max_turns})"

    # Hard stop: wall-clock time budget exceeded
    max_minutes = proto.get("max_time_minutes", 15)
    elapsed = _elapsed_minutes(state.created_at)
    if elapsed >= max_minutes:
        return True, f"max_time_minutes reached ({elapsed:.1f}/{max_minutes} min)"

    # Hard stop: aggregate token budget exhausted
    token_budget = proto.get("token_budget", 100_000)
    total_tokens = sum(
        u.input_tokens + u.output_tokens
        for u in state.token_usage.values()
    )
    if total_tokens >= token_budget:
        return True, f"token_budget exhausted ({total_tokens}/{token_budget} tokens)"

    # --- Soft stops ---

    # Soft stop: too few challenges issued across the whole debate.
    # Only evaluated when there are no outstanding challenges (proposition has
    # had its chance to respond) and enough turns have elapsed to be meaningful.
    min_challenges = proto.get("min_challenges", 3)
    total_challenges = sum(1 for a in state.acts if a.act_type == "CHALLENGE")
    min_turns_for_check = max(8, min_challenges * 4)
    if (state.turn >= min_turns_for_check
            and not state.outstanding_challenges
            and total_challenges < min_challenges):
        return True, (
            f"challenge_rate_floor: {total_challenges} challenge(s) issued "
            f"over {state.turn} turns — minimum required: {min_challenges}"
        )

    # Soft stop: PROPOSE was immediately followed by CONCEDE
    if len(state.acts) >= 2:
        last_two = state.acts[-2:]
        if last_two[0].act_type == "PROPOSE" and last_two[1].act_type == "CONCEDE":
            return True, "PROPOSE met with CONCEDE — mutual agreement to close"

    # Soft stop: repetition — normalised claim text matches a previously revised claim
    repetition_tolerance = proto.get("repetition_tolerance", 1)
    if _detect_repetition(state, repetition_tolerance):
        return True, "repetition detected — claim content matches a previously challenged and revised claim"

    return False, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _elapsed_minutes(created_at: str) -> float:
    """Compute minutes elapsed since debate creation timestamp."""
    try:
        start = datetime.fromisoformat(created_at)
        delta = datetime.utcnow() - start
        return delta.total_seconds() / 60.0
    except Exception:
        return 0.0


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, and strip punctuation for comparison."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _content_hash(text: str) -> str:
    """Return SHA-256 hex digest of normalised claim text."""
    return hashlib.sha256(_normalise(text).encode()).hexdigest()


def _detect_repetition(state: DialogueState, tolerance: int) -> bool:
    """Return True if any open claim duplicates a previously revised claim's content."""
    # Collect hashes of claims that have been through challenge+revise
    revised_hashes: set[str] = set()
    open_hashes: list[str] = []

    for claim in state.claims.values():
        h = _content_hash(claim.content)
        if claim.status == "revised":
            revised_hashes.add(h)
        elif claim.status in ("open", "challenged"):
            open_hashes.append(h)

    # Count how many open claims collide with previously revised content
    collisions = sum(1 for h in open_hashes if h in revised_hashes)
    return collisions >= tolerance
