"""Tests for providers/keycall_backend.py — the sole provider-dispatch router,
wired into agents/base.py and runners/debate.py since the KeyCall migration.
These tests mock keycall.KeyCall itself
(constructor and result-type signatures verified live against the currently
installed keycall version, not guessed — checked again at 1.5.0, 2026-08-29)
so they run offline, with no network calls and no live keys.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from keycall import ErrorCode, KeyCallError

import providers.keycall_backend as backend


# ------------------------------------------------------------------
# Fakes standing in for keycall's own return types. Field names and
# nesting match keycall 0.8.0's InvocationResult/Usage/Citation/
# ModelDiscovery/Model dataclasses, inspected live before writing these.
# ------------------------------------------------------------------

@dataclass
class _FakeUsage:
    input_tokens: int | None = 10
    output_tokens: int | None = 5


@dataclass
class _FakeCitation:
    url: str
    title: str | None = None
    cited_text: str | None = None


@dataclass
class _FakeResult:
    text: str | None = "hello"
    usage: _FakeUsage = field(default_factory=_FakeUsage)
    citations: tuple = ()


@dataclass
class _FakeModel:
    id: str
    display_name: str | None = None
    provider: str = "openai"


@dataclass
class _FakeDiscovery:
    models: tuple = ()


class _FakeClient:
    """Stands in for a `with KeyCall(...) as client:` block."""

    def __init__(self, generate_result=None, generate_exc=None,
                 models_result=None, models_exc=None):
        self._generate_result = generate_result
        self._generate_exc = generate_exc
        self._models_result = models_result
        self._models_exc = models_exc
        self.generate_calls: list[dict] = []
        self.list_calls: int = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def generate_text(self, **kwargs):
        self.generate_calls.append(kwargs)
        if self._generate_exc:
            raise self._generate_exc
        return self._generate_result

    def list_models(self, **kwargs):
        self.list_calls += 1
        if self._models_exc:
            raise self._models_exc
        return self._models_result


def _patch_keycall(monkeypatch, client: _FakeClient):
    """KeyCall(provider=, api_key=) -> the given fake, regardless of args."""
    monkeypatch.setattr(backend, "KeyCall", lambda **kwargs: client)


# ------------------------------------------------------------------
# list_provider_names / get_key_env — no keycall involved, pure mapping
# ------------------------------------------------------------------

class TestProviderNames:
    def test_lists_all_six_agora_providers(self):
        assert set(backend.list_provider_names()) == {
            "anthropic", "openai", "google", "perplexity", "moonshot", "xai"}

    def test_key_env_matches_existing_adapter_env_vars(self):
        # Must match providers/*.py's KEY_ENV character for character — settings.py's .env
        # writes depend on this staying stable across the migration.
        assert backend.get_key_env("openai") == "OPENAI_API_KEY"
        assert backend.get_key_env("anthropic") == "ANTHROPIC_API_KEY"
        assert backend.get_key_env("google") == "GOOGLE_API_KEY"
        assert backend.get_key_env("perplexity") == "PERPLEXITY_API_KEY"
        assert backend.get_key_env("moonshot") == "MOONSHOT_API_KEY"
        assert backend.get_key_env("xai") == "XAI_API_KEY"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            backend.get_key_env("not-a-known-provider")


# ------------------------------------------------------------------
# Client construction — every provider, xai included, is a bare
# provider="..." call since keycall 1.0.0 added xai natively. No
# protocol/base_url special-casing anywhere in this module anymore.
# ------------------------------------------------------------------

class TestClientConstruction:
    @pytest.mark.parametrize("provider", [
        "anthropic", "openai", "google", "perplexity", "moonshot", "xai",
    ])
    def test_every_provider_gets_a_bare_call_no_protocol_or_base_url(self, monkeypatch, provider):
        seen = {}
        fake = _FakeClient(generate_result=_FakeResult())
        monkeypatch.setattr(backend, "KeyCall", lambda **kw: seen.update(kw) or fake)
        backend.generate(provider, "sk-test", "some-model", "sys", "usr", 0.5)
        # read_timeout raised above keycall's 60s default: grok-4.6 exceeded
        # 60s and kimi-k3 exceeded 150s on live turns (2026-09-06); sized
        # under the orchestrator's 300s hard stop so the transport's typed,
        # retryable error fires first.
        assert seen == {"provider": provider, "api_key": "sk-test",
                        "read_timeout": 240.0}


# ------------------------------------------------------------------
# generate()
# ------------------------------------------------------------------

class TestGenerate:
    def test_returns_text_and_token_counts(self, monkeypatch):
        client = _FakeClient(generate_result=_FakeResult(
            text="the answer", usage=_FakeUsage(input_tokens=100, output_tokens=42)))
        _patch_keycall(monkeypatch, client)

        text, in_tok, out_tok = backend.generate(
            "openai", "sk-test", "gpt-4.1-mini", "be brief", "hello", 0.7)

        assert text == "the answer"
        assert in_tok == 100
        assert out_tok == 42

    def test_none_text_becomes_empty_string(self, monkeypatch):
        """A reasoning model can burn its whole budget on hidden tokens and
        return no visible text — callers must get '', never None."""
        _patch_keycall(monkeypatch, _FakeClient(generate_result=_FakeResult(text=None)))
        text, _, _ = backend.generate("openai", "k", "o4-mini", "s", "u", 1.0)
        assert text == ""

    def test_none_usage_fields_become_zero(self, monkeypatch):
        """A provider that doesn't report usage must not crash token accounting."""
        _patch_keycall(monkeypatch, _FakeClient(
            generate_result=_FakeResult(usage=_FakeUsage(input_tokens=None, output_tokens=None))))
        _, in_tok, out_tok = backend.generate("openai", "k", "m", "s", "u", 0.5)
        assert in_tok == 0 and out_tok == 0

    def test_sends_system_and_user_as_separate_messages(self, monkeypatch):
        client = _FakeClient(generate_result=_FakeResult())
        _patch_keycall(monkeypatch, client)
        backend.generate("openai", "k", "m", "SYSTEM TEXT", "USER TEXT", 0.5)
        call = client.generate_calls[0]
        roles = [m.role for m in call["messages"]]
        assert roles == ["system", "user"]

    def test_no_tools_or_web_search_passed(self, monkeypatch):
        """generate() is tool-free by contract — retrieval goes through research()."""
        client = _FakeClient(generate_result=_FakeResult())
        _patch_keycall(monkeypatch, client)
        backend.generate("openai", "k", "m", "s", "u", 0.5)
        call = client.generate_calls[0]
        assert "web_search" not in call or call.get("web_search") is False
        assert "tools" not in call or not call.get("tools")

    def test_keycall_error_propagates(self, monkeypatch):
        exc = KeyCallError("out of credit", code=ErrorCode.PERMISSION_DENIED,
                            provider="openai", retryable=False)
        _patch_keycall(monkeypatch, _FakeClient(generate_exc=exc))
        with pytest.raises(KeyCallError) as excinfo:
            backend.generate("openai", "k", "m", "s", "u", 0.5)
        assert excinfo.value.code is ErrorCode.PERMISSION_DENIED

    def test_rate_limited_error_carries_retry_after(self, monkeypatch):
        exc = KeyCallError("slow down", code=ErrorCode.RATE_LIMITED,
                            retryable=True, retry_after=12.0)
        _patch_keycall(monkeypatch, _FakeClient(generate_exc=exc))
        with pytest.raises(KeyCallError) as excinfo:
            backend.generate("openai", "k", "m", "s", "u", 0.5)
        assert excinfo.value.retryable is True
        assert excinfo.value.retry_after == 12.0

    def test_model_not_suitable_retries_once_without_temperature(self, monkeypatch):
        """Agora's config always sends a concrete temperature. A model with a
        pinned sampling value (all four Moonshot models, some OpenAI/Anthropic
        reasoning models) rejects it — the retry must omit temperature/top_p
        entirely rather than surface the error, since the provider's own
        default is the pinned value for every model gated this way."""
        class _RetryOnceClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def generate_text(self, **kwargs):
                self.generate_calls.append(kwargs)
                self.calls += 1
                if self.calls == 1:
                    raise KeyCallError(
                        "model 'kimi-k3' accepts only temperature=1, not 0.7",
                        code=ErrorCode.MODEL_NOT_SUITABLE)
                return _FakeResult(text="ok")

        client = _RetryOnceClient()
        _patch_keycall(monkeypatch, client)

        text, _, _ = backend.generate("moonshot", "k", "kimi-k3", "s", "u", 0.7)

        assert text == "ok"
        assert client.calls == 2
        assert "temperature" in client.generate_calls[0]
        assert "temperature" not in client.generate_calls[1]

    def test_other_model_not_suitable_causes_are_not_retried_forever(self, monkeypatch):
        """A second MODEL_NOT_SUITABLE (e.g. the model truly can't serve
        this request at all) must surface, not loop or swallow silently."""
        exc = KeyCallError("model cannot serve this request",
                            code=ErrorCode.MODEL_NOT_SUITABLE)
        client = _FakeClient(generate_exc=exc)
        _patch_keycall(monkeypatch, client)

        with pytest.raises(KeyCallError):
            backend.generate("openai", "k", "m", "s", "u", 0.5)
        # Two attempts and no more: the original call, and the one retry without
        # sampling params — never an unbounded loop.
        assert len(client.generate_calls) == 2


