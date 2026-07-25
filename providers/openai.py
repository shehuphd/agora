"""OpenAI provider adapter.

Routes to Chat Completions (/v1/chat/completions) or the Responses API (/v1/responses)
with automatic fallback. The active strategy is governed by the module-level
`_endpoint_mode` variable, which settings.py updates when configuration is saved.

Endpoint modes:
  "auto"             — prefer Responses for gpt-5.6-* and models stored with endpoint_type
                       "responses"; fall back to Chat Completions (and vice versa) if the
                       primary endpoint rejects the model.
  "prefer_responses" — try Responses API first for all OpenAI models; fall back to Chat.
  "chat_only"        — never use the Responses API; always use Chat Completions.
"""
from __future__ import annotations
import time
from providers.base import MAX_ATTEMPTS, BACKOFF_BASE, ModelInfo, ProviderAdapter, QuotaExhaustedError

# Per-process caches — shared across all adapter calls in this process.
_temperature_unsupported: set[str] = set()
_json_object_unsupported: set[str] = set()  # models that don't accept response_format=json_object
_responses_unsupported: set[str] = set()
_web_search_unsupported: set[str] = set()  # models that don't support web_search via Responses API

# Responses API endpoint mode — updated via set_endpoint_mode() when config is loaded/saved.
_endpoint_mode: str = "auto"

# Model prefixes that should default to the Responses API when mode is "auto".
_RESPONSES_PREFIXES = ("gpt-5.6",)

# Prefixes that identify a model as chat-inference-capable (for list_models filtering).
_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chat-")

# Substrings in a model ID that disqualify it from the chat picker regardless of prefix.
_NONCHAT_FRAGMENTS = (
    "-audio", "-image", "-realtime", "-tts", "-transcribe",
    "-whisper", "-diarize", "-search-preview", "-search-api",
    "-instruct", "-codex",
)


def set_endpoint_mode(mode: str) -> None:
    """Update the global endpoint strategy. Called by providers.configure() on config load/save."""
    global _endpoint_mode
    if mode not in ("auto", "prefer_responses", "chat_only"):
        mode = "auto"
    _endpoint_mode = mode


def _default_endpoint_type(model_id: str) -> str:
    return "responses" if any(model_id.startswith(p) for p in _RESPONSES_PREFIXES) else "chat_completions"


class OpenAIAdapter(ProviderAdapter):
    PROVIDER = "openai"
    KEY_ENV  = "OPENAI_API_KEY"

    def test_key(self, key: str) -> dict:
        try:
            from openai import OpenAI
            OpenAI(api_key=key).models.list()
            return {"present": True, "valid": True, "error": None}
        except Exception as e:
            msg = str(e).lower()
            friendly = (
                "Invalid API key"
                if any(w in msg for w in ("auth", "invalid", "unauthorized", "incorrect", "403", "401"))
                else str(e)[:60]
            )
            return {"present": True, "valid": False, "error": friendly}

    def list_models(self, key: str) -> list[ModelInfo]:
        from openai import OpenAI
        result = []
        for m in OpenAI(api_key=key).models.list():
            if not any(m.id.startswith(p) for p in _CHAT_PREFIXES):
                continue
            if any(frag in m.id for frag in _NONCHAT_FRAGMENTS):
                continue
            result.append(ModelInfo(
                model_id=m.id,
                display_name=m.id,
                endpoint_type=_default_endpoint_type(m.id),
            ))
        return result

    def generate(self, key, model_id, endpoint_type, system, user, temperature, max_tokens=2048, enable_web_search=False):
        from openai import OpenAI, RateLimitError, APIStatusError
        client = OpenAI(api_key=key)

        # Determine the ordered list of endpoints to attempt.
        if _endpoint_mode == "chat_only":
            ep_order = ["chat"]
        elif _endpoint_mode == "prefer_responses":
            ep_order = ["responses", "chat"] if model_id not in _responses_unsupported else ["chat"]
        else:  # "auto"
            use_responses_first = (
                endpoint_type == "responses"
                or any(model_id.startswith(p) for p in _RESPONSES_PREFIXES)
            ) and model_id not in _responses_unsupported
            ep_order = ["responses", "chat"] if use_responses_first else ["chat", "responses"]

        last_exc: Exception | None = None
        ep_idx = 0
        attempt = 0

        while ep_idx < len(ep_order) and attempt < MAX_ATTEMPTS:
            ep = ep_order[ep_idx]
            try:
                if ep == "responses":
                    return self._via_responses(client, model_id, system, user, max_tokens, enable_web_search)
                else:
                    return self._via_chat_completions(client, model_id, system, user, temperature, max_tokens)

            except RateLimitError as e:
                if "insufficient_quota" in str(e).lower():
                    raise QuotaExhaustedError("openai", str(e))
                last_exc = e
                attempt += 1
                if attempt < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))

            except APIStatusError as e:
                msg = str(e).lower()

                # Temperature not accepted by this model — strip it and retry same endpoint.
                if e.status_code == 400 and "temperature" in msg and model_id not in _temperature_unsupported:
                    _temperature_unsupported.add(model_id)
                    last_exc = e
                    attempt += 1
                    continue

                # response_format not accepted by this model — strip it and retry same endpoint.
                if e.status_code == 400 and "response_format" in msg and model_id not in _json_object_unsupported:
                    _json_object_unsupported.add(model_id)
                    last_exc = e
                    attempt += 1
                    continue

                # Endpoint doesn't support this model — fall back to the other endpoint.
                if e.status_code in (400, 404) and ep_idx < len(ep_order) - 1:
                    if ep == "responses":
                        _responses_unsupported.add(model_id)
                    last_exc = e
                    ep_idx += 1
                    attempt = 0
                    continue

                # Transient server-side errors — retry same endpoint.
                if e.status_code in (403, 500, 502, 503) and attempt < MAX_ATTEMPTS - 1:
                    last_exc = e
                    attempt += 1
                    time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
                else:
                    raise

        assert last_exc is not None
        raise last_exc

    def _via_chat_completions(self, client, model_id, system, user, temperature, max_tokens):
        kwargs: dict = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if model_id not in _temperature_unsupported:
            kwargs["temperature"] = temperature
        messages_text = " ".join(m.get("content", "") for m in kwargs["messages"])
        if model_id not in _json_object_unsupported and "json" in messages_text.lower():
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        usage = response.usage
        return response.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens

    def _via_responses(self, client, model_id, system, user, max_tokens, enable_web_search=False):
        from openai import APIStatusError
        kwargs: dict = {
            "model": model_id,
            "instructions": system or None,
            "input": user,
            "max_output_tokens": max_tokens,
        }
        use_search = enable_web_search and model_id not in _web_search_unsupported
        if use_search:
            kwargs["tools"] = [{"type": "web_search"}]
        try:
            response = client.responses.create(**kwargs)
        except APIStatusError as e:
            if use_search and e.status_code == 400:
                # Model doesn't support web_search — remember and retry without it.
                _web_search_unsupported.add(model_id)
                del kwargs["tools"]
                response = client.responses.create(**kwargs)
            else:
                raise
        usage = response.usage
        return response.output_text, usage.input_tokens, usage.output_tokens
