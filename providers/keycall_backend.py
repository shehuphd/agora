"""KeyCall-backed provider router.

Backs providers/__init__.py's dispatch as of 2026-08-09; each cutover step
was live-verified before going into production.

Provider naming: Agora's own canonical names (openai, anthropic, google,
perplexity, moonshot) are used everywhere, including as keys into
_KEY_ENVS below. keycall normalises "google" to "gemini" internally and
reports its own Model.provider field back as "gemini" (verified live,
2026-08-09) — this module never reads that field. ModelInfo carries no
provider of its own; the caller already knows which provider it asked for
and keys the registry on that, so there is nothing to relabel.

No endpoint_type parameter anywhere: keycall resolves OpenAI's
Responses-vs-Chat-Completions routing internally per model (verified live,
2026-08-09 — see the migration plan's resolved question 1). Agora's own
endpoint_type field has been removed everywhere it's reachable from a live
call path; the provider_models.endpoint_type DB column stays in the DDL
for existing databases but nothing reads or writes it anymore.

xAI became a native keycall provider in 1.0.0 (2026-08-14, checked against
1.5.0's CHANGELOG on 2026-08-29) — it needs nothing beyond a bare
provider="xai", the same as every other provider here. It was wired in
earlier this project against 0.10.0 via keycall's openai-compatible
custom-target path (protocol/base_url), a workaround removed in the
2026-08-29 keycall 1.5.0 upgrade. Moonshot and xAI also both gained working
web_search support in 1.0.0 (Moonshot via keycall's internal $web_search
echo loop; xAI via its agentic /v1/responses route) — both live-verified
2026-08-29 with live citations from xAI and live (uncited — Moonshot
returns no citation structure) grounded answers from Moonshot.
"""
from __future__ import annotations

import asyncio

from keycall import (
    KeyCall, KeyCallError, Message, ModelCategory, TextInput,
)

from providers.base import ModelInfo, RetrievedSource

_KEY_ENVS: dict[str, str] = {
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "moonshot":   "MOONSHOT_API_KEY",
    "xai":        "XAI_API_KEY",
}

# Providers with no native web search: a call with web_search=True raises
# UNSUPPORTED_OPERATION before any network call, and research() treats that
# as "no findings" via the except branch below rather than needing an entry
# here. Empty as of keycall 1.5.0 — every provider Agora wires in supports
# search — kept as the mechanism for whichever provider loses it next.
_NO_WEB_SEARCH: set[str] = set()

# Transport read timeout for generation and research calls. keycall's 60s
# default is shorter than a reasoning model's thinking time — grok-4.6 timed
# out at 60s three attempts running on a live turn (2026-09-06), and kimi-k3
# then exceeded a 150s window twice on one mid-debate prompt (measured
# duration_ms 150,048, i.e. the transport gave up, not the model). Kept under
# the orchestrator's hard stop (runners/debate.py: _AGENT_TIMEOUT, 300s) so
# the transport gives up first with a typed, retryable error instead of the
# blunt task timeout.
_READ_TIMEOUT = 240.0

_SEMAPHORES: dict[str, asyncio.Semaphore] = {
    provider: asyncio.Semaphore(3) for provider in _KEY_ENVS
}


def list_provider_names() -> list[str]:
    return list(_KEY_ENVS)


def get_key_env(provider: str) -> str:
    try:
        return _KEY_ENVS[provider]
    except KeyError:
        raise ValueError(f"Unknown provider '{provider}'. Available: {list(_KEY_ENVS)}")


