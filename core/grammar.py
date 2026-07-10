"""Dialogue grammar — defines legal act transitions and validates moves against protocol."""
from __future__ import annotations
from core.state import DialogueState

# Phase-to-phase transition table defining legal successor acts (standard mode)
LEGAL_TRANSITIONS_STANDARD: dict[str, list[str]] = {
    "INIT":      ["ASSERT"],            # opening state — only Proposition's first ASSERT is valid
    "ASSERT":    ["CHALLENGE", "CONCEDE"],
    # ASSERT included here so Proposition can open new fronts while challenges remain outstanding.
    # Without it, an opposition that never concedes can bottleneck the whole debate on one claim.
    "CHALLENGE": ["REVISE", "DEFEND", "CONCEDE", "ASSERT"],
    "REVISE":    ["CHALLENGE", "CONCEDE", "PROPOSE"],
    "DEFEND":    ["CHALLENGE", "CONCEDE"],
    "CONCEDE":   ["PROPOSE", "ASSERT"],
    "PROPOSE":   [],
}

# Rapoport mode — Opposition must steelman before challenging
LEGAL_TRANSITIONS_RAPOPORT: dict[str, list[str]] = {
    "INIT":             ["ASSERT"],     # opening state
    "ASSERT":           ["STEELMAN"],
    "STEELMAN":         ["ACCEPT_STEELMAN", "REJECT_STEELMAN"],
    "ACCEPT_STEELMAN":  ["CHALLENGE", "CONCEDE"],
    "REJECT_STEELMAN":  ["STEELMAN"],
    "CHALLENGE":        ["REVISE", "DEFEND", "CONCEDE", "ASSERT"],
    "REVISE":           ["CHALLENGE", "CONCEDE", "PROPOSE"],
    "DEFEND":           ["CHALLENGE", "CONCEDE"],
    "CONCEDE":          ["PROPOSE", "ASSERT"],
    "PROPOSE":          [],
}

# Backwards-compatible alias
LEGAL_TRANSITIONS = LEGAL_TRANSITIONS_STANDARD

# Acts that are always legal regardless of phase (Moderator-only in practice)
ALWAYS_LEGAL = {"STATUS", "CLOSE", "MODERATOR_INTERVENTION"}

# Map phase names (from state.phase) to their uppercase act-type key
_PHASE_TO_ACT = {v.lower(): k for k, v in {
    "ASSERT": "assert",
    "CHALLENGE": "challenge",
    "REVISE": "revise",
    "DEFEND": "defend",
    "CONCEDE": "concede",
    "PROPOSE": "propose",
    "STATUS": "status",
    "CLOSE": "closed",
    "STEELMAN": "steelman",
    "ACCEPT_STEELMAN": "accept_steelman",
    "REJECT_STEELMAN": "reject_steelman",
}.items()}


def validate_act(state: DialogueState, act_type: str) -> None:
    """Raise ValueError if act_type is illegal given current dialogue state.

    STATUS, CLOSE, and MODERATOR_INTERVENTION bypass phase checking — they are
    always permitted. When state.steelman_mode is True the Rapoport transition
    table is used; otherwise the standard table applies.
    """
    act_type = act_type.upper()

    # Debate is over — no acts allowed except always-legal ones
    if state.phase == "closed" and act_type not in ALWAYS_LEGAL:
        raise ValueError(
            f"Debate is closed. Act '{act_type}' is not permitted after closure."
        )

    # Always-legal acts skip transition checks
    if act_type in ALWAYS_LEGAL:
        return

    # Pick the right transition table
    steelman_mode = getattr(state, "steelman_mode", False)
    table = LEGAL_TRANSITIONS_RAPOPORT if steelman_mode else LEGAL_TRANSITIONS_STANDARD

    # Determine which acts are currently legal from the current phase
    current_phase_act = state.phase.upper()
    legal = table.get(current_phase_act, [])

    if act_type not in legal:
        raise ValueError(
            f"Illegal act '{act_type}' in phase '{state.phase}'. "
            f"Legal acts are: {legal or ['(none — awaiting Moderator CLOSE)']}"
        )
