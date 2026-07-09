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
    proposition_model: str = "claude-sonnet-4-6"
    opposition_model: str = "gpt-4o"
    moderator_model: str = "claude-opus-4-8"
    proposition_nickname: str = "Thesis"
    opposition_nickname: str = "Antithesis"
    max_turns: int = 8
    max_time_minutes: int = 15
    token_budget: int = 100000
    min_challenges: int = 2
    min_concessions: int = 1
    temperature_proposition: float = 0.7
    temperature_opposition: float = 0.4
    temperature_moderator: float = 0.3
    aggression: float = 0.8


class ActResponse(BaseModel):
    """Serialised Act for API responses."""
    act_id: str
    session_id: str
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
    session_id: str
    debate_title: str
    topic: str
    status: str
    created_at: str
    acts: list
    claims: list
