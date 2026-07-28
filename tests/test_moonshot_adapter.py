"""Tests for the Moonshot (Kimi) adapter.

Covers registration, listing, key validation, and the classification that
decides whether a 429 is worth retrying — all without a live key.
"""
import io
import json
import urllib.error

import pytest

import providers
from providers.base import QuotaExhaustedError
from providers.moonshot import MoonshotAdapter


@pytest.fixture
def adapter():
    return MoonshotAdapter()


def _http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b""))


def _models_payload(*ids):
    return json.dumps(
        {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}
    ).encode()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ------------------------------------------------------------------
# Registration — the 3 steps the registry docstring promises
# ------------------------------------------------------------------

class TestRegistration:
    def test_listed_as_a_provider(self):
        assert "moonshot" in providers.list_provider_names()

    def test_resolves_to_the_adapter(self):
        assert isinstance(providers.get_adapter("moonshot"), MoonshotAdapter)

    def test_key_env_is_published(self):
        assert providers.get_key_env("moonshot") == "MOONSHOT_API_KEY"

    def test_agents_resolve_the_key_env_through_the_registry(self):
        """agents.base must not carry its own provider→env list."""
        from agents.base import _key_env
        assert _key_env("moonshot") == "MOONSHOT_API_KEY"

    def test_has_a_concurrency_semaphore(self):
        from providers import _SEMAPHORES
        assert "moonshot" in _SEMAPHORES


# ------------------------------------------------------------------
# list_models
# ------------------------------------------------------------------

class TestListModels:
    def test_parses_openai_shaped_listing(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: _Resp(_models_payload("kimi-k3", "moonshot-v1-8k")))
        models = adapter.list_models("sk-test")
        assert [m.model_id for m in models] == ["kimi-k3", "moonshot-v1-8k"]

    def test_ids_are_not_rewritten(self, adapter, monkeypatch):
        """Whatever the API calls a model is what must be sent back to it."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: _Resp(_models_payload("kimi-k2.7-code-highspeed")))
        assert adapter.list_models("k")[0].model_id == "kimi-k2.7-code-highspeed"

    def test_marks_models_as_chat_completions(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: _Resp(_models_payload("kimi-k3")))
        assert adapter.list_models("k")[0].endpoint_type == "chat_completions"

    def test_skips_entries_without_an_id(self, adapter, monkeypatch):
        payload = json.dumps({"data": [{"object": "model"}, {"id": "kimi-k3"}]}).encode()
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(payload))
        assert [m.model_id for m in adapter.list_models("k")] == ["kimi-k3"]

    def test_network_failure_returns_empty_not_raise(self, adapter, monkeypatch):
        def _boom(*a, **k):
            raise urllib.error.URLError("no route to host")
        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert adapter.list_models("k") == []


# ------------------------------------------------------------------
# test_key
# ------------------------------------------------------------------

class TestTestKey:
    def test_valid_key(self, adapter, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: _Resp(_models_payload("kimi-k3")))
        assert adapter.test_key("good") == {
            "present": True, "valid": True, "error": None}

    @pytest.mark.parametrize("code", [401, 403])
    def test_rejected_key_reports_invalid(self, adapter, monkeypatch, code):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(_http_error(code)))
        r = adapter.test_key("bad")
        assert r["valid"] is False and r["error"] == "Invalid API key"

    def test_server_error_is_not_reported_as_a_bad_key(self, adapter, monkeypatch):
        """A 500 says nothing about the key; calling it invalid would send the
        user to rotate a key that was fine."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(_http_error(500)))
        r = adapter.test_key("k")
        assert r["valid"] is False and r["error"] == "HTTP 500"


# ------------------------------------------------------------------
# Quota vs rate limit — retrying a billing failure never succeeds
# ------------------------------------------------------------------

class TestQuotaClassification:
    @pytest.mark.parametrize("message", [
        "Error: insufficient_quota, please top up",
        "Your account has insufficient balance",
        "billing: account in arrears",
        "You have exceeded_current_quota",
    ])
    def test_billing_failures_raise_quota_exhausted(self, adapter, monkeypatch, message):
        from openai import RateLimitError

        def _fail(*a, **k):
            raise RateLimitError(message, response=_FakeResp(429), body=None)
        monkeypatch.setattr(
            "openai.resources.chat.completions.Completions.create", _fail)
        with pytest.raises(QuotaExhaustedError) as exc:
            adapter.generate("k", "kimi-k3", "chat_completions", "s", "u", 0.5)
        assert exc.value.provider == "moonshot"

    def test_plain_rate_limit_is_retried_then_raised(self, adapter, monkeypatch):
        """A transient 429 must not be misfiled as out-of-credit."""
        from openai import RateLimitError
        calls = []

        def _fail(*a, **k):
            calls.append(1)
            raise RateLimitError("Rate limit reached, slow down",
                                 response=_FakeResp(429), body=None)
        monkeypatch.setattr(
            "openai.resources.chat.completions.Completions.create", _fail)
        monkeypatch.setattr("time.sleep", lambda s: None)

        with pytest.raises(RateLimitError):
            adapter.generate("k", "kimi-k3", "chat_completions", "s", "u", 0.5)
        assert len(calls) == 3      # retried, not abandoned on first failure


class _FakeResp:
    """Minimal stand-in for the httpx response the OpenAI SDK wraps."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}
        self.request = None
