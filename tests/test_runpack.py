"""Tests for core/runpack.py — the full-record export.

Covers the joins (act ↔ trace ↔ sources ↔ search log), the integrity counters,
and degradation when any input file is missing or malformed.
"""
import json
import pytest

from core.runpack import (
    PACK_VERSION,
    build_pack_markdown,
    build_run_pack,
    load_traces_for_run,
    _summarise_trace,
)

RUN_ID = "run-abc"


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _act(turn=0, role="proposition", act_type="ASSERT", content="A claim.", **kw):
    base = {
        "act_id": f"act-{turn}-{role}",
        "run_id": RUN_ID,
        "turn": turn,
        "agent": role.title(),
        "agent_role": role,
        "act_type": act_type,
        "claim_id": None,
        "target_act_id": None,
        "content": content,
        "reason": None,
        "input_tokens": 100,
        "output_tokens": 20,
        "model_used": "test-model",
        "timestamp": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


def _trace(turn=0, actor="proposition", steps=None, urls=None, fabricated=None):
    return {
        "trace_id": f"trc-{turn}-{actor}",
        "correlation_id": RUN_ID,
        "action": "agent.generate",
        "actor": actor,
        "status": "completed",
        "started_at": f"2026-01-01T00:0{turn}:00Z",
        "duration_ms": 1500.0,
        "steps": [{"label": s} for s in (steps or [])],
        "events": [{"tokens_in": 100, "tokens_out": 20}],
        "outputs": {
            "urls_found": urls or [],
            **({"fabricated_urls": fabricated} if fabricated else {}),
        },
        "errors": [],
        "meta": {"model": "test-model", "turn": turn},
    }


def _source(url, turn=0, by="proposition", query="q1", excerpt=""):
    return {
        "url": url, "title": f"Title for {url}", "snippet": "A snippet.",
        "published": "", "excerpt": excerpt, "provider": "searxng",
        "harvested_by": by, "query": query, "turn": turn,
        "harvested_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "sources.json").write_text(json.dumps({
        "run_id": RUN_ID,
        "sources": [
            _source("https://a.com/1", excerpt="Extracted page text for A."),
            _source("https://b.com/2"),
            _source("https://c.com/3", turn=1, by="opposition", query="q2",
                    excerpt="Extracted page text for C."),
        ],
    }))
    (d / "search_log.jsonl").write_text(
        json.dumps({"at": "2026-01-01T00:00:01Z", "tier": "searxng", "query": "q1",
                    "result_count": 2, "raw": {"results": [{"url": "https://a.com/1"}]}}) + "\n"
        + json.dumps({"at": "2026-01-01T00:01:01Z", "tier": "searxng", "query": "q2",
                      "result_count": 1, "raw": {"results": []}}) + "\n"
    )
    return d


@pytest.fixture
def traces_dir(tmp_path):
    d = tmp_path / "traces"
    d.mkdir()
    traces = [
        _trace(0, "proposition",
               steps=["retrieve: 30+10 tokens, pool=2", "parse: ASSERT",
                      "cite.check: 1 pooled, 0 fabricated"],
               urls=["https://a.com/1"]),
        _trace(1, "opposition",
               steps=["retrieve: 40+12 tokens, pool=3", "parse: CHALLENGE",
                      "cite.check: 1 pooled, 1 fabricated"],
               urls=["https://c.com/3"], fabricated=["https://fake.com/x"]),
        {**_trace(1, "moderator", steps=["parse: STATUS"]), "action": "agent.generate"},
        {"trace_id": "trc-other", "correlation_id": "different-run",
         "action": "agent.generate", "actor": "proposition", "meta": {"turn": 0}},
    ]
    (d / "traces.jsonl").write_text("\n".join(json.dumps(t) for t in traces) + "\n")
    return d


@pytest.fixture
def data():
    return {
        "run_id": RUN_ID,
        "debate_title": "Test Debate",
        "topic": "Testing is good",
        "status": "closed",
        "created_at": "2026-01-01T00:00:00",
        "closure_reason": "max_turns",
        "config": {"max_turns": 2, "token_budget": 15000,
                   "proposition_model": "test-model"},
        "acts": [
            _act(0, "proposition", "ASSERT", "Claim with [src](https://a.com/1)."),
            _act(1, "moderator", "STATUS", '{"turns_used": 1}'),
            _act(1, "opposition", "CHALLENGE", "Counter with [src](https://c.com/3)."),
        ],
        "claims": [{"claim_id": "c1", "run_id": RUN_ID, "author": "proposition",
                    "content": "A claim.", "status": "challenged",
                    "last_updated": "2026-01-01T00:01:00"}],
    }


