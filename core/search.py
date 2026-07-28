"""Neutral web-search layer — search decoupled from LLM vendors.

Vendor-bundled search bills the SERP through the model's context window at
model token rates (one observed GPT-4.1 retrieval: 18.6k tokens, of which we
kept ~600). Here the model only writes the query; execution goes to a search
service at flat per-query cost, outside the token economy entirely.

Fallback chain, best to worst:
  1. SearXNG   — self-hosted, zero marginal cost, no vendor
  2. Brave     — flat ~$1/1k queries (BRAVE_API_KEY)
  3. Serper    — flat ~$1/1k queries (SERPER_API_KEY)
  4. (caller)  — provider research(), token-billed; the caller warns the user

Every raw response is appended to <run_dir>/search_log.jsonl — the evidence
lockfile that makes a run auditable and, later, replayable.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from providers.base import RetrievedSource

_SEARXNG_DEFAULT = "http://127.0.0.1:8888"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_SERPER_URL = "https://google.serper.dev/search"

# SearXNG availability is probed once and cached; a dead local instance must
# not add a connect-timeout to every retrieval of every debate.
_searxng_ok: bool | None = None
_searxng_checked_at: float = 0.0
_SEARXNG_PROBE_TTL = 300.0
_probe_lock = threading.Lock()


def _searxng_url() -> str:
    return (os.environ.get("SEARXNG_URL") or _SEARXNG_DEFAULT).rstrip("/")


def searxng_available(force: bool = False) -> bool:
    global _searxng_ok, _searxng_checked_at
    with _probe_lock:
        if not force and _searxng_ok is not None and time.time() - _searxng_checked_at < _SEARXNG_PROBE_TTL:
            return _searxng_ok
        import httpx
        try:
            r = httpx.get(f"{_searxng_url()}/search", params={"q": "ping", "format": "json"}, timeout=3.0)
            _searxng_ok = r.status_code == 200
        except Exception:
            _searxng_ok = False
        _searxng_checked_at = time.time()
        return _searxng_ok


def brave_available() -> bool:
    return bool((os.environ.get("BRAVE_API_KEY") or "").strip())


def serper_available() -> bool:
    return bool((os.environ.get("SERPER_API_KEY") or "").strip())


def active_tier() -> str:
    """Which rung of the chain will answer the next search.

    'searxng' | 'brave' | 'serper' | 'provider' — 'provider' means
    token-billed vendor search, the thing the user should be warned about.
    """
    if searxng_available():
        return "searxng"
    if brave_available():
        return "brave"
    if serper_available():
        return "serper"
    return "provider"


def _search_searxng(query: str, max_results: int) -> tuple[list[RetrievedSource], dict]:
    import httpx
    r = httpx.get(
        f"{_searxng_url()}/search",
        params={"q": query, "format": "json"},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    sources = [
        RetrievedSource(
            url=res.get("url") or "",
            title=str(res.get("title") or "")[:200],
            snippet=str(res.get("content") or "")[:300],
            published=str(res.get("publishedDate") or "")[:40],
        )
        for res in (data.get("results") or [])[:max_results]
        if res.get("url")
    ]
    return sources, data


def _search_brave(query: str, max_results: int) -> tuple[list[RetrievedSource], dict]:
    import httpx
    r = httpx.get(
        _BRAVE_URL,
        headers={
            "X-Subscription-Token": os.environ["BRAVE_API_KEY"].strip(),
            "Accept": "application/json",
        },
        params={"q": query, "count": min(max_results, 20)},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    sources = [
        RetrievedSource(
            url=res.get("url") or "",
            title=str(res.get("title") or "")[:200],
            snippet=str(res.get("description") or "")[:300],
            published=str(res.get("age") or res.get("page_age") or "")[:40],
        )
        for res in (data.get("web", {}).get("results") or [])[:max_results]
        if res.get("url")
    ]
    return sources, data


def _search_serper(query: str, max_results: int) -> tuple[list[RetrievedSource], dict]:
    import httpx
    r = httpx.post(
        _SERPER_URL,
        headers={"X-API-KEY": os.environ["SERPER_API_KEY"].strip(),
                 "Content-Type": "application/json"},
        json={"q": query, "num": max_results},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    sources = [
        RetrievedSource(
            url=res.get("link") or "",
            title=str(res.get("title") or "")[:200],
            snippet=str(res.get("snippet") or "")[:300],
            published=str(res.get("date") or "")[:40],
        )
        for res in (data.get("organic") or [])[:max_results]
        if res.get("link")
    ]
    return sources, data


def _record(run_dir: Path | None, tier: str, query: str, raw: dict, count: int) -> None:
    """Append the raw search response to the run's evidence lockfile.

    Never raises — recording must not be able to kill a retrieval.
    """
    if run_dir is None:
        return
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "tier": tier,
            "query": query,
            "result_count": count,
            "raw": raw,
        }
        path = Path(run_dir) / "search_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"[search] lockfile append failed: {exc}", flush=True)


def search(query: str, max_results: int = 8, run_dir: Path | None = None) -> tuple[str, list[RetrievedSource]]:
    """Run one neutral search. Returns (tier_used, sources).

    Returns ('none', []) when no neutral tier is available or the available
    tiers failed — the caller then falls back to token-billed provider search.
    """
    if searxng_available():
        try:
            sources, raw = _search_searxng(query, max_results)
            _record(run_dir, "searxng", query, raw, len(sources))
            return "searxng", sources
        except Exception as exc:
            print(f"[search] searxng failed: {exc}", flush=True)
    if brave_available():
        try:
            sources, raw = _search_brave(query, max_results)
            _record(run_dir, "brave", query, raw, len(sources))
            return "brave", sources
        except Exception as exc:
            print(f"[search] brave failed: {exc}", flush=True)
    if serper_available():
        try:
            sources, raw = _search_serper(query, max_results)
            _record(run_dir, "serper", query, raw, len(sources))
            return "serper", sources
        except Exception as exc:
            print(f"[search] serper failed: {exc}", flush=True)
    return "none", []


def fetch_page_markdown(url: str, max_chars: int = 1500) -> str:
    """Fetch a page and extract its readable text as markdown, capped.

    Local extraction (trafilatura): zero marginal cost, no vendor. Returns ""
    on any failure — enrichment is best-effort. The returned text is untrusted
    web content; callers must keep it inside the data envelope.
    """
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=False,
            include_comments=False,
        ) or ""
        return text[:max_chars]
    except Exception as exc:
        print(f"[search] trafilatura error for {url[:60]}: {exc}", flush=True)
        return ""


def enrich_sources(sources: list[RetrievedSource], sample_k: int = 2,
                   sample_from: int = 5, max_chars: int = 1500) -> None:
    """Fetch page content for a random sample of the top results, in place.

    Picks `sample_k` sources at random from the first `sample_from` results.
    Randomising avoids position bias (always enriching slots 0-1) while staying
    cheap. Fills `excerpt` (markdown) on each enriched source. Bounded,
    concurrent, best-effort.
    """
    import random
    from concurrent.futures import ThreadPoolExecutor
    candidates = sources[:sample_from]
    if not candidates:
        return
    targets = random.sample(candidates, min(sample_k, len(candidates)))
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        excerpts = list(pool.map(lambda s: fetch_page_markdown(s.url, max_chars), targets))
    enriched = 0
    for s, ex in zip(targets, excerpts):
        s.excerpt = ex
        if ex:
            enriched += 1
    print(f"[search] enrich: {enriched}/{len(targets)} pages yielded content", flush=True)
