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
    # Dollar cost of this act's tokens, priced by core/cost.py at the moment
    # the act was generated (recorded, never recomputed later against
    # whatever rates happen to say at read time). None when the model
    # couldn't be priced.
    cost_usd: Optional[float] = None
    # Challenge taxonomy label from the opposition's JSON ("sourcing",
    # "premise", ..., or "multi"). None for every other act type, and for
    # opposition acts recorded before this field existed.
    challenge_type: Optional[str] = None
    # Correction re-prompts this act needed before parsing and validating
    # (0 = clean first parse). None for acts recorded before this field
    # existed — unknown, not zero.
    retries: Optional[int] = None
    # Corrective re-prompts triggered by a mismatched quote (a quote the
    # cited source's stored text doesn't contain), bounded to one per act.
    # Kept separate from retries so quote drift stays measurable even when
    # the repair succeeds. None for acts recorded before this field existed.
    citation_repairs: Optional[int] = None
    # Structured citations from the agent's JSON: a list of
    # {"url", "quote", "status", "ungrounded_numbers"} dicts, with status and
    # grounding filled in by the mechanical check (core/citations.py) after
    # generation. None for acts recorded before this field existed, and for
    # act types that never cite.
    citations: Optional[list] = None


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
    chapters: list = field(default_factory=list)  # LLM chapter summaries, every K turns (see agent_settings.chapter_every)
    lapsed_challenges: list = field(default_factory=list)  # act_ids retired by lapse_stale_challenges
    # Spend on auxiliary model calls that produce no act (chapter and epoch
    # summaries), priced at call time. Kept separate so per-act costs still
    # sum to the acts' own figures; the run total adds this on top.
    aux_cost_usd: float = 0.0


# A defended challenge with no opposition follow-up for this many debater
# turns lapses: it leaves outstanding_challenges (and every prompt built from
# it) and is recorded in lapsed_challenges. Without this, challenges the
# opposition has moved on from accumulate forever and every seat's prompt
# grows linearly with them.
LAPSE_AFTER_TURNS = 10


def lapse_stale_challenges(state: "DialogueState", n: int = LAPSE_AFTER_TURNS) -> list:
    """Retire outstanding challenges the debate has moved past.

    A challenge lapses when all three hold: it has at least one DEFEND or
    REVISE responding to it, the opposition has not followed up on that
    thread since the last such defence, and at least `n` debater turns have
    passed since that defence. Undefended challenges never lapse — silence
    from the proposition keeps a challenge alive indefinitely.

    A follow-up must target the thread itself: the challenge's own act, or
    one of its defences. Merely sharing the claim does not count — in a
    single-claim debate every opposition act shares the claim, which made the
    original claim-wide test keep all 50 challenges alive across 100 turns
    (measured 2026-09-05, zero lapses). Defences still match by claim as a
    fallback because the proposition sometimes omits target_act_id.

    Deterministic on purpose: the condition is objective, so it runs as
    protocol rather than as a moderator judgement call. Returns the act_ids
    that lapsed this call.
    """
    lapsed: list = []
    for ch_id in list(state.outstanding_challenges):
        ch = next((a for a in state.acts if a.act_id == ch_id), None)
        if ch is None:
            continue
        defences = [
            a for a in state.acts
            if a.act_type in ("DEFEND", "REVISE")
            and a.turn > ch.turn
            and (a.target_act_id == ch_id or (ch.claim_id and a.claim_id == ch.claim_id))
        ]
        if not defences:
            continue
        last_defence_turn = max(a.turn for a in defences)
        thread_ids = {ch_id} | {a.act_id for a in defences}
        followed_up = any(
            a.agent_role == "opposition"
            and a.turn > last_defence_turn
            and a.target_act_id in thread_ids
            for a in state.acts
        )
        if followed_up:
            continue
        if state.turn - last_defence_turn >= n:
            state.outstanding_challenges.remove(ch_id)
            state.lapsed_challenges.append(ch_id)
            lapsed.append(ch_id)
    return lapsed


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
        # Moderator summary — auxiliary participants don't consume debate turns.
        # max_turns budgets debater moves only; before this, every STATUS act
        # silently halved the effective turn budget.
        pass

    elif act.act_type == "MODERATOR_INTERVENTION":
        # Log only; phase advances to reflect intervention. No turn consumed.
        state.phase = "moderator_intervention"

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
