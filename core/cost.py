"""Dollar-cost pricing for model calls, backed by `rates` (Mo's pricing
registry, https://pypi.org/project/rates — installed via requirements.txt).

Additive only: any miss (rates not installed, provider/model rates doesn't
carry, ambiguous match) returns None rather than raising, so a debate and
its run pack are byte-for-byte the same shape whether or not rates is
installed. Nothing in this module is on a live debate's call path — it only
prices token counts already recorded.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

# Agora's own provider keys (providers/keycall_backend.py's _KEY_ENVS) mapped
# to rates' provider keys. Verified live against rates 0.0.4,
# 2026-09-02 — "google" needs no alias (Agora never uses keycall's internal
# "gemini" label), but moonshot does: rates lists Moonshot's models under
# "moonshotai", not "moonshot".
_RATES_PROVIDER = {
    "anthropic":  "anthropic",
    "openai":     "openai",
    "google":     "google",
    "perplexity": "perplexity",
    "moonshot":   "moonshotai",
    "xai":        "xai",
}


@dataclass(frozen=True)
class Price:
    input_mtok: float | None
    output_mtok: float | None


@lru_cache(maxsize=1)
def _registry():
    """Load the rates ledger once per process. None if rates isn't installed
    or fails to load, so every caller degrades the same way as a pricing miss.

    Prefers the stable tier: one small network request that downloads a newer
    published ledger when one exists, and by rates' own contract never raises,
    it falls back to the best local snapshot with a SyncFallbackWarning. The
    bundled tier remains the last resort for a rates version without the
    fetch parameter. api/main.py warms this cache in a startup thread so the
    network check happens at boot, never inside a debate turn.
    """
    try:
        import rates.ai
    except ImportError:
        return None
    try:
        return rates.ai.load(fetch="stable", timeout=10)
    except Exception:
        pass
    try:
        return rates.ai.load()
    except Exception:
        return None


def snapshot_date() -> str | None:
    """Date of the price snapshot in use, for the UI to state alongside any
    figure. None when rates is unavailable, matching every other miss."""
    reg = _registry()
    if reg is None:
        return None
    try:
        return str(reg.snapshot_date)
    except Exception:
        return None


@lru_cache(maxsize=1024)
def price_for(provider: str, model_id: str) -> Price | None:
    """Look up the per-million-token price for one (provider, model_id) pair.

    None on any miss: rates unavailable, provider not in rates' catalogue,
    zero matches, or an ambiguous match (more than one rates record for the
    same (provider, model) pair) — never guess between candidates.

    Memoized per (provider, model_id): the ledger is a static in-process
    snapshot, and the model-picker enrichment asks for every registry model
    on each /api/models request.
    """
    reg = _registry()
    if reg is None:
        return None
    rates_provider = _RATES_PROVIDER.get(provider)
    if rates_provider is None:
        return None
    try:
        hits = list(reg.filter(model=model_id, provider=rates_provider))
    except Exception:
        return None
    if len(hits) != 1:
        return None
    p = hits[0].price
    return Price(input_mtok=p.get("input_mtok"), output_mtok=p.get("output_mtok"))


def cost_usd(
    provider: str, model_id: str, input_tokens: int, output_tokens: int,
) -> float | None:
    """Dollar cost of one call. None if either rate is unknown."""
    price = price_for(provider, model_id)
    if price is None or price.input_mtok is None or price.output_mtok is None:
        return None
    return (input_tokens * price.input_mtok + output_tokens * price.output_mtok) / 1e6