# ------------------------------------------------------------------
# research()
# ------------------------------------------------------------------

class TestResearch:
    def test_no_provider_short_circuits_by_default(self):
        """Every provider Agora wires in supports web_search natively as of
        keycall 1.0.0 — _NO_WEB_SEARCH is the empty-set default, not a
        per-provider exclusion list."""
        assert backend._NO_WEB_SEARCH == set()

    @pytest.mark.parametrize("provider,model", [
        ("moonshot", "kimi-k3"), ("xai", "grok-4.5"),
    ])
    def test_moonshot_and_xai_now_reach_keycall_with_web_search(self, monkeypatch, provider, model):
        """Both gained working web_search support in keycall 1.0.0 (2026-08-14,
        live-verified 2026-08-29) — neither short-circuits anymore."""
        client = _FakeClient(generate_result=_FakeResult(text="grounded answer"))
        _patch_keycall(monkeypatch, client)
        text, sources, in_tok, out_tok = backend.research(provider, "k", model, "some query")
        assert text == "grounded answer"
        assert client.generate_calls[0]["web_search"] is True

    def test_short_circuit_mechanism_still_works_for_a_future_no_search_provider(self, monkeypatch):
        """_NO_WEB_SEARCH being empty today doesn't mean the mechanism is
        dead — prove it still short-circuits if a provider is ever added."""
        called = []
        monkeypatch.setattr(backend, "KeyCall", lambda **kw: called.append(kw) or _FakeClient())
        monkeypatch.setattr(backend, "_NO_WEB_SEARCH", {"moonshot"})
        text, sources, in_tok, out_tok = backend.research(
            "moonshot", "k", "kimi-k3", "some query")
        assert (text, sources, in_tok, out_tok) == ("", [], 0, 0)
        assert called == []

    def test_maps_citations_to_retrieved_source(self, monkeypatch):
        result = _FakeResult(
            text="findings here",
            usage=_FakeUsage(input_tokens=20, output_tokens=15),
            citations=(
                _FakeCitation(url="https://a.example", title="A", cited_text="quoted text"),
                _FakeCitation(url="https://b.example", title=None, cited_text=None),
            ),
        )
        _patch_keycall(monkeypatch, _FakeClient(generate_result=result))

        text, sources, in_tok, out_tok = backend.research(
            "perplexity", "k", "sonar", "query")

        assert text == "findings here"
        assert in_tok == 20 and out_tok == 15
        assert len(sources) == 2
        assert sources[0].url == "https://a.example"
        assert sources[0].title == "A"
        assert sources[0].excerpt == "quoted text"
        assert sources[0].snippet == ""
        assert sources[0].published == ""
        # A citation with no title/cited_text degrades to empty strings, not None.
        assert sources[1].title == ""
        assert sources[1].excerpt == ""

    def test_unsupported_operation_returns_empty_not_raise(self, monkeypatch):
        """A model keycall hasn't verified web_search for must not break the run."""
        exc = KeyCallError("no search here", code=ErrorCode.UNSUPPORTED_OPERATION)
        _patch_keycall(monkeypatch, _FakeClient(generate_exc=exc))
        text, sources, in_tok, out_tok = backend.research(
            "openai", "k", "some-model", "query")
        assert (text, sources, in_tok, out_tok) == ("", [], 0, 0)

    def test_other_keycall_errors_propagate(self, monkeypatch):
        """Only UNSUPPORTED_OPERATION is swallowed — a quota failure must still surface."""
        exc = KeyCallError("no credit", code=ErrorCode.PERMISSION_DENIED)
        _patch_keycall(monkeypatch, _FakeClient(generate_exc=exc))
        with pytest.raises(KeyCallError):
            backend.research("openai", "k", "gpt-4.1-mini", "query")

    def test_web_search_flag_is_set(self, monkeypatch):
        client = _FakeClient(generate_result=_FakeResult())
        _patch_keycall(monkeypatch, client)
        backend.research("openai", "k", "gpt-4.1-mini", "query")
        assert client.generate_calls[0]["web_search"] is True


