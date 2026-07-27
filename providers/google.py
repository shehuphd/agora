"""Google Gemini provider adapter.

Key validation and model listing use direct urllib calls to the Google AI REST API.
The google-genai SDK's sync client deadlocks when called from asyncio.to_thread()
(the SDK detects the running event loop and switches to async mode, which cannot be
awaited from a thread). Direct HTTP avoids this entirely.
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

_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_google_search_unsupported: set[str] = set()  # models that don't support google_search grounding

# Gemini returns citations as Vertex redirect links rather than publisher URLs.
_REDIRECT_HOST = "vertexaisearch.cloud.google.com"


def _resolve_redirects(urls: list[str], timeout: float = 8.0) -> dict[str, str]:
    """Map Gemini redirect URLs to their real publisher URLs.

    Resolved concurrently because each is a network round trip. A URL that fails
    to resolve keeps its redirect form: it is still a genuine search result, so
    dropping it would wrongly shrink the evidence pool.
    """
    from concurrent.futures import ThreadPoolExecutor
    import httpx

    def _one(u: str) -> tuple[str, str]:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as c:
                # Only the final URL matters — a ranged GET avoids downloading
                # whole page bodies just to follow the redirect chain.
                return u, str(c.get(u, headers={"Range": "bytes=0-0"}).url)
        except Exception:
            return u, u

    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return dict(pool.map(_one, urls))


class GoogleAdapter(ProviderAdapter):
    PROVIDER = "google"
    KEY_ENV  = "GOOGLE_API_KEY"

    def test_key(self, key: str) -> dict:
        req = urllib.request.Request(f"{_MODELS_URL}?key={key}&pageSize=1")
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
            return {"present": True, "valid": True, "error": None}
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                return {"present": True, "valid": False, "error": "Invalid API key"}
            return {"present": True, "valid": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"present": True, "valid": False, "error": str(e)}

    def list_models(self, key: str) -> list[ModelInfo]:
        # Fragments in a model_id that indicate it is not a general text-generation model.
        # The API lists image, audio, TTS, live, embedding, and robotics models under the
        # same endpoint — but they are useless (or broken) for debate generation.
        _SKIP = ("image", "audio", "tts", "live", "translate", "robotics",
                 "computer-use", "embedding", "omni", "customtools")
        result = []
        page_token = None
        while True:
            url = f"{_MODELS_URL}?key={key}&pageSize=100"
            if page_token:
                url += f"&pageToken={page_token}"
            req = urllib.request.Request(url)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read())
            except Exception:
                break
            for m in data.get("models", []):
                raw_id = m.get("name", "")
                model_id = raw_id.removeprefix("models/")
                if not model_id.startswith("gemini"):
                    continue
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                if any(frag in model_id for frag in _SKIP):
                    continue
                result.append(ModelInfo(
                    model_id=model_id,
                    display_name=m.get("displayName") or model_id,
                    endpoint_type="generate_content",
                ))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return result

    def generate(self, key, model_id, endpoint_type, system, user, temperature, max_tokens=2048):
        from google import genai
        from google.genai import types
        from google.genai import errors as gerrors
        client = genai.Client(api_key=key)
        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                config_kwargs: dict = {
                    "system_instruction": system or None,
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                }
                response = client.models.generate_content(
                    model=model_id,
                    contents=user,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                usage = response.usage_metadata
                return response.text, usage.prompt_token_count or 0, usage.candidates_token_count or 0

            except gerrors.ClientError as e:
                msg = str(e).lower()
                status = getattr(e, "status", None)
                if any(k in msg for k in ("billing", "quota", "exhausted", "exceeded")):
                    raise QuotaExhaustedError("google", str(e))
                # Permanent 404 (model not found) — do not retry.
                if status == 404 and any(k in msg for k in ("not found", "does not exist", "invalid model")):
                    raise
                if status in (429, 404) and attempt < MAX_ATTEMPTS - 1:
                    last_exc = e
                    time.sleep(BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

            except gerrors.ServerError as e:
                last_exc = e
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

        assert last_exc is not None
        raise last_exc

    def research(self, key, model_id, query, max_tokens=1500):
        """Search via the Interactions API with the google_search tool.

        The legacy generate_content + grounding_metadata path returns no grounding
        chunks on current models; Interactions returns url_citation annotations.
        Those carry Vertex redirect links, which are resolved to publisher URLs here.
        """
        from google import genai
        if model_id in _google_search_unsupported:
            return "", [], 0, 0

        try:
            client = genai.Client(api_key=key)
            interaction = client.interactions.create(
                model=model_id,
                input="Search the web and report findings plainly, noting which "
                      f"source supports each point.\n\n{query}",
                tools=[{"type": "google_search"}],
            )
            data = interaction.model_dump()
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in ("billing", "quota", "exhausted", "exceeded")):
                raise QuotaExhaustedError("google", str(e))
            if "tool" in msg or "not supported" in msg:
                _google_search_unsupported.add(model_id)
            return "", [], 0, 0

        # Harvest url_citation annotations off the model_output step.
        raw: dict[str, str] = {}   # url -> title
        for step in (data.get("steps") or []):
            if step.get("type") != "model_output":
                continue
            for part in (step.get("content") or []):
                for ann in (part.get("annotations") or []):
                    url = ann.get("url")
                    if url and url not in raw:
                        raw[url] = str(ann.get("title") or "")[:200]

        redirects = [u for u in raw if _REDIRECT_HOST in u]
        resolved = _resolve_redirects(redirects)

        sources, seen = [], set()
        for url, title in raw.items():
            final = resolved.get(url, url)
            if final in seen:
                continue
            seen.add(final)
            sources.append(RetrievedSource(url=final, title=title))

        usage = data.get("usage") or {}
        # Thought tokens are billed but reported separately from output tokens.
        output_tokens = (usage.get("total_output_tokens") or 0) + (usage.get("total_thought_tokens") or 0)
        return (
            getattr(interaction, "output_text", "") or "",
            sources,
            usage.get("total_input_tokens") or 0,
            output_tokens,
        )
