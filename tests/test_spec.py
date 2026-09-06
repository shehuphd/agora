"""Experiment specs — validation, deterministic expansion, grouping keys."""
import pytest

from core import spec as _spec


def _grid_spec(**over):
    spec = {
        "base": {"topic": "cats are better than dogs", "token_budget": 50000},
        "factors": [
            {"field": "proposition_model", "levels": ["kimi-k3", "gpt-4.1"]},
            {"field": "synth_model", "levels": ["sonar", "sonar-pro"]},
        ],
        "replicates": 3,
    }
    spec.update(over)
    return spec


class TestValidate:
    def test_valid_grid_passes(self):
        assert _spec.validate(_grid_spec()) is None

    def test_unknown_factor_field_rejected(self):
        with pytest.raises(_spec.SpecError, match="unknown factor field"):
            _spec.validate(_grid_spec(factors=[{"field": "vibes", "levels": ["a"]}]))

    def test_unknown_base_field_rejected(self):
        with pytest.raises(_spec.SpecError, match="unknown base config field"):
            _spec.validate(_grid_spec(base={"topic": "x", "vibes": 1}))

    def test_duplicate_factor_rejected(self):
        with pytest.raises(_spec.SpecError, match="appears twice"):
            _spec.validate(_grid_spec(factors=[
                {"field": "synth_model", "levels": ["a"]},
                {"field": "synth_model", "levels": ["b"]},
            ]))

    def test_topic_required_unless_a_factor(self):
        with pytest.raises(_spec.SpecError, match="topic"):
            _spec.validate(_grid_spec(base={"token_budget": 1000}))
        # Topic as a factor satisfies the requirement.
        _spec.validate(_grid_spec(
            base={"token_budget": 1000},
            factors=[{"field": "topic", "levels": ["a", "b"]}],
        ))

    def test_expansion_ceiling(self):
        with pytest.raises(_spec.SpecError, match="ceiling"):
            _spec.validate(_grid_spec(replicates=200))

    def test_rows_spec_needs_topics(self):
        with pytest.raises(_spec.SpecError, match="no topic"):
            _spec.validate({"rows": [{"topic": "ok"}, {"token_budget": 5}]})

    def test_replicates_must_be_positive_int(self):
        with pytest.raises(_spec.SpecError, match="replicates"):
            _spec.validate(_grid_spec(replicates=0))


class TestExpand:
    def test_cartesian_times_replicates(self):
        rows = _spec.expand(_grid_spec())
        assert len(rows) == 2 * 2 * 3
        # Every row carries its full config and its condition labels.
        first = rows[0]
        assert first["topic"] == "cats are better than dogs"
        assert first["proposition_model"] in ("kimi-k3", "gpt-4.1")
        cond = first["_condition"]
        assert set(cond) == {"proposition_model", "synth_model", "replicate"}
        # All 4 conditions appear in every replicate.
        keys = {_spec.condition_key(r["_condition"]) for r in rows}
        assert len(keys) == 4
        reps = {r["_condition"]["replicate"] for r in rows}
        assert reps == {1, 2, 3}

    def test_expansion_is_deterministic(self):
        assert _spec.expand(_grid_spec()) == _spec.expand(_grid_spec())

    def test_replicate_start_continues_numbering(self):
        rows = _spec.expand(_grid_spec(), replicates=2, replicate_start=4)
        assert {r["_condition"]["replicate"] for r in rows} == {4, 5}

    def test_no_factors_is_one_condition(self):
        rows = _spec.expand({"base": {"topic": "t"}, "replicates": 2})
        assert len(rows) == 2
        assert rows[0]["_condition"] == {"replicate": 1}

    def test_rows_spec_expands_with_row_labels(self):
        rows = _spec.expand({"rows": [{"topic": "a"}, {"topic": "b"}], "replicates": 2})
        assert len(rows) == 4
        assert rows[0]["_condition"] == {"row": 1, "replicate": 1}


class TestConditionKey:
    def test_replicate_is_ignored(self):
        a = {"proposition_model": "x", "replicate": 1}
        b = {"proposition_model": "x", "replicate": 9}
        assert _spec.condition_key(a) == _spec.condition_key(b)

    def test_key_is_order_independent(self):
        assert (_spec.condition_key({"a": 1, "b": 2})
                == _spec.condition_key({"b": 2, "a": 1}))

    def test_empty_condition(self):
        assert _spec.condition_key(None) == ""


class TestCsvColumnsStayAligned:
    def test_batch_router_uses_the_same_field_list(self):
        """The CSV import and specs must accept the same fields — the router
        imports its column list from core/spec.py, and this guards against
        someone re-declaring it locally."""
        from api.routers import batch as batch_router
        assert batch_router._COLUMNS is _spec.SPEC_FIELDS
