"""Base types and ABC for all provider adapters."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass

# Retry settings shared by all adapters.
MAX_ATTEMPTS = 3
BACKOFF_BASE = 5  # seconds; attempt n waits BACKOFF_BASE * 2^n


@dataclass
class ModelInfo:
    """Metadata for a single model returned by list_models()."""
    model_id: str
    display_name: str
    endpoint_type: str = "default"


@dataclass
class RetrievedSource:
    """A document a search engine actually returned.

    The url here is always provider-reported. It is never parsed out of model
    prose — that distinction is the whole point of the retrieval phase.
    """
    url: str
    title: str = ""
    snippet: str = ""      # description of the content, when the provider gives one
    published: str = ""    # provider-reported age/date; evidence freshness, not description
    excerpt: str = ""      # extracted page text (markdown, capped) — untrusted web content


class QuotaExhaustedError(Exception):
    """Raised when a provider returns a billing/quota exhaustion error (not a transient rate limit)."""
    def __init__(self, provider: str, message: str):
        super().__init__(message)
        self.provider = provider


class ProviderAdapter(ABC):
    """Common interface every provider adapter must implement.

    To add a new provider: subclass this, implement the three abstract methods,
    then register an instance in providers/__init__.py. No other code needs to change.
    """
    PROVIDER: str  # Registry key, e.g. "openai"
    KEY_ENV: str   # os.environ key holding the API key, e.g. "OPENAI_API_KEY"

    @abstractmethod
    def test_key(self, key: str) -> dict:
        """Verify connectivity. Returns {"present": True, "valid": bool, "error": str|None}."""

    @abstractmethod
    def list_models(self, key: str) -> list[ModelInfo]:
        """Return all inference-capable models accessible with this key."""

    @abstractmethod
    def generate(
        self,
        key: str,
        model_id: str,
        endpoint_type: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int = 2048,
    ) -> tuple[str, int, int]:
        """Run a single inference call. Returns (text, input_tokens, output_tokens).

        Never enables tools: search here corrupts strict-JSON output, because the
        model interleaves tool blocks and conversational preamble with the JSON.
        Retrieval belongs in research().
        """

    def research(
        self,
        key: str,
        model_id: str,
        query: str,
        max_tokens: int = 1500,
    ) -> tuple[str, list[RetrievedSource], int, int]:
        """Search the web and report findings.

        Returns (findings_text, sources, input_tokens, output_tokens).

        Not abstract: a provider with no search capability inherits this no-op and
        simply contributes nothing to the pool. Its agents then cite from what the
        other agents retrieved, and cite-only-from-pool still holds — so a weak
        provider can never reopen the fabrication hole.
        """
        return "", [], 0, 0
