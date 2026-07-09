"""Pydantic schema for validating the structure of defaults.yaml."""
from pydantic import BaseModel
from typing import Optional


class PropositionAgentConfig(BaseModel):
    """Config for the proposition (asserting) agent."""
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.7
    max_claims: int = 5


class OppositionAgentConfig(BaseModel):
    """Config for the opposition (challenging) agent."""
    model: str = "gpt-4o"
    temperature: float = 0.4
    aggression: float = 0.8


class ModeratorAgentConfig(BaseModel):
    """Config for the moderator agent — low temperature for determinism."""
    model: str = "claude-opus-4-8"
    temperature: float = 0.3
    auto_generate_title: bool = True


class AgentsConfig(BaseModel):
    """Nested config block for all three active agent roles."""
    proposition: PropositionAgentConfig = PropositionAgentConfig()
    opposition: OppositionAgentConfig = OppositionAgentConfig()
    moderator: ModeratorAgentConfig = ModeratorAgentConfig()


class ProtocolConfig(BaseModel):
    """Governs termination conditions and debate length limits."""
    min_challenges: int = 2
    min_concessions: int = 1
    max_turns: int = 8
    max_time_minutes: int = 15
    token_budget: int = 100000
    repetition_tolerance: int = 1
    require_full_resolution: bool = False


class OutputConfig(BaseModel):
    """Controls what artefacts are written after a debate closes."""
    generate_markdown: bool = True
    store_argument_trace: bool = True
    score_final_output: bool = True


class AgoraConfig(BaseModel):
    """Root Pydantic model for the complete defaults.yaml configuration file."""
    protocol: ProtocolConfig = ProtocolConfig()
    agents: AgentsConfig = AgentsConfig()
    output: OutputConfig = OutputConfig()