def generate(
    provider: str,
    key: str,
    model_id: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int = 2048,
) -> tuple[str, int, int]:
    """Run a single inference call. Returns (text, input_tokens, output_tokens).

    Raises KeyCallError on any provider failure — callers branch on
    error.code rather than a bespoke exception hierarchy. No tools, no
    web_search: retrieval goes through research() instead, same boundary
    the current adapters enforce.

    Agora's per-role config always sends a concrete temperature (0.7/0.4/0.3),
    but some models (all four Moonshot/kimi models, some OpenAI/Anthropic
    reasoning models) accept only one pinned sampling value and reject any
    other with MODEL_NOT_SUITABLE before the request reaches the network —
    live-verified 2026-08-09. Retrying once with temperature omitted lets
    the provider fall back to its own default, which for every model gated
    this way IS the pinned value (also live-verified) — so the retry
    succeeds rather than needing Agora to hardcode which model wants which
    number.
    """
    messages = [
        Message(role="system", content=[TextInput(text=system)]),
        Message(role="user", content=[TextInput(text=user)]),
    ]
    with KeyCall(provider=provider, api_key=key,
                 read_timeout=_READ_TIMEOUT) as client:
        try:
            result = client.generate_text(
                model=model_id, messages=messages,
                max_output_tokens=max_tokens, temperature=temperature,
            )
        except KeyCallError as e:
            if e.code.name != "MODEL_NOT_SUITABLE":
                raise
            result = client.generate_text(
                model=model_id, messages=messages, max_output_tokens=max_tokens,
            )
    return (
        result.text or "",
        result.usage.input_tokens or 0,
        result.usage.output_tokens or 0,
    )


def research(
    provider: str,
    key: str,
    model_id: str,
    query: str,
    max_tokens: int = 1500,
) -> tuple[str, list[RetrievedSource], int, int]:
    """Search the web and report findings via the provider's native search tool.

    Returns (findings_text, sources, input_tokens, output_tokens). A
    provider with no search capability (or a model keycall hasn't verified
    it for) returns no findings and no sources rather than raising.
    """
    if provider in _NO_WEB_SEARCH:
        return "", [], 0, 0

    with KeyCall(provider=provider, api_key=key,
                 read_timeout=_READ_TIMEOUT) as client:
        try:
            result = client.generate_text(
                model=model_id,
                messages=[Message(role="user", content=[TextInput(text=query)])],
                max_output_tokens=max_tokens,
                web_search=True,
            )
        except KeyCallError as e:
            if e.code.name == "UNSUPPORTED_OPERATION":
                return "", [], 0, 0
            raise

    sources = [
        RetrievedSource(
            url=c.url,
            title=c.title or "",
            snippet="",
            published="",
            excerpt=c.cited_text or "",
        )
        for c in result.citations
    ]
    return (
        result.text or "",
        sources,
        result.usage.input_tokens or 0,
        result.usage.output_tokens or 0,
    )


async def test_key_async(provider: str, key: str, timeout: float = 20.0) -> dict:
    """Validate a key by listing models. Returns {present, valid, error}.

    Only ever called with a non-empty key — the empty-key short-circuit
    lives at the settings-router call site, same as today.
    """
    def _check() -> dict:
        with KeyCall(provider=provider, api_key=key) as client:
            try:
                client.list_models(categories={ModelCategory.TEXT_GENERATION}, refresh=True)
                return {"present": True, "valid": True, "error": None}
            except KeyCallError as e:
                if e.code.name in ("INVALID_API_KEY", "PERMISSION_DENIED"):
                    return {"present": True, "valid": False, "error": e.message}
                return {"present": True, "valid": False, "error": f"{e.code.name}: {e.message}"}

    async with _SEMAPHORES[provider]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_check), timeout=timeout)
        except asyncio.TimeoutError:
            return {"present": True, "valid": False, "error": "Connection timed out"}


async def list_models_async(provider: str, key: str, timeout: float = 30.0) -> list[ModelInfo]:
    """List text-generation models available with this key."""
    def _list() -> list[ModelInfo]:
        with KeyCall(provider=provider, api_key=key) as client:
            discovery = client.list_models(categories={ModelCategory.TEXT_GENERATION}, refresh=True)
        return [
            ModelInfo(model_id=m.id, display_name=m.display_name or m.id)
            for m in discovery.models
        ]

    async with _SEMAPHORES[provider]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_list), timeout=timeout)
        except Exception:
            return []
