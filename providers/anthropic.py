"""Anthropic provider adapter."""
from __future__ import annotations
import time
from providers.base import (
    MAX_ATTEMPTS, BACKOFF_BASE, ModelInfo, ProviderAdapter,
    QuotaExhaustedError, RetrievedSource,
)

# Tracks which model IDs have rejected temperature — a model property, not per-call state.
_temperature_unsupported: set[str] = set()
# Organisation-level flag — set True if the key's org has web search disabled.
_web_search_disabled: bool = False


class AnthropicAdapter(ProviderAdapter):
    PROVIDER = "anthropic"
    KEY_ENV  = "ANTHROPIC_API_KEY"

    def test_key(self, key: str) -> dict:
        try:
            import anthropic
            anthropic.Anthropic(api_key=key).models.list(limit=1)
            return {"present": True, "valid": True, "error": None}
        except Exception as e:
            msg = str(e).lower()
            friendly = (
                "Invalid API key"
                if any(w in msg for w in ("auth", "invalid", "unauthorized", "403", "401"))
                else str(e)[:60]
            )
            return {"present": True, "valid": False, "error": friendly}

    def list_models(self, key: str) -> list[ModelInfo]:
        import anthropic
        result = []
        for m in anthropic.Anthropic(api_key=key).models.list():
            result.append(ModelInfo(
                model_id=m.id,
                display_name=getattr(m, "display_name", None) or m.id,
                endpoint_type="messages",
            ))
        return result

    def generate(self, key, model_id, endpoint_type, system, user, temperature, max_tokens=2048):
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                kwargs: dict = {
                    "model": model_id,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
                if model_id not in _temperature_unsupported:
                    kwargs["temperature"] = temperature

                response = client.messages.create(**kwargs)
                # Join every text block. Selecting one block truncates the answer
                # whenever the response is split (which citations and tool use both do).
                text = "".join(b.text for b in response.content if b.type == "text")
                return text, response.usage.input_tokens, response.usage.output_tokens

            except anthropic.BadRequestError as e:
                msg = str(e).lower()
                if any(k in msg for k in ("credit", "balance", "quota")):
                    raise QuotaExhaustedError("anthropic", str(e))
                if "temperature" in msg and model_id not in _temperature_unsupported:
                    _temperature_unsupported.add(model_id)
                    last_exc = e
                    continue
                raise

            except anthropic.PermissionDeniedError as e:
                if any(k in str(e).lower() for k in ("credit", "balance", "quota")):
                    raise QuotaExhaustedError("anthropic", str(e))
                raise

            except anthropic.RateLimitError as e:
                last_exc = e
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE * (2 ** attempt))

            except anthropic.APIStatusError as e:
                if e.status_code in (529, 500, 502, 503) and attempt < MAX_ATTEMPTS - 1:
                    last_exc = e
                    time.sleep(BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

        assert last_exc is not None
        raise last_exc

    def research(self, key, model_id, query, max_tokens=1500):
        """Search via Anthropic's server-side web_search tool.

        Sources come out of web_search_tool_result blocks — the search engine's
        own output — not out of the model's prose.
        """
        global _web_search_disabled
        import anthropic
        if _web_search_disabled:
            return "", [], 0, 0

        client = anthropic.Anthropic(api_key=key)
        try:
            resp = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system="You are a research assistant. Search the web and report "
                       "findings plainly, noting which source supports each point.",
                messages=[{"role": "user", "content": query}],
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
            )
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("credit", "balance", "quota")):
                raise QuotaExhaustedError("anthropic", str(e))
            if "web search" in msg and "not enabled" in msg:
                _web_search_disabled = True
            return "", [], 0, 0
        except Exception:
            return "", [], 0, 0

        sources: list[RetrievedSource] = []
        for block in resp.content:
            if getattr(block, "type", "") != "web_search_tool_result":
                continue
            for res in (getattr(block, "content", None) or []):
                url = getattr(res, "url", None)
                if url:
                    # Anthropic returns no description field; page_age is a date.
                    sources.append(RetrievedSource(
                        url=url,
                        title=str(getattr(res, "title", "") or "")[:200],
                        published=str(getattr(res, "page_age", "") or "")[:40],
                    ))

        findings = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return findings, sources, resp.usage.input_tokens, resp.usage.output_tokens
