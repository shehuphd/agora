"""Tests for the cost surfaces added 2026-08-30: the Settings lifetime-spend
aggregation, provider resolution in the pre-debate estimate (CSV batch rows
carry no provider column), cost on the SSE act events, and the transcript
export's cost line.

Every surface reads recorded per-act cost_usd — priced once at generation
time — and follows the shared honesty semantics: an unpriceable total is
None (never $0.00), and a total missing any priced-but-unknown act is
flagged partial, shown as a minimum.
"""
import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from api.models import CostEstimateRequest
from api.routers.debates import estimate_cost
from api.routers.settings import _total_tokens_from_runs
from core.export import build_markdown
from core.runs_db import init as _init
from core.state import Act
from runners.debate import _act_to_dict


# ------------------------------------------------------------------
# _total_tokens_from_runs — lifetime spend on the Settings screen
# ------------------------------------------------------------------

_ACTS_DDL = """
CREATE TABLE acts (
    act_id TEXT, run_id TEXT, turn INTEGER, agent TEXT, agent_role TEXT,
    act_type TEXT, claim_id TEXT, target_act_id TEXT, content TEXT,
    reason TEXT, input_tokens INTEGER, output_tokens INTEGER,
    model_used TEXT, timestamp TEXT{extra}
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _make_run_db(run_dir, acts, with_cost_column=True, reset_at=None):
    run_dir.mkdir(parents=True)
    conn = sqlite3.connect(run_dir / "debate.db")
    conn.executescript(_ACTS_DDL.format(extra=", cost_usd REAL" if with_cost_column else ""))
    for i, (tokens, cost, ts) in enumerate(acts):
        cols = "act_id, run_id, turn, agent_role, input_tokens, output_tokens, timestamp"
        vals = [f"a{i}", "r", i, "proposition", tokens, 0, ts]
        if with_cost_column:
            cols += ", cost_usd"
            vals.append(cost)
        conn.execute(f"INSERT INTO acts ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    if reset_at:
        conn.execute("INSERT INTO meta (key, value) VALUES ('token_reset_event', ?)", (reset_at,))
    conn.commit()
    conn.close()


class TestLifetimeSpend:
    def test_sums_recorded_costs_across_runs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("api.routers.settings.RUNS_DIR", tmp_path)
        _make_run_db(tmp_path / "run1", [(100, 0.5, "2026-01-01"), (100, 0.25, "2026-01-02")])
        _make_run_db(tmp_path / "run2", [(100, 0.1, "2026-01-01")])
        t = _total_tokens_from_runs()
        assert t["cost_usd"] == pytest.approx(0.85)
        assert t["cost_partial"] is False

    def test_unpriced_act_with_tokens_flags_partial(self, tmp_path, monkeypatch):
        monkeypatch.setattr("api.routers.settings.RUNS_DIR", tmp_path)
        _make_run_db(tmp_path / "run1", [(100, 0.5, "2026-01-01"), (100, None, "2026-01-02")])
        t = _total_tokens_from_runs()
        assert t["cost_usd"] == pytest.approx(0.5)
        assert t["cost_partial"] is True

    def test_nothing_priced_is_none_not_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("api.routers.settings.RUNS_DIR", tmp_path)
        _make_run_db(tmp_path / "run1", [(100, None, "2026-01-01")])
        t = _total_tokens_from_runs()
        assert t["cost_usd"] is None
        assert t["cost_partial"] is True

    def test_pre_cost_column_db_counts_as_unpriced_not_free(self, tmp_path, monkeypatch):
        """A run DB from before the cost column existed must flag the total
        partial — its spend happened, it just was never priced."""
        monkeypatch.setattr("api.routers.settings.RUNS_DIR", tmp_path)
        _make_run_db(tmp_path / "old", [(100, None, "2026-01-01")], with_cost_column=False)
        _make_run_db(tmp_path / "new", [(100, 0.3, "2026-01-01")])
        t = _total_tokens_from_runs()
        assert t["cost_usd"] == pytest.approx(0.3)
        assert t["cost_partial"] is True

    def test_reset_event_excludes_earlier_spend(self, tmp_path, monkeypatch):
        """The spend counter honours the same token_reset_event the token
        counters use — one button resets both displays, data untouched."""
        monkeypatch.setattr("api.routers.settings.RUNS_DIR", tmp_path)
        _make_run_db(
            tmp_path / "run1",
            [(100, 0.5, "2026-01-01"), (100, 0.25, "2026-03-01")],
            reset_at="2026-02-01",
        )
        t = _total_tokens_from_runs()
        assert t["cost_usd"] == pytest.approx(0.25)
        assert t["cost_partial"] is False


# ------------------------------------------------------------------
# estimate-cost — provider resolution for provider-less requests
# ------------------------------------------------------------------

class _KeepOpen:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


@pytest.fixture
def registry(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init(conn)
    conn.executemany(
        "INSERT INTO provider_models "
        "(provider, model_id, display_name, is_active, last_updated) "
        "VALUES (?,?,?,?,'now')",
        [("openai", "gpt-4.1", "gpt-4.1", 1),
         ("anthropic", "claude-sonnet-5", "claude-sonnet-5", 1)],
    )
    conn.commit()
    monkeypatch.setattr("core.runs_db.connect", lambda: _KeepOpen(conn))
    monkeypatch.setattr("api.routers.debates._runs_db.connect", lambda: _KeepOpen(conn))
    return conn


class TestEstimateResolvesProvider:
    def test_model_without_provider_is_resolved_from_registry(self, registry):
        """CSV batch rows name models with no provider column; the estimate
        must resolve the provider the same way debate creation does instead
        of silently skipping the role."""
        req = CostEstimateRequest(prop_model="gpt-4.1", token_budget=100_000)
        est = asyncio.run(estimate_cost(req))
        assert est["by_role"]["proposition"]["provider"] == "openai"
        assert est["assumptions"]["roles_counted"] == 1

    def test_unregistered_model_stays_unpriced_not_guessed(self, registry):
        req = CostEstimateRequest(prop_model="no-such-model", token_budget=100_000)
        est = asyncio.run(estimate_cost(req))
        assert est["by_role"]["proposition"]["provider"] is None
        assert est["assumptions"]["roles_counted"] == 0
        assert est["total_usd"] is None


# ------------------------------------------------------------------
# SSE act events and transcript export
# ------------------------------------------------------------------

def _act(cost_usd=None, input_tokens=100, output_tokens=50):
    return Act(
        act_id="a1", run_id="r1", turn=0, agent="T", agent_role="proposition",
        act_type="ASSERT", claim_id=None, target_act_id=None, content="x",
        reason=None, input_tokens=input_tokens, output_tokens=output_tokens,
        model_used="m", timestamp=datetime.utcnow().isoformat(),
        cost_usd=cost_usd,
    )


class TestActEventCarriesCost:
    def test_act_to_dict_includes_cost(self):
        """The live debate view's running spend sums cost off the SSE act
        events; an event without the field silently zeroes the readout."""
        assert _act_to_dict(_act(cost_usd=0.12))["cost_usd"] == 0.12

    def test_unpriced_act_sends_null_not_zero(self):
        assert _act_to_dict(_act(cost_usd=None))["cost_usd"] is None


def _export_data(acts):
    return {
        "run_id": "r1", "debate_title": "T", "topic": "T",
        "status": "closed", "created_at": "2026-01-01",
        "config": {}, "claims": [],
        "acts": [
            {
                "turn": i, "agent": "T", "agent_role": "proposition",
                "act_type": "ASSERT", "content": "x", "reason": None,
                "input_tokens": a["in"], "output_tokens": a["out"],
                "model_used": "m", "timestamp": "2026-01-01",
                "cost_usd": a["cost"],
            }
            for i, a in enumerate(acts)
        ],
    }


class TestExportCostLine:
    def test_total_cost_line_from_recorded_costs(self):
        md = build_markdown(_export_data([
            {"in": 100, "out": 50, "cost": 0.30},
            {"in": 100, "out": 50, "cost": 0.20},
        ]))
        assert "**Total cost:** $0.50" in md

    def test_partial_total_is_named_a_minimum(self):
        md = build_markdown(_export_data([
            {"in": 100, "out": 50, "cost": 0.30},
            {"in": 100, "out": 50, "cost": None},
        ]))
        assert "**Total cost:** $0.30 (minimum — some calls could not be priced)" in md

    def test_no_priced_acts_no_cost_line(self):
        """A pre-cost-tracking export shows no cost line at all rather than
        an invented figure."""
        md = build_markdown(_export_data([{"in": 100, "out": 50, "cost": None}]))
        assert "Total cost" not in md
