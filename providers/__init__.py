"""Provider registry and central inference router.

Backed by keycall (github.com/shehuphd/keycall) as of 2026-08-09. All
providers route through the single generic implementation in
providers/keycall_backend.py rather than separate hand-rolled adapter
classes.

The old providers/openai.py, anthropic.py, google.py, perplexity.py,
moonshot.py, and providers/base.py's ProviderAdapter ABC are deleted —
nothing outside this package called get_adapter()/ProviderAdapter directly
(checked before removing it), so there was no external contract to break.
"""
from __future__ import annotations
from keycall import KeyCallError  # noqa: F401 — re-exported
from providers.base import ModelInfo, RetrievedSource  # noqa: F401 — re-exported
from providers import keycall_backend as _backend

__all__ = [
    "ModelInfo", "KeyCallError", "RetrievedSource",
    "list_provider_names", "get_key_env",
    "generate", "research", "test_key_async", "list_models_async",
]

list_provider_names = _backend.list_provider_names
get_key_env = _backend.get_key_env
generate = _backend.generate
research = _backend.research
test_key_async = _backend.test_key_async
list_models_async = _backend.list_models_async