# ------------------------------------------------------------------
# Trace loading and filtering
# ------------------------------------------------------------------

class TestTraceLoading:
    def test_filters_by_correlation_id(self, traces_dir):
        traces = load_traces_for_run(RUN_ID, traces_dir)
        assert len(traces) == 3
        assert all(t["correlation_id"] == RUN_ID for t in traces)

    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_traces_for_run(RUN_ID, tmp_path / "nope") == []

    def test_unknown_run_returns_empty(self, traces_dir):
        assert load_traces_for_run("no-such-run", traces_dir) == []


class TestSummariseTrace:
    def test_parses_cite_check_step(self):
        s = _summarise_trace(_trace(steps=["cite.check: 3 pooled, 2 fabricated"]))
        assert s["citations_pooled"] == 3
        assert s["citations_fabricated"] == 2

    def test_parses_retrieve_step(self):
        s = _summarise_trace(_trace(steps=["retrieve: 136+40 tokens, pool=13"]))
        assert s["retrieval_tokens"] == {
            "input": 136, "output": 40, "pool_size_at_call": 13,
        }

    def test_sums_event_tokens(self):
        t = _trace()
        t["events"] = [{"tokens_in": 10, "tokens_out": 5},
                       {"tokens_in": 20, "tokens_out": 7}]
        s = _summarise_trace(t)
        assert s["tokens_in"] == 30
        assert s["tokens_out"] == 12

    def test_handles_string_steps_from_older_traceact(self):
        t = _trace()
        t["steps"] = ["cite.check: 1 pooled, 0 fabricated"]
        assert _summarise_trace(t)["citations_pooled"] == 1

    def test_no_cite_step_leaves_counts_none(self):
        s = _summarise_trace(_trace(steps=["parse: STATUS"]))
        assert s["citations_pooled"] is None


# ------------------------------------------------------------------
# Pack assembly
# ------------------------------------------------------------------

