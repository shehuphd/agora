"""Tests for model→provider resolution and retirement of unservable models.

Routing is resolved once, at debate creation, against the provider_models
registry. The registry is authoritative because its rows are written by
whichever adapter enumerated the model, using that provider's own key — the
serving provider is recorded as fact, never inferred from the model's name.

The same model id may be served by more than one provider (bought direct, and
resold by an aggregator). Those are different endpoints, keys, and prices, so
they are different selections and must stay distinguishable.
"""
import sqlite3

import pytest

from agents.base import BaseAgent
from core.runs_db import (
    ModelNotRoutable,
    init as _init,
    mark_model_unservable,
    resolve_model,
    upsert_provider_models,
)
from runners.debate import _retire_unknown_model


class _Agent(BaseAgent):
    def _build_prompt(self, state):
        return "", ""


class _KeepOpen:
    """Proxy whose close() is a no-op.

    Production opens a fresh connection per call and closes it, which is
    correct; the test shares one in-memory DB, so a real close would discard
    the fixture mid-test.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


@pytest.fixture
def registry(monkeypatch):
    """In-memory provider_models built with the production DDL, so the test
    schema cannot drift from the real one."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init(conn)
    rows = [
        ("perplexity", "kimi-k3", "kimi-k3", "chat_completions", 1),
        ("perplexity", "sonar", "sonar", "chat_completions", 1),
        ("openai", "gpt-4.1", "gpt-4.1", "responses", 1),
        ("anthropic", "claude-opus-4-6", "claude-opus-4-6", "messages", 1),
        ("perplexity", "gone-from-listing", "x", "chat_completions", 0),
    ]
    conn.executemany(
        "INSERT INTO provider_models "
        "(provider, model_id, display_name, endpoint_type, is_active, last_updated) "
        "VALUES (?,?,?,?,?,'now')", rows)
    conn.commit()
    monkeypatch.setattr("core.runs_db.connect", lambda: _KeepOpen(conn))
    return conn


# ------------------------------------------------------------------
# resolve_model — the single routing decision
# ------------------------------------------------------------------

class TestResolveModel:
    def test_returns_full_routable_address(self, registry):
        assert resolve_model(registry, "gpt-4.1") == {
            "provider": "openai", "model_id": "gpt-4.1",
            "endpoint_type": "responses",
        }

    def test_aggregator_model_resolves_to_its_reseller(self, registry):
        """kimi-k3 looks like nobody's first-party model; only the registry knows."""
        assert resolve_model(registry, "kimi-k3")["provider"] == "perplexity"

    def test_name_is_never_consulted(self, registry):
        """A model whose name implies one vendor but is registered to another
        must follow the registry. This is the whole contract."""
        registry.execute(
            "INSERT INTO provider_models (provider, model_id, display_name, "
            "endpoint_type, is_active, last_updated) VALUES "
            "('perplexity','claude-opus-4-7','x','chat_completions',1,'now')")
        registry.commit()
        assert resolve_model(registry, "claude-opus-4-7")["provider"] == "perplexity"

    def test_unregistered_model_raises(self, registry):
        with pytest.raises(ModelNotRoutable) as exc:
            resolve_model(registry, "brand-new-model")
        assert "brand-new-model" in str(exc.value)
        assert "Settings" in str(exc.value)

    def test_inactive_model_raises(self, registry):
        with pytest.raises(ModelNotRoutable):
            resolve_model(registry, "gone-from-listing")


class TestSameModelTwoProviders:
    """kimi-k3 direct from Moonshot and resold by Perplexity must coexist."""

    @pytest.fixture
    def both(self, registry):
        registry.execute(
            "INSERT INTO provider_models (provider, model_id, display_name, "
            "endpoint_type, is_active, last_updated) VALUES "
            "('moonshot','kimi-k3','kimi-k3','chat_completions',1,'now')")
        registry.commit()
        return registry

    def test_ambiguous_selection_refuses_to_pick(self, both):
        with pytest.raises(ModelNotRoutable) as exc:
            resolve_model(both, "kimi-k3")
        msg = str(exc.value)
        assert "more than one provider" in msg
        assert "moonshot" in msg and "perplexity" in msg

    def test_provider_pins_the_direct_vendor(self, both):
        assert resolve_model(both, "kimi-k3", "moonshot")["provider"] == "moonshot"

    def test_provider_pins_the_reseller(self, both):
        assert resolve_model(both, "kimi-k3", "perplexity")["provider"] == "perplexity"

    def test_both_can_be_resolved_in_one_debate(self, both):
        """The point of the feature: one model, two vendors, same run."""
        a = resolve_model(both, "kimi-k3", "moonshot")
        b = resolve_model(both, "kimi-k3", "perplexity")
        assert a["provider"] != b["provider"]
        assert a["model_id"] == b["model_id"] == "kimi-k3"

    def test_wrong_provider_for_model_raises(self, both):
        with pytest.raises(ModelNotRoutable) as exc:
            resolve_model(both, "kimi-k3", "anthropic")
        assert "does not serve" in str(exc.value)

    def test_retiring_one_leaves_the_other(self, both):
        """A reseller dropping support must not disable the direct route."""
        mark_model_unservable(both, "perplexity", "kimi-k3")
        assert resolve_model(both, "kimi-k3")["provider"] == "moonshot"


