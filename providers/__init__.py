"""Provider registry and central inference router.

To add a new provider (e.g. DeepSeek, Meta):
  1. Create providers/<name>.py implementing ProviderAdapter.
  2. Import and register an instance in _REGISTRY below.
  3. Done — debate orchestration, agent code, and the settings router need no changes.

Architecture:
  - ProviderAdapter (base.py): ABC with test_key / list_models / generate
  - _REGISTRY: dict mapping provider name → adapter instance
  - generate(): sync router called by agents in thread-pool context
  - test_key_async() / list_models_async(): async wrappers for the settings router,
    with per-provider concurrency semaphores
"""
from __future__ import annotations
import asyncio
from providers.base import (  # noqa: F401 — re-exported
    ModelInfo, ProviderAdapter, QuotaExhaustedError, RetrievedSource,
)
from providers.anthropic import AnthropicAdapter
from providers.openai import OpenAIAdapter
from providers.google import GoogleAdapter
from providers.perplexity import PerplexityAdapter
from providers.moonshot import MoonshotAdapter

__all__ = [
    "ModelInfo", "QuotaExhaustedError", "RetrievedSource",
    "get_adapter", "list_provider_names", "get_key_env",
    "generate", "research", "test_key_async", "list_models_async",
    "configure",
]

_REGISTRY: dict[str, ProviderAdapter] = {
    "anthropic":  AnthropicAdapter(),
    "openai":     OpenAIAdapter(),
    "google":     GoogleAdapter(),
    "perplexity": PerplexityAdapter(),
    "moonshot":   MoonshotAdapter(),
}

# Limit concurrent outbound calls per provider (protects against accidental thundering herd
# when many debates or key-tests run simultaneously).
_SEMAPHORES: dict[str, asyncio.Semaphore] = {
    provider: asyncio.Semaphore(3) for provider in _REGISTRY
}


def list_provider_names() -> list[str]:
    return list(_REGISTRY)


def get_adapter(provider: str) -> ProviderAdapter:
    try:
        return _REGISTRY[provider]
    except KeyError:
        raise ValueError(f"Unknown provider '{provider}'. Available: {list(_REGISTRY)}")


def get_key_env(provider: str) -> str:
    """Return the os.environ key name for this provider's API key."""
    return get_adapter(provider).KEY_ENV


def generate(
    provider: str,
    key: str,
    model_id: str,
    endpoint_type: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int = 2048,
) -> tuple[str, int, int]:
    """Dispatch an inference call to the correct provider adapter.

    Called synchronously from agents running inside asyncio.to_thread().
    Returns (text, input_tokens, output_tokens). Tool-free by design — web
    retrieval goes through research() instead.
    """
    return get_adapter(provider).generate(key, model_id, endpoint_type, system, user, temperature, max_tokens)


def research(
    provider: str,
    key: str,
    model_id: str,
    query: str,
    max_tokens: int = 1500,
):
    """Dispatch a retrieval call to the correct provider adapter.

    Returns (findings_text, sources, input_tokens, output_tokens). Providers
    without search return no sources rather than raising.
    """
    return get_adapter(provider).research(key, model_id, query, max_tokens)


async def test_key_async(provider: str, key: str, timeout: float = 20.0) -> dict:
    """Async wrapper around adapter.test_key with semaphore and timeout."""
    async with _SEMAPHORES[provider]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(get_adapter(provider).test_key, key),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return {"present": True, "valid": False, "error": "Connection timed out"}


async def list_models_async(provider: str, key: str, timeout: float = 30.0) -> list[ModelInfo]:
    """Async wrapper around adapter.list_models with semaphore and timeout."""
    async with _SEMAPHORES[provider]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(get_adapter(provider).list_models, key),
                timeout=timeout,
            )
        except Exception:
            return []


def configure(config: dict) -> None:
    """Apply global provider settings from the loaded config dict.

    Called at startup (api/main.py) and whenever settings are saved, so provider
    behaviour stays in sync with the user's configuration without a server restart.
    """
    from providers.openai import set_endpoint_mode
    mode = config.get("openai", {}).get("responses_mode", "auto")
    set_endpoint_mode(mode)
