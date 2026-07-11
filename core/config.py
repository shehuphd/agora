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
        """Convert a Pydantic DebateConfig (from api/models.py) to a run config.

        The new-debate form sends short-name keys (prop_model, opp_model, etc.)
        while the canonical model fields use long names (proposition_model, etc.).
        Short names take priority when non-None; long-name fields provide defaults.
        """
        def _pick(short, long, fallback):
            v = getattr(c, short, None)
            return v if v is not None else getattr(c, long, fallback)

        return cls(
            topic=c.topic,
            debate_title=getattr(c, "debate_title", None) or f"Debate: {c.topic[:60]}",
            proposition=AgentRunConfig(
                model=_pick("prop_model",     "proposition_model",     "claude-sonnet-4-6"),
                temperature=_pick("prop_temperature", "temperature_proposition", 0.7),
                nickname=_pick("prop_nickname", "proposition_nickname", "Thesis"),
            ),
            opposition=AgentRunConfig(
                model=_pick("opp_model",     "opposition_model",     "gpt-4o"),
                temperature=_pick("opp_temperature", "temperature_opposition", 0.4),
                nickname=_pick("opp_nickname", "opposition_nickname", "Antithesis"),
                aggression=_pick("opp_aggression", "aggression", 0.8),
            ),
            moderator=AgentRunConfig(
                model=_pick("mod_model", "moderator_model", "claude-opus-4-8"),
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

    @classmethod
    def from_dict(cls, d: dict) -> "DebateRunConfig":
        """Reconstruct from a serialised config dict (the sessions.config column value)."""
        proto = d.get("protocol", d)  # supports both flat and nested protocol keys
        return cls(
            topic=d["topic"],
            debate_title=d.get("debate_title", f"Debate: {d['topic'][:60]}"),
            proposition=AgentRunConfig(
                model=d.get("proposition_model", "claude-sonnet-4-6"),
                temperature=d.get("temperature_proposition", 0.7),
                nickname=d.get("proposition_nickname", "Thesis"),
            ),
            opposition=AgentRunConfig(
                model=d.get("opposition_model", "gpt-4o"),
                temperature=d.get("temperature_opposition", 0.4),
                nickname=d.get("opposition_nickname", "Antithesis"),
                aggression=d.get("aggression", 0.8),
            ),
            moderator=AgentRunConfig(
                model=d.get("moderator_model", "claude-opus-4-8"),
                temperature=d.get("temperature_moderator", 0.3),
                nickname="Moderator",
            ),
            protocol=ProtocolRunConfig(
                max_turns=proto.get("max_turns", 8),
                max_time_minutes=proto.get("max_time_minutes", 15),
                token_budget=proto.get("token_budget", 100_000),
                min_challenges=proto.get("min_challenges", 2),
                min_concessions=proto.get("min_concessions", 1),
                repetition_tolerance=proto.get("repetition_tolerance", 1),
            ),
            steelman_mode=d.get("steelman_mode", False),
        )

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