# ------------------------------------------------------------------
# test_key_async()
# ------------------------------------------------------------------

class TestTestKeyAsync:
    def test_valid_key(self, monkeypatch):
        _patch_keycall(monkeypatch, _FakeClient(models_result=_FakeDiscovery()))
        result = asyncio.run(backend.test_key_async("openai", "sk-good"))
        assert result == {"present": True, "valid": True, "error": None}

    def test_invalid_key(self, monkeypatch):
        exc = KeyCallError("bad key", code=ErrorCode.INVALID_API_KEY)
        _patch_keycall(monkeypatch, _FakeClient(models_exc=exc))
        result = asyncio.run(backend.test_key_async("openai", "sk-bad"))
        assert result["present"] is True
        assert result["valid"] is False
        assert result["error"] == "bad key"

    def test_permission_denied_reports_as_invalid_not_a_crash(self, monkeypatch):
        """HTTP 402 / billing hold (keycall 0.8.0's PERMISSION_DENIED mapping)
        must surface as a clean invalid-key result, not an unhandled error."""
        exc = KeyCallError("billing hold", code=ErrorCode.PERMISSION_DENIED)
        _patch_keycall(monkeypatch, _FakeClient(models_exc=exc))
        result = asyncio.run(backend.test_key_async("openai", "sk-unfunded"))
        assert result["valid"] is False
        assert "billing hold" in result["error"]

    def test_transient_error_still_reports_invalid_with_code_named(self, monkeypatch):
        exc = KeyCallError("gateway timeout", code=ErrorCode.PROVIDER_UNAVAILABLE)
        _patch_keycall(monkeypatch, _FakeClient(models_exc=exc))
        result = asyncio.run(backend.test_key_async("openai", "sk-x"))
        assert result["valid"] is False
        assert "PROVIDER_UNAVAILABLE" in result["error"]

    def test_timeout_reports_cleanly(self, monkeypatch):
        """A live timeout, not a mocked one: the fake client's list_models()
        blocks longer than the timeout given to test_key_async(), so this
        exercises the actual asyncio.wait_for path rather than simulating it."""
        class _SlowClient(_FakeClient):
            def list_models(self, **kwargs):
                import time
                time.sleep(0.3)
                return _FakeDiscovery()

        _patch_keycall(monkeypatch, _SlowClient())
        result = asyncio.run(backend.test_key_async("openai", "sk-x", timeout=0.02))
        assert result == {"present": True, "valid": False, "error": "Connection timed out"}


