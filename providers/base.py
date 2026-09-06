"""Shared value types for the provider layer.

Provider dispatch itself lives in providers/keycall_backend.py, backed by
keycall (github.com/shehuphd/keycall). The types here are what survive that
migration: return-value dataclasses, not an adapter interface — keycall_backend
is one generic implementation for all five providers, not five classes
behind a common ABC, so there is nothing left to abstract over.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ModelInfo:
    """Metadata for a single model returned by list_models()."""
    model_id: str
    display_name: str


@dataclass
class RetrievedSource:
    """A document a search engine returned.

    The url here is always provider-reported. It is never parsed out of model
    prose — that distinction is the whole point of the retrieval phase.
    """
    url: str
    title: str = ""
    snippet: str = ""      # description of the content, when the provider gives one
    published: str = ""    # provider-reported age/date; evidence freshness, not description
    excerpt: str = ""      # extracted page text (markdown, capped) — untrusted web content
