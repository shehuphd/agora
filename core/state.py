"""DialogueState and related dataclasses representing the full protocol state of a debate."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid
from datetime import datetime


class ActType(str, Enum):
    """All valid act types in the Agora protocol.

    Subclassing str means ActType members compare equal to their string values,
    so existing code that uses raw strings ("ASSERT", etc.) keeps working.
    """
    ASSERT               = "ASSERT"
    CHALLENGE            = "CHALLENGE"
    REVISE               = "REVISE"
    DEFEND               = "DEFEND"
    CONCEDE              = "CONCEDE"
    PROPOSE              = "PROPOSE"
    STATUS               = "STATUS"
    CLOSE                = "CLOSE"
    ARGUMENT_MAP         = "ARGUMENT_MAP"
    STEELMAN             = "STEELMAN"
    ACCEPT_STEELMAN      = "ACCEPT_STEELMAN"
    REJECT_STEELMAN      = "REJECT_STEELMAN"
    MODERATOR_INTERVENTION = "MODERATOR_INTERVENTION"


# Terminal claim statuses — claims in these states are fully resolved
TERMINAL_STATUSES = {"conceded", "survived", "contested"}

# Phases that map to act types
PHASE_MAP = {
    "ASSERT": "assert",
    "CHALLENGE": "challenge",
    "REVISE": "revise",
    "DEFEND": "defend",
    "CONCEDE": "concede",
    "PROPOSE": "propose",
    "STATUS": "status",
    "CLOSE": "closed",
    # Rapoport/steelman phases
    "STEELMAN": "steelman",
    "ACCEPT_STEELMAN": "accept_steelman",
    "REJECT_STEELMAN": "reject_steelman",
    "MODERATOR_INTERVENTION": "moderator_intervention",
}


@dataclass
class TokenUsage:
    """Tracks cumulative token consumption for one agent role."""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Claim:
    """A single asserted proposition tracked through its lifecycle."""
    claim_id: str
    run_id: str
    author: str           # agent role: 'proposition' | 'opposition'
    content: str
    status: str           # 'open' | 'challenged' | 'revised' | 'conceded' | 'survived' | 'contested'
    last_updated: str     # ISO timestamp
    steelman_attempts: int = 0  # count of REJECT_STEELMAN acts targeting this claim


@dataclass
class Act:
    """One speech act in the dialogue — the atomic unit of the protocol.

    Valid act_type values:
        ASSERT, CHALLENGE, REVISE, DEFEND, CONCEDE, PROPOSE,
        STATUS, CLOSE,
        STEELMAN, ACCEPT_STEELMAN, REJECT_STEELMAN, MODERATOR_INTERVENTION
    """
    act_id: str
    run_id: str
    turn: int
    agent: str            # nickname
    agent_role: str       # 'proposition' | 'opposition' | 'moderator' | 'synthesiser'
    act_type: str
    claim_id: Optional[str]
    target_act_id: Optional[str]
    content: str
    reason: Optional[str]
    input_tokens: int
    output_tokens: int
    model_used: str
    timestamp: str        # ISO timestamp


@dataclass
class DialogueState:
    """Complete mutable state of one debate run."""
    run_id: str
    turn: int
    phase: str                           # current grammatical phase
    claims: dict                         # claim_id -> Claim
    acts: list                           # ordered list of Act objects
    outstanding_challenges: list         # act_ids of unresolved CHALLENGE acts
    next_agent: str                      # which agent moves next
    legal_acts: list                     # currently legal act types
    token_usage: dict                    # agent_role -> TokenUsage
    debate_title: str
    topic: str
    config: dict                         # full merged config for this run
    created_at: str
    closed_at: Optional[str]
    closure_reason: Optional[str]
    steelman_mode: bool = False          # True = Rapoport mode (require steelman)


def legal_acts_for(state: DialogueState) -> list:
    """Derive the list of legal act types from current protocol phase."""
    from core.grammar import LEGAL_TRANSITIONS_STANDARD, LEGAL_TRANSITIONS_RAPOPORT
    table = LEGAL_TRANSITIONS_RAPOPORT if getattr(state, "steelman_mode", False) else LEGAL_TRANSITIONS_STANDARD
    base = table.get(state.phase.upper(), [])
    extras = [a for a in ("STATUS", "CLOSE", "MODERATOR_INTERVENTION") if a not in base]
    return base + extras


def apply_act(state: DialogueState, act: Act) -> None:
    """Mutate DialogueState after a validated act is applied to the protocol.

    Handles standard acts plus Rapoport/steelman variants:
        STEELMAN              — no claim status change
        ACCEPT_STEELMAN       — no claim status change; unblocks challenge
        REJECT_STEELMAN       — increments claim.steelman_attempts
        MODERATOR_INTERVENTION — log only; phase advances
    """
    state.acts.append(act)
    now = datetime.utcnow().isoformat()

    # Update token usage for the acting agent
    usage = state.token_usage.get(act.agent_role)
    if usage:
        usage.input_tokens += act.input_tokens
        usage.output_tokens += act.output_tokens

    if act.act_type == "ASSERT":
        # Always assign a fresh server-side UUID so models can never collide or
        # reuse an existing claim_id intentionally.
        claim_id = str(uuid.uuid4())
        act.claim_id = claim_id
        state.claims[claim_id] = Claim(
            claim_id=claim_id,
            run_id=state.run_id,
            author=act.agent_role,
            content=act.content,
            status="open",
            last_updated=now,
        )
        state.phase = "assert"
        state.turn += 1

    elif act.act_type == "STEELMAN":
        # Opposition restates the proposition's claim — no status change
        state.phase = "steelman"
        state.turn += 1

    elif act.act_type == "ACCEPT_STEELMAN":
        # Proposition accepts the restatement; challenge may now proceed
        state.phase = "accept_steelman"
        state.turn += 1

    elif act.act_type == "REJECT_STEELMAN":
        # Proposition rejects the restatement; increment steelman_attempts on targeted claim
        if act.claim_id and act.claim_id in state.claims:
            state.claims[act.claim_id].steelman_attempts += 1
            state.claims[act.claim_id].last_updated = now
        state.phase = "reject_steelman"
        state.turn += 1

    elif act.act_type == "CHALLENGE":
        # Mark the targeted claim as challenged; add this act to outstanding list
        if act.claim_id and act.claim_id in state.claims:
            state.claims[act.claim_id].status = "challenged"
            state.claims[act.claim_id].last_updated = now
        state.outstanding_challenges.append(act.act_id)
        state.phase = "challenge"
        state.turn += 1

    elif act.act_type == "REVISE":
        # Update claim content with revision; remove resolved challenge from outstanding
        if act.claim_id and act.claim_id in state.claims:
            state.claims[act.claim_id].content = act.content
            state.claims[act.claim_id].status = "revised"
            state.claims[act.claim_id].last_updated = now
        if act.target_act_id and act.target_act_id in state.outstanding_challenges:
            state.outstanding_challenges.remove(act.target_act_id)
        state.phase = "revise"
        state.turn += 1

    elif act.act_type == "DEFEND":
        # Defending a challenge; challenge stays outstanding until resolved
        state.phase = "defend"
        state.turn += 1

    elif act.act_type == "CONCEDE":
        # Opposition concedes their challenge — the claim returns to open (not "conceded").
        # "conceded" as a claim status would mean the proposition gave up their claim, which
        # never happens here: only the opposition can CONCEDE, and they're conceding their
        # own challenge, not the proposition's assertion.
        if act.claim_id and act.claim_id in state.claims:
            state.claims[act.claim_id].status = "open"
            state.claims[act.claim_id].last_updated = now
        # Remove the resolved challenge. Use target_act_id first; fall back to the most
        # recent outstanding challenge targeting this claim if the LLM omitted it.
        if act.target_act_id and act.target_act_id in state.outstanding_challenges:
            state.outstanding_challenges.remove(act.target_act_id)
        elif state.outstanding_challenges:
            for ch_id in reversed(list(state.outstanding_challenges)):
                ch_act = next((a for a in state.acts if a.act_id == ch_id and (not act.claim_id or a.claim_id == act.claim_id)), None)
                if ch_act:
                    state.outstanding_challenges.remove(ch_id)
                    break
        state.phase = "concede"
        state.turn += 1

    elif act.act_type == "PROPOSE":
        # Propose closure; Moderator will decide whether to CLOSE
        state.phase = "propose"
        state.turn += 1

    elif act.act_type == "STATUS":
        # Moderator summary — advance turn without changing phase
        state.turn += 1

    elif act.act_type == "MODERATOR_INTERVENTION":
        # Log only; phase advances to reflect intervention
        state.phase = "moderator_intervention"
        state.turn += 1

    elif act.act_type == "CLOSE":
        # Finalise debate
        state.phase = "closed"
        state.closed_at = now
        state.closure_reason = act.reason or act.content[:120]
        # Mark surviving open/revised claims
        for claim in state.claims.values():
            if claim.status in ("open", "revised"):
                claim.status = "survived"
                claim.last_updated = now
            elif claim.status == "challenged":
                claim.status = "contested"
                claim.last_updated = now

    # Refresh legal acts after every mutation
    state.legal_acts = legal_acts_for(state)
