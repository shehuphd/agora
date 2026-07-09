"""Typed run-time configuration for a single debate session.

DebateRunConfig is built from the API's Pydantic DebateConfig at the router
boundary and passed through the system from there. No dict key-path lookups;
all fields are typed and IDE-navigable.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json


@dataclass(frozen=True)
class AgentRunConfig:
    model: str
    temperature: float
    nickname: str
    aggression: float = 0.8  # only meaningful for opposition


@dataclass(frozen=True)
class ProtocolRunConfig:
    max_turns: int = 15
    max_time_minutes: int = 30
    token_budget: int = 40_000
    min_challenges: int = 3
    min_concessions: int = 1
    repetition_tolerance: int = 1


@dataclass(frozen=True)
class DebateRunConfig:
    topic: str
    debate_title: str
    proposition: AgentRunConfig
    opposition: AgentRunConfig
    moderator: AgentRunConfig
    protocol: ProtocolRunConfig = field(default_factory=ProtocolRunConfig)
    steelman_mode: bool = False

    @classmethod
    def from_api(cls, c: object) -> "DebateRunConfig":
        """Convert a Pydantic DebateConfig (from api/models.py) to a run config."""
        return cls(
            topic=c.topic,
            debate_title=getattr(c, "debate_title", None) or f"Debate: {c.topic[:60]}",
            proposition=AgentRunConfig(
                model=getattr(c, "proposition_model", "claude-sonnet-4-6"),
                temperature=getattr(c, "temperature_proposition", 0.7),
                nickname=getattr(c, "proposition_nickname", "Thesis"),
            ),
            opposition=AgentRunConfig(
                model=getattr(c, "opposition_model", "gpt-4o"),
                temperature=getattr(c, "temperature_opposition", 0.4),
                nickname=getattr(c, "opposition_nickname", "Antithesis"),
                aggression=getattr(c, "aggression", 0.8),
            ),
            moderator=AgentRunConfig(
                model=getattr(c, "moderator_model", "claude-opus-4-8"),
                temperature=getattr(c, "temperature_moderator", 0.3),
                nickname="Moderator",
            ),
            protocol=ProtocolRunConfig(
                max_turns=getattr(c, "max_turns", 8),
                max_time_minutes=getattr(c, "max_time_minutes", 15),
                token_budget=getattr(c, "token_budget", 100_000),
                min_challenges=getattr(c, "min_challenges", 2),
                min_concessions=getattr(c, "min_concessions", 1),
                repetition_tolerance=getattr(c, "repetition_tolerance", 1),
            ),
            steelman_mode=getattr(c, "require_steelman", False),
        )

    def to_termination_dict(self) -> dict:
        """Return the dict shape that core/termination.py expects."""
        return {
            "protocol": {
                "max_turns": self.protocol.max_turns,
                "max_time_minutes": self.protocol.max_time_minutes,
                "token_budget": self.protocol.token_budget,
                "min_challenges": self.protocol.min_challenges,
                "repetition_tolerance": self.protocol.repetition_tolerance,
            }
        }

    def to_json(self) -> str:
        """Serialise for storage in the sessions.config column."""
        return json.dumps({
            "topic": self.topic,
            "debate_title": self.debate_title,
            "proposition_model": self.proposition.model,
            "proposition_nickname": self.proposition.nickname,
            "temperature_proposition": self.proposition.temperature,
            "opposition_model": self.opposition.model,
            "opposition_nickname": self.opposition.nickname,
            "temperature_opposition": self.opposition.temperature,
            "aggression": self.opposition.aggression,
            "moderator_model": self.moderator.model,
            "temperature_moderator": self.moderator.temperature,
            "max_turns": self.protocol.max_turns,
            "max_time_minutes": self.protocol.max_time_minutes,
            "token_budget": self.protocol.token_budget,
            "min_challenges": self.protocol.min_challenges,
            "min_concessions": self.protocol.min_concessions,
            "repetition_tolerance": self.protocol.repetition_tolerance,
            "steelman_mode": self.steelman_mode,
            "protocol": {
                "max_turns": self.protocol.max_turns,
                "max_time_minutes": self.protocol.max_time_minutes,
                "token_budget": self.protocol.token_budget,
                "min_challenges": self.protocol.min_challenges,
                "repetition_tolerance": self.protocol.repetition_tolerance,
            },
        })
