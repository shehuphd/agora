"""Pydantic schema for validating the structure of defaults.yaml."""
from pydantic import BaseModel
from typing import Optional


class PropositionAgentConfig(BaseModel):
    model: Optional[str] = None
    temperature: float = 0.7
    max_claims: int = 5


class OppositionAgentConfig(BaseModel):
    model: Optional[str] = None
    temperature: float = 0.4
    aggression: float = 0.8


class ModeratorAgentConfig(BaseModel):
    model: Optional[str] = None
    temperature: float = 0.3
    auto_generate_title: bool = True


class SynthesiserAgentConfig(BaseModel):
    model: Optional[str] = None
    temperature: float = 0.3


class AgentsConfig(BaseModel):
    proposition: PropositionAgentConfig = PropositionAgentConfig()
    opposition: OppositionAgentConfig = OppositionAgentConfig()
    moderator: ModeratorAgentConfig = ModeratorAgentConfig()
    synthesiser: SynthesiserAgentConfig = SynthesiserAgentConfig()


class OpenAIConfig(BaseModel):
    responses_mode: str = "auto"


class ProtocolConfig(BaseModel):
    min_challenges: int = 2
    min_concessions: int = 1
    max_turns: int = 8
    max_time_minutes: int = 15
    token_budget: int = 100000
    repetition_tolerance: int = 1
    require_full_resolution: bool = False


class OutputConfig(BaseModel):
    generate_markdown: bool = True
    store_argument_trace: bool = True
    score_final_output: bool = True


class ProvidersConfig(BaseModel):
    model_order: list[str] = ["openai", "anthropic", "google", "perplexity"]


class AgoraConfig(BaseModel):
    protocol: ProtocolConfig = ProtocolConfig()
    agents: AgentsConfig = AgentsConfig()
    output: OutputConfig = OutputConfig()
    openai: OpenAIConfig = OpenAIConfig()
    providers: ProvidersConfig = ProvidersConfig()
