"""Tests for core/cost.py — dollar pricing backed by the `rates` registry.

Prices are checked against records from the installed `rates` package
(PyPI, pinned in requirements.txt), not a mocked stand-in: the whole point
of this module is the provider-alias mapping and the miss-handling contract,
both of which only mean something against the registry's actual data.
"""
import pytest

from core import cost


@pytest.fixture(autouse=True)
def _fresh_price_cache():
    """price_for is memoized (the picker enrichment asks for every model per
    request); tests that monkeypatch the registry or alias map must not see
    another test's cached hits."""
    cost.price_for.cache_clear()
    yield
    cost.price_for.cache_clear()


# ------------------------------------------------------------------
# price_for / cost_usd against live-verified rates records
# ------------------------------------------------------------------

class TestKnownPrices:
    @pytest.mark.parametrize("provider,model,in_mtok,out_mtok", [
        ("anthropic",  "claude-opus-4-8",           5.0,  25.0),
        ("openai",     "gpt-4.1",                   2.0,   8.0),
        ("google",     "gemini-2.5-pro",             1.25, 10.0),
        ("perplexity", "sonar",                      1.0,   1.0),
        ("moonshot",   "kimi-k3",                    3.0,  15.0),
        ("xai",        "grok-4.5",                   2.0,   6.0),
    ])
    def test_price_matches_verified_table(self, provider, model, in_mtok, out_mtok):
        price = cost.price_for(provider, model)
        assert price is not None, f"{provider}/{model} should be priced"
        assert price.input_mtok == in_mtok
        assert price.output_mtok == out_mtok

    def test_cost_usd_computes_from_real_tokens(self):
        # gpt-4.1: $2/$8 per Mtok. 1000 in + 500 out.
        c = cost.cost_usd("openai", "gpt-4.1", 1000, 500)
        assert c == pytest.approx((1000 * 2.0 + 500 * 8.0) / 1e6)

    def test_moonshot_needs_the_moonshotai_alias(self):
        """The crux this module exists for: Agora's provider key is
        'moonshot', but rates lists Moonshot's models under 'moonshotai'.
        Confirms the alias is applied, not just present in the map."""
        assert cost.price_for("moonshot", "kimi-k3") is not None
        # And the un-aliased name must NOT accidentally work — if it did,
        # rates would have to carry a "moonshot" provider too, which it
        # doesn't (verified live, 2026-08-29).
        assert cost._RATES_PROVIDER["moonshot"] == "moonshotai"


# ------------------------------------------------------------------
# Misses must degrade to None, never raise — this is the whole contract
# ------------------------------------------------------------------

class TestMisses:
    def test_unknown_model_returns_none(self):
        assert cost.price_for("openai", "definitely-not-a-model-xyz") is None

    def test_unknown_provider_returns_none(self):
        assert cost.price_for("some-provider-agora-has-never-heard-of", "gpt-4.1") is None

    def test_cost_usd_returns_none_on_miss_not_raise(self):
        assert cost.cost_usd("openai", "definitely-not-a-model-xyz", 100, 100) is None

    def test_rates_uninstalled_degrades_to_none_everywhere(self, monkeypatch):
        """Simulates rates not being installed at all — cost.py must never
        raise or behave differently, only report everything as unpriced."""
        monkeypatch.setattr(cost, "_registry", lambda: None)
        assert cost.price_for("openai", "gpt-4.1") is None
        assert cost.cost_usd("anthropic", "claude-opus-4-8", 1000, 1000) is None

    def test_registry_load_failure_degrades_to_none(self, monkeypatch):
        """A corrupt bundle or any other load-time exception must not
        propagate out of cost.py."""
        def _boom():
            raise RuntimeError("ledger exploded")
        monkeypatch.setattr("rates.ai.load", _boom, raising=False)
        cost._registry.cache_clear()
        try:
            assert cost.price_for("openai", "gpt-4.1") is None
        finally:
            cost._registry.cache_clear()  # don't leak the monkeypatched failure into later tests


# ------------------------------------------------------------------
# Mutation check: prove the alias table is load-bearing, not decorative
# ------------------------------------------------------------------

class TestAliasIsLoadBearing:
    def test_without_the_alias_moonshot_would_not_price(self, monkeypatch):
        monkeypatch.setitem(cost._RATES_PROVIDER, "moonshot", "moonshot")  # the wrong, unaliased name
        assert cost.price_for("moonshot", "kimi-k3") is None


# ------------------------------------------------------------------
# Freshness: the stable tier is preferred, the bundled tier is the fallback
# ------------------------------------------------------------------

class TestStableTierPreference:
    def _load_recorder(self, monkeypatch, stable_raises=False):
        import rates.ai
        calls = []
        real_load = rates.ai.load

        def fake_load(*args, **kwargs):
            calls.append(kwargs)
            if kwargs.get("fetch") == "stable" and stable_raises:
                raise TypeError("no fetch parameter on this rates version")
            return real_load()  # bundled: offline, deterministic for the test

        monkeypatch.setattr("rates.ai.load", fake_load)
        cost._registry.cache_clear()
        return calls

    def test_stable_tier_is_asked_for_first(self, monkeypatch):
        """Prices should track the published ledger when reachable, not stay
        frozen at whatever snapshot shipped inside the installed package."""
        calls = self._load_recorder(monkeypatch)
        try:
            assert cost.price_for("openai", "gpt-4.1") is not None
            assert calls[0].get("fetch") == "stable"
        finally:
            cost._registry.cache_clear()

    def test_falls_back_to_bundled_when_stable_unavailable(self, monkeypatch):
        """A rates version without the fetch parameter (or any stable-path
        surprise) must degrade to the bundled snapshot, never to no prices."""
        calls = self._load_recorder(monkeypatch, stable_raises=True)
        try:
            assert cost.price_for("openai", "gpt-4.1") is not None
            assert calls[0].get("fetch") == "stable"
            assert "fetch" not in calls[1]
        finally:
            cost._registry.cache_clear()


class TestSnapshotDate:
    def test_reports_the_registry_snapshot_date(self):
        d = cost.snapshot_date()
        assert d is not None
        assert len(d) == 10 and d[4] == "-" and d[7] == "-"  # YYYY-MM-DD

    def test_none_when_rates_unavailable(self, monkeypatch):
        monkeypatch.setattr(cost, "_registry", lambda: None)
        assert cost.snapshot_date() is None
