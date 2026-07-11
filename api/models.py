"""Pydantic request/response models for Agora API."""
from pydantic import BaseModel, field_validator
from typing import Optional


class DebateConfig(BaseModel):
    """Configuration submitted when starting a new debate."""
    topic: str

    @field_validator("topic")
    @classmethod
    def topic_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("topic cannot be blank")
        if len(v) > 500:
            raise ValueError("topic too long (max 500 characters)")
        return v
    debate_title: Optional[str] = None
    # Long-name canonical fields (used internally and in exports).
    proposition_model: str = "claude-sonnet-4-6"
    opposition_model: str = "gpt-4o"
    moderator_model: str = "claude-opus-4-8"
    proposition_nickname: str = "Thesis"
    opposition_nickname: str = "Antithesis"
    temperature_proposition: float = 0.7
    temperature_opposition: float = 0.4
    temperature_moderator: float = 0.3
    # Short-name aliases sent by the new-debate form (FormData uses name= attributes).
    # Pydantic would otherwise silently drop them; we accept both and prefer short if present.
    prop_model: Optional[str] = None
    opp_model: Optional[str] = None
    mod_model: Optional[str] = None
    prop_nickname: Optional[str] = None
    opp_nickname: Optional[str] = None
    prop_temperature: Optional[float] = None
    opp_temperature: Optional[float] = None
    opp_aggression: Optional[float] = None
    max_turns: int = 8
    max_time_minutes: int = 15
    token_budget: int = 100000
    min_challenges: int = 2
    min_concessions: int = 1
    aggression: float = 0.8
    require_steelman: bool = False
    require_full_resolution: bool = False
    auto_generate_title: bool = True


class ActResponse(BaseModel):
    """Serialised Act for API responses."""
    act_id: str
    run_id: str
    turn: int
    agent: str
    agent_role: str
    act_type: str
    claim_id: Optional[str]
    content: str
    reason: Optional[str]
    input_tokens: int
    output_tokens: int
    model_used: str
    timestamp: str


class DebateResponse(BaseModel):
    """Full debate state returned by GET /debates/{id}."""
    run_id: str
    debate_title: str
    topic: str
    status: str
    created_at: str
    acts: list
    claims: list