# ------------------------------------------------------------------
# Agents are told their route, never derive it
# ------------------------------------------------------------------

class TestAgentTakesResolvedRoute:
    def test_uses_what_it_is_given(self):
        a = _Agent(role="proposition", nickname="T", model="kimi-k3",
                   temperature=0.5, config={}, provider="moonshot",
                   endpoint_type="chat_completions")
        assert a._provider == "moonshot"
        assert a._endpoint_type == "chat_completions"

    def test_same_model_different_providers_are_independent(self):
        direct = _Agent(role="proposition", nickname="P", model="kimi-k3",
                        temperature=0.5, config={}, provider="moonshot",
                        endpoint_type="chat_completions")
        resold = _Agent(role="opposition", nickname="O", model="kimi-k3",
                        temperature=0.5, config={}, provider="perplexity",
                        endpoint_type="chat_completions")
        assert direct._provider == "moonshot"
        assert resold._provider == "perplexity"

    def test_no_registry_lookup_happens(self, monkeypatch):
        """Construction must not touch the DB — routing is already decided."""
        monkeypatch.setattr(
            "core.runs_db.connect",
            lambda: pytest.fail("agent must not query the registry"))
        _Agent(role="proposition", nickname="T", model="gpt-4.1",
               temperature=0.5, config={}, provider="openai",
               endpoint_type="responses")

    def test_missing_provider_is_rejected(self):
        with pytest.raises(ValueError, match="needs a provider"):
            _Agent(role="proposition", nickname="T", model="gpt-4.1",
                   temperature=0.5, config={}, provider="")


# ------------------------------------------------------------------
# Retiring models the provider rejects at inference time
# ------------------------------------------------------------------

class TestRetireUnservableModel:
    def _row(self, conn, provider, model_id):
        return conn.execute(
            "SELECT is_active, unservable FROM provider_models "
            "WHERE provider=? AND model_id=?", (provider, model_id)).fetchone()

    def _agent(self, model="kimi-k3", provider="perplexity"):
        return _Agent(role="proposition", nickname="T", model=model,
                      temperature=0.5, config={}, provider=provider,
                      endpoint_type="chat_completions")

    @pytest.mark.parametrize("message", [
        "Error code: 400 - {'error': {'code': 'model_not_found'}}",
        "Invalid model 'kimi-k3'. Permitted models can be found in the docs.",
        "The requested model 'kimi-k3' does not exist.",
        "invalid_model",
    ])
    def test_retires_on_unknown_model_verdicts(self, registry, message):
        _retire_unknown_model(self._agent(), Exception(message))
        assert self._row(registry, "perplexity", "kimi-k3")["is_active"] == 0

    @pytest.mark.parametrize("message", [
        "Rate limit reached, please try again later",
        "Authentication failed: invalid api key",
        "The AI provider is currently overloaded",
        "Connection timed out",
        "context_length exceeded",
    ])
    def test_keeps_model_on_unrelated_errors(self, registry, message):
        """Transient and auth failures say nothing about whether a model exists."""
        _retire_unknown_model(self._agent(), Exception(message))
        assert self._row(registry, "perplexity", "kimi-k3")["is_active"] == 1

    def test_retires_only_the_offending_provider_pair(self, registry):
        registry.execute(
            "INSERT INTO provider_models (provider, model_id, display_name, "
            "endpoint_type, is_active, last_updated) VALUES "
            "('moonshot','kimi-k3','kimi-k3','chat_completions',1,'now')")
        registry.commit()
        _retire_unknown_model(self._agent(), Exception("Invalid model 'kimi-k3'."))
        assert self._row(registry, "perplexity", "kimi-k3")["is_active"] == 0
        assert self._row(registry, "moonshot", "kimi-k3")["is_active"] == 1
        assert self._row(registry, "perplexity", "sonar")["is_active"] == 1

    def test_retirement_survives_a_model_resync(self, registry):
        """The provider keeps advertising it; a re-sync must not put it back.
        Otherwise the retirement lasts only until Settings is next opened."""
        _retire_unknown_model(self._agent(), Exception("Invalid model 'kimi-k3'."))
        upsert_provider_models(registry, "perplexity", [
            {"model_id": "kimi-k3", "display_name": "kimi-k3",
             "endpoint_type": "chat_completions"},
            {"model_id": "sonar", "display_name": "sonar",
             "endpoint_type": "chat_completions"},
        ])
        assert self._row(registry, "perplexity", "kimi-k3")["is_active"] == 0
        assert self._row(registry, "perplexity", "sonar")["is_active"] == 1

    def test_resync_restores_models_that_were_merely_absent(self, registry):
        """Only proven-unservable models stay down. One that dropped out of the
        listing and returned must come back."""
        upsert_provider_models(registry, "perplexity", [
            {"model_id": "gone-from-listing", "display_name": "x",
             "endpoint_type": "chat_completions"},
        ])
        assert self._row(registry, "perplexity", "gone-from-listing")["is_active"] == 1

    def test_never_raises_when_db_unavailable(self, monkeypatch, registry):
        agent = self._agent()
        monkeypatch.setattr(
            "core.runs_db.connect",
            lambda: (_ for _ in ()).throw(RuntimeError("gone")))
        _retire_unknown_model(agent, Exception("Invalid model 'kimi-k3'."))