# ------------------------------------------------------------------
# list_models_async()
# ------------------------------------------------------------------

class TestListModelsAsync:
    def test_returns_model_info_list(self, monkeypatch):
        discovery = _FakeDiscovery(models=(
            _FakeModel(id="gpt-4.1-mini", display_name="GPT-4.1 Mini"),
            _FakeModel(id="gpt-4.1", display_name=None),
        ))
        _patch_keycall(monkeypatch, _FakeClient(models_result=discovery))

        models = asyncio.run(backend.list_models_async("openai", "sk-good"))

        assert len(models) == 2
        assert models[0].model_id == "gpt-4.1-mini"
        assert models[0].display_name == "GPT-4.1 Mini"
        # display_name falls back to the model id when keycall reports none.
        assert models[1].display_name == "gpt-4.1"

    def test_any_failure_returns_empty_list_not_raise(self, monkeypatch):
        """Settings page model-refresh must never crash the page on a bad key."""
        exc = KeyCallError("bad key", code=ErrorCode.INVALID_API_KEY)
        _patch_keycall(monkeypatch, _FakeClient(models_exc=exc))
        models = asyncio.run(backend.list_models_async("openai", "sk-bad"))
        assert models == []

    def test_google_alias_works_transparently(self, monkeypatch):
        """provider='google' must not require any special-casing here —
        keycall accepts it as an alias; ModelInfo carries no provider field
        to get confused about (verified live against keycall 0.8.0)."""
        discovery = _FakeDiscovery(models=(_FakeModel(id="gemini-2.5-flash", provider="gemini"),))
        _patch_keycall(monkeypatch, _FakeClient(models_result=discovery))
        models = asyncio.run(backend.list_models_async("google", "sk-good"))
        assert models[0].model_id == "gemini-2.5-flash"
