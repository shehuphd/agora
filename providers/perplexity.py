"""Perplexity provider adapter.

Perplexity exposes an OpenAI-compatible chat completions endpoint at
https://api.perplexity.ai/chat/completions (no /v1/ prefix).
Model listing uses GET /v1/models directly — the OpenAI SDK hits /models
(no /v1/) when given the bare base URL, returning 404.
Only Perplexity-owned models (owned_by == "perplexity") are kept.
"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from providers.base import (
    MAX_ATTEMPTS, BACKOFF_BASE, ModelInfo, ProviderAdapter,
    QuotaExhaustedError, RetrievedSource,
)

_BASE_URL      = "https://api.perplexity.ai"
_MODELS_URL    = "https://api.perplexity.ai/v1/models"


class PerplexityAdapter(ProviderAdapter):
    PROVIDER = "perplexity"
    KEY_ENV  = "PERPLEXITY_API_KEY"

    def test_key(self, key: str) -> dict:
        # Validate by hitting /v1/models directly — 200 means the key is accepted.
        req = urllib.request.Request(_MODELS_URL, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
            return {"present": True, "valid": True, "error": None}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"present": True, "valid": False, "error": "Invalid API key"}
            return {"present": True, "valid": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"present": True, "valid": False, "error": str(e)}

    def list_models(self, key: str) -> list[ModelInfo]:
        # Use a direct request — the OpenAI SDK with the bare base URL hits /models (404).
        req = urllib.request.Request(_MODELS_URL, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception:
            return []
        result = []
        for m in data.get("data", []):
            owned_by = (m.get("owned_by") or "").lower()
            if owned_by != "perplexity":
                continue
            raw_id   = m.get("id", "")
            model_id = raw_id.split("/", 1)[-1] if "/" in raw_id else raw_id
            if model_id:
                result.append(ModelInfo(model_id=model_id, display_name=model_id, endpoint_type="chat_completions"))
        return result

    def generate(self, key, model_id, endpoint_type, system, user, temperature, max_tokens=2048):
        from openai import OpenAI, RateLimitError, APIStatusError
        client = OpenAI(api_key=key, base_url=_BASE_URL)
        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = client.chat.completions.create(
                    model=model_id,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                usage = response.usage
                return response.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens

            except RateLimitError as e:
                if any(k in str(e).lower() for k in ("insufficient_quota", "credit")):
                    raise QuotaExhaustedError("perplexity", str(e))
                last_exc = e
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE * (2 ** attempt))

            except APIStatusError as e:
                if e.status_code in (500, 502, 503) and attempt < MAX_ATTEMPTS - 1:
                    last_exc = e
                    time.sleep(BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

        assert last_exc is not None
        raise last_exc

    def research(self, key, model_id, query, max_tokens=1500):
        """Search via Perplexity's always-on retrieval.

        Sonar models search on every call and return the documents they used in
        a `search_results` field (older deployments: `citations`, bare URLs).
        Harvested via direct HTTP because the OpenAI SDK's typed response drops
        Perplexity's extra fields.
        """
        payload = json.dumps({
            "model": model_id,
            "messages": [
                {"role": "system", "content": "You are a research assistant. Search the web "
                                              "and report findings plainly, noting which "
                                              "source supports each point."},
                {"role": "user", "content": query},
            ],
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{_BASE_URL}/chat/completions", data=payload, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (402, 429) and "quota" in (e.read().decode("utf-8", "ignore")).lower():
                raise QuotaExhaustedError("perplexity", f"HTTP {e.code}")
            return "", [], 0, 0
        except Exception:
            return "", [], 0, 0

        sources, seen = [], set()
        for res in (data.get("search_results") or []):
            url = res.get("url")
            if url and url not in seen:
                seen.add(url)
                sources.append(RetrievedSource(
                    url=url,
                    title=str(res.get("title") or "")[:200],
                    published=str(res.get("date") or "")[:40],
                ))
        for url in (data.get("citations") or []):
            if url and url not in seen:
                seen.add(url)
                sources.append(RetrievedSource(url=url))

        findings = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return findings, sources, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