class TestPackAssembly:
    def test_top_level_shape(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        for key in ("pack_version", "generated_at", "run", "config", "token_usage",
                    "integrity", "turns", "claims", "evidence_pool", "searches", "traces"):
            assert key in pack
        assert pack["pack_version"] == PACK_VERSION

    def test_one_turn_record_per_act(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        assert len(pack["turns"]) == len(data["acts"])

    def test_act_joined_to_its_trace(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        t0 = pack["turns"][0]
        assert t0["trace"]["trace_id"] == "trc-0-proposition"
        assert t0["trace"]["duration_ms"] == 1500.0

    def test_act_joined_to_its_sources(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        t0 = pack["turns"][0]
        assert t0["retrieval"]["sources_pooled"] == 2
        assert t0["retrieval"]["pages_enriched"] == 1
        assert t0["retrieval"]["query"] == "q1"

    def test_retrieval_carries_search_log_tier_and_count(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        assert pack["turns"][0]["retrieval"]["tier"] == "searxng"
        assert pack["turns"][0]["retrieval"]["results_returned"] == 2

    def test_excerpts_present_in_turn_sources(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        srcs = pack["turns"][0]["retrieval"]["sources"]
        enriched = [s for s in srcs if s["enriched"]]
        assert len(enriched) == 1
        assert enriched[0]["excerpt"] == "Extracted page text for A."

    def test_citations_split_pooled_and_fabricated(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        opp = next(t for t in pack["turns"] if t["agent_role"] == "opposition")
        assert opp["citations"]["cited"] == ["https://c.com/3"]
        assert opp["citations"]["fabricated"] == ["https://fake.com/x"]

    def test_moderator_turn_has_no_retrieval(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        mod = next(t for t in pack["turns"] if t["agent_role"] == "moderator")
        assert mod["retrieval"] is None

    def test_raw_serp_excluded_by_default(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        assert all("raw" not in s for s in pack["searches"])

    def test_raw_serp_included_on_request(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir, include_raw_serp=True)
        assert pack["searches"][0]["raw"] == {"results": [{"url": "https://a.com/1"}]}


class TestIntegrityCounters:
    def test_counts_pool_and_enrichment(self, data, run_dir, traces_dir):
        integ = build_run_pack(data, run_dir, traces_dir)["integrity"]
        assert integ["sources_pooled"] == 3
        assert integ["sources_enriched"] == 2
        assert integ["enrichment_rate"] == pytest.approx(0.667, abs=0.001)

    def test_counts_fabricated_citations(self, data, run_dir, traces_dir):
        integ = build_run_pack(data, run_dir, traces_dir)["integrity"]
        assert integ["urls_cited"] == 2
        assert integ["urls_fabricated"] == 1
        assert integ["fabrication_rate"] == 0.5

    def test_records_search_tiers(self, data, run_dir, traces_dir):
        integ = build_run_pack(data, run_dir, traces_dir)["integrity"]
        assert integ["searches_run"] == 2
        assert integ["search_tiers_used"] == ["searxng"]

    def test_no_division_by_zero_on_empty_run(self, tmp_path, traces_dir):
        empty = {"run_id": RUN_ID, "acts": [], "claims": [], "config": {}}
        integ = build_run_pack(empty, tmp_path, traces_dir)["integrity"]
        assert integ["enrichment_rate"] == 0.0
        assert integ["fabrication_rate"] == 0.0


class TestTokenUsage:
    def test_totals_and_shares_by_role(self, data, run_dir, traces_dir):
        usage = build_run_pack(data, run_dir, traces_dir)["token_usage"]
        assert usage["total"] == 360           # 3 acts x 120
        assert usage["by_role"]["moderator"]["calls"] == 1
        assert usage["by_role"]["proposition"]["total"] == 120
        assert sum(u["share"] for u in usage["by_role"].values()) == pytest.approx(1.0, abs=0.01)


# ------------------------------------------------------------------
# Degradation — a pack must survive missing or broken inputs
# ------------------------------------------------------------------

class TestDegradation:
    def test_missing_sources_file(self, data, tmp_path, traces_dir):
        pack = build_run_pack(data, tmp_path, traces_dir)
        assert pack["evidence_pool"] == []
        assert len(pack["turns"]) == 3

    def test_missing_search_log(self, data, tmp_path, traces_dir):
        assert build_run_pack(data, tmp_path, traces_dir)["searches"] == []

    def test_malformed_sources_json(self, data, tmp_path, traces_dir):
        (tmp_path / "sources.json").write_text("{not json")
        assert build_run_pack(data, tmp_path, traces_dir)["evidence_pool"] == []

    def test_malformed_search_log_lines(self, data, tmp_path, traces_dir):
        (tmp_path / "search_log.jsonl").write_text("{bad}\n{also bad\n")
        assert build_run_pack(data, tmp_path, traces_dir)["searches"] == []

    def test_no_traces_still_builds(self, data, run_dir, tmp_path):
        pack = build_run_pack(data, run_dir, tmp_path / "none")
        assert pack["traces"] == []
        assert all(t["trace"] is None for t in pack["turns"])
        assert pack["integrity"]["urls_cited"] == 0


# ------------------------------------------------------------------
# Markdown rendering
# ------------------------------------------------------------------

class TestPackMarkdown:
    def test_renders_core_sections(self, data, run_dir, traces_dir):
        md = build_pack_markdown(build_run_pack(data, run_dir, traces_dir))
        for heading in ("# Test Debate — run pack", "## Integrity", "## Configuration",
                        "## Token usage", "## Retrieval ledger",
                        "## Turn-by-turn record", "## Evidence pool", "## Claims"):
            assert heading in md

    def test_flags_fabricated_citations(self, data, run_dir, traces_dir):
        md = build_pack_markdown(build_run_pack(data, run_dir, traces_dir))
        assert "**1 fabricated citation(s)**" in md
        assert "fabricated, stripped** — https://fake.com/x" in md

    def test_clean_run_states_no_fabrication(self, data, run_dir, traces_dir):
        pack = build_run_pack(data, run_dir, traces_dir)
        pack["integrity"]["urls_fabricated"] = 0
        assert "**No fabricated citations.**" in build_pack_markdown(pack)

    def test_excerpts_rendered_in_fenced_block(self, data, run_dir, traces_dir):
        md = build_pack_markdown(build_run_pack(data, run_dir, traces_dir))
        assert "Extracted page text for A." in md
        assert "```" in md

    def test_enrichment_markers_distinguish_sources(self, data, run_dir, traces_dir):
        md = build_pack_markdown(build_run_pack(data, run_dir, traces_dir))
        assert "◆" in md   # has page text
        assert "◇" in md   # snippet only

    def test_pipes_in_content_escaped_for_tables(self, data, run_dir, traces_dir):
        data["acts"][0]["content"] = "a | b | c"
        pack = build_run_pack(data, run_dir, traces_dir)
        pack["evidence_pool"][0]["title"] = "pipe | title"
        assert "pipe \\| title" in build_pack_markdown(pack)

    def test_empty_run_renders_without_error(self, tmp_path, traces_dir):
        empty = {"run_id": RUN_ID, "topic": "Nothing", "acts": [],
                 "claims": [], "config": {}}
        md = build_pack_markdown(build_run_pack(empty, tmp_path, traces_dir))
        assert "## Integrity" in md
