"""Tests for the single-source-of-truth max_turns resolution in core/config.py.

Before this fix, four different literals disagreed across the codebase
(config/defaults.yaml: 60, api/models.py: 8, core/config.py: 15, and the
New Debate form's HTML slider: 20) — a debate created via the API with no
max_turns specified silently got capped at 8 turns regardless of its token
budget. Found 2026-08-30.
"""
import pytest

from api.models import DebateConfig
from core.config import DebateRunConfig, ProtocolRunConfig, DEFAULT_MAX_TURNS, _configured_max_turns


class TestDebateConfigDefault:
    def test_max_turns_defaults_to_none_not_a_literal(self):
        """api/models.py must not carry its own numeric opinion — None means
        'unspecified', letting DebateRunConfig.from_api() resolve it against
        the single source of truth instead of silently using 8."""
        cfg = DebateConfig(topic="does UBI work")
        assert cfg.max_turns is None


class TestConfiguredMaxTurns:
    def test_matches_defaults_yaml(self):
        assert _configured_max_turns() == 100

    def test_default_max_turns_constant_matches(self):
        # The Python-level fallback constant must agree with the live YAML
        # value — they're meant to be kept in lockstep by hand.
        assert DEFAULT_MAX_TURNS == _configured_max_turns()


class TestProtocolRunConfigDefault:
    def test_bare_construction_uses_configured_value(self):
        """A ProtocolRunConfig() built with no args (only happens off the
        live create-debate path, e.g. in tests) must not silently disagree
        with config/defaults.yaml."""
        assert ProtocolRunConfig().max_turns == _configured_max_turns()


class TestFromApiResolvesMaxTurns:
    def test_unset_max_turns_resolves_to_configured_default(self):
        cfg = DebateConfig(topic="t", prop_model="m", opp_model="m", mod_model="m")
        run_cfg = DebateRunConfig.from_api(cfg)
        assert run_cfg.protocol.max_turns == _configured_max_turns()

    def test_explicit_max_turns_is_respected_not_overridden(self):
        cfg = DebateConfig(topic="t", prop_model="m", opp_model="m", mod_model="m", max_turns=42)
        run_cfg = DebateRunConfig.from_api(cfg)
        assert run_cfg.protocol.max_turns == 42

    def test_zero_max_turns_still_resolves_to_default(self):
        # max_turns=0 is nonsensical (a debate with no turns) — treated the
        # same as unset rather than producing a debate that can never run.
        cfg = DebateConfig(topic="t", prop_model="m", opp_model="m", mod_model="m", max_turns=0)
        run_cfg = DebateRunConfig.from_api(cfg)
        assert run_cfg.protocol.max_turns == _configured_max_turns()


class TestFromDictResolvesMaxTurns:
    def test_missing_max_turns_resolves_to_configured_default(self):
        d = {"topic": "t", "protocol": {}}
        run_cfg = DebateRunConfig.from_dict(d)
        assert run_cfg.protocol.max_turns == _configured_max_turns()

    def test_stored_max_turns_is_respected(self):
        d = {"topic": "t", "protocol": {"max_turns": 77}}
        run_cfg = DebateRunConfig.from_dict(d)
        assert run_cfg.protocol.max_turns == 77


class TestSeatModelFieldNames:
    def test_every_seat_accepts_its_long_form_model_field(self):
        # The long names are the documented canonical fields. synthesiser_model
        # was missing from DebateConfig until 2026-09-05: pydantic silently
        # dropped the key and the seat fell back to the first available model
        # (a live run got gpt-3.5-turbo in a debate configured all-flash-lite).
        cfg = DebateConfig(
            topic="t",
            proposition_model="model-p", opposition_model="model-o",
            moderator_model="model-m", synthesiser_model="model-s",
        )
        run_cfg = DebateRunConfig.from_api(cfg, first_available="fallback")
        assert run_cfg.proposition.model == "model-p"
        assert run_cfg.opposition.model == "model-o"
        assert run_cfg.moderator.model == "model-m"
        assert run_cfg.synthesiser.model == "model-s"

    def test_short_alias_still_wins_when_both_are_sent(self):
        cfg = DebateConfig(topic="t", synth_model="short", synthesiser_model="long")
        run_cfg = DebateRunConfig.from_api(cfg, first_available="fallback")
        assert run_cfg.synthesiser.model == "short"
