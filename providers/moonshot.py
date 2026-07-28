"""Moonshot AI (Kimi) provider adapter.

Moonshot exposes an OpenAI-compatible API at https://api.moonshot.ai/v1, so the
OpenAI SDK drives it with nothing but a base_url swap. Auth is a bearer token
from MOONSHOT_API_KEY.

Kimi models are also resold by aggregators (Perplexity lists kimi-k3 and
kimi-k2.7-code). Buying direct and buying resold are different endpoints, keys,
and prices, so both can be registered at once: provider_models is keyed on
(provider, model_id), and a debate can legitimately run one against the other.

No research(): Moonshot serves inference, not search. Its agents cite from the
pool the searching debaters filled, and cite-only-from-pool still holds.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from providers.base import (
    MAX_ATTEMPTS, BACKOFF_BASE, ModelInfo, ProviderAdapter,
    QuotaExhaustedError,
)

_BASE_URL   = "https://api.moonshot.ai/v1"
_MODELS_URL = f"{_BASE_URL}/models"

# Substrings marking a billing failure rather than a transient rate limit.
# A 429 that means "out of credit" must not be retried — it will never succeed.
_QUOTA_MARKERS = ("insufficient_quota", "exceeded_current_quota", "insufficient balance",
                  "quota", "credit", "arrears", "billing")


class MoonshotAdapter(ProviderAdapter):
    PROVIDER = "moonshot"
    KEY_ENV  = "MOONSHOT_API_KEY"

    # ------------------------------------------------------------------

    def test_key(self, key: str) -> dict:
        """Validate by listing models — cheapest call that proves the key works."""
        req = urllib.request.Request(
            _MODELS_URL, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
            return {"present": True, "valid": True, "error": None}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"present": True, "valid": False, "error": "Invalid API key"}
            return {"present": True, "valid": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"present": True, "valid": False, "error": str(e)[:60]}

    def list_models(self, key: str) -> list[ModelInfo]:
        """Enumerate via GET /v1/models (OpenAI-compatible shape).

        Whatever this returns is filed under `moonshot`, which is what makes the
        registry authoritative: the provider is recorded because this adapter,
        holding this provider's key, is what answered — never inferred from a
        model's name.
        """
        req = urllib.request.Request(
            _MODELS_URL, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as exc:
            print(f"[moonshot] list_models failed: {exc}", flush=True)
            return []

        models = []
        for m in data.get("data", []):
            model_id = m.get("id") or ""
            if not model_id:
                continue
            models.append(ModelInfo(
                model_id=model_id,
                display_name=model_id,
                endpoint_type="chat_completions",
            ))
        return models

    # ------------------------------------------------------------------

    def generate(self, key, model_id, endpoint_type, system, user, temperature,
                 max_tokens=2048):
        from openai import OpenAI, RateLimitError, APIStatusError, BadRequestError
        client = OpenAI(api_key=key, base_url=_BASE_URL)
        last_exc: Exception | None = None
        effective_temp = temperature

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = client.chat.completions.create(
                    model=model_id,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    max_tokens=max_tokens,
                    temperature=effective_temp,
                )
                usage = response.usage
                return (response.choices[0].message.content,
                        usage.prompt_tokens, usage.completion_tokens)

            except RateLimitError as e:
                # Out of credit is terminal; a genuine rate limit is worth waiting out.
                if any(k in str(e).lower() for k in _QUOTA_MARKERS):
                    raise QuotaExhaustedError("moonshot", str(e))
                last_exc = e
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE * (2 ** attempt))

            except BadRequestError as e:
                if "temperature" in str(e).lower() and effective_temp != 1.0:
                    effective_temp = 1.0
                    continue
                raise

            except APIStatusError as e:
                if e.status_code in (500, 502, 503) and attempt < MAX_ATTEMPTS - 1:
                    last_exc = e
                    time.sleep(BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

        assert last_exc is not None
        raise last_exc
