"""The quote contract and number grounding (core/citations.py), and their
enforcement pass in agents/base.py.

The construction mirrors the URL layer: agents can only attach source text the
system can verify by string comparison. Matching is loose on surface form
("18%" and "eighteen per cent" are the same number) and strict on substance.
"""
import uuid
from datetime import datetime

import pytest

from core.citations import (
    check_act_citations,
    extract_numbers,
    normalise,
    quote_matches,
    sentences_citing,
    strip_links,
    ungrounded_numbers,
)
from core.state import Act


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

class TestNormalise:
    @pytest.mark.parametrize("a,b", [
        ("18%", "eighteen per cent"),
        ("18%", "18 percent"),
        ("18%", "Eighteen Percent"),
        ("twenty-five", "25"),
        ("ninety-nine", "99"),
        ("18–25", "18-25"),          # en dash vs hyphen
        ("18—25", "18-25"),          # em dash vs hyphen
        ("“quoted”", '"quoted"'),
        ("it’s", "it's"),
        ("a  b\n c", "a b c"),
    ])
    def test_equivalent_forms_normalise_identically(self, a, b):
        assert normalise(a) == normalise(b)

    def test_two_adjacent_unit_words_stay_two_numbers(self):
        assert normalise("four five") == "4 5"

    def test_percentage_word_survives(self):
        assert normalise("a percentage of") == "a percentage of"


# ---------------------------------------------------------------------------
# Quote matching
# ---------------------------------------------------------------------------

_SOURCE = (
    "The line's contribution to GDP, including induced effects of 18 to 25% "
    "(Fouqueray, 2016), was concentrated in the Rhône corridor — a finding the "
    "authors call “robust”."
)


class TestQuoteMatches:
    def test_verbatim_quote_matches(self):
        assert quote_matches("induced effects of 18 to 25%", _SOURCE)

    def test_case_whitespace_dash_and_quote_variants_match(self):
        assert quote_matches(
            'Rhône corridor - a finding the authors call "ROBUST"', _SOURCE
        )

    def test_numeral_variant_matches_spelt_out_source(self):
        assert quote_matches("effects of eighteen to twenty-five percent", _SOURCE)

    def test_elided_quote_matches_in_order(self):
        assert quote_matches("contribution to GDP ... Rhône corridor", _SOURCE)

    def test_elided_quote_out_of_order_fails(self):
        assert not quote_matches("Rhône corridor ... contribution to GDP", _SOURCE)

    def test_paraphrase_fails(self):
        assert not quote_matches("HSR induces 18-25% more ridership", _SOURCE)

    def test_empty_quote_never_matches(self):
        assert not quote_matches("", _SOURCE)
        assert not quote_matches("...", _SOURCE)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

class TestNumbers:
    def test_extraction_canonicalises(self):
        assert extract_numbers("1,200 riders, 18.50%, v2") == {"1200", "18.5", "2"}

    def test_url_digits_are_not_figures(self):
        s = "Ridership rose 12% ([study](https://example.org/2016/report-18))."
        assert extract_numbers(strip_links(s)) == {"12"}

    def test_grounded_range_across_dash_variants(self):
        assert ungrounded_numbers(
            "induces 18–25% more ridership", "effects of 18 to 25%"
        ) == []

    def test_spelt_out_sentence_grounded_by_numeral_quote(self):
        assert ungrounded_numbers(
            "roughly eighteen per cent more", "a decline of 18%"
        ) == []

    def test_ungrounded_number_is_named(self):
        assert ungrounded_numbers("a 30% drop", "a decline of 18%") == ["30"]

    def test_sentences_citing_pairs_urls(self):
        content = (
            "First point. Ridership rose ([s](https://a.org/x)). "
            "Unsourced aside. Costs fell ([t](https://b.org/y))."
        )
        got = sentences_citing(content)
        assert len(got) == 2
        assert "https://a.org/x" in got[0][1]
        assert "https://b.org/y" in got[1][1]


# ---------------------------------------------------------------------------
# Act-level check
# ---------------------------------------------------------------------------

_URL = "https://example.org/hsr-study"


def _report(content, citations, texts, titles=None):
    return check_act_citations(
        content, citations,
        lambda u: texts.get(u, None),
        (lambda u: (titles or {}).get(u, None)),
    )


class TestCheckActCitations:
    def test_verified_quote(self):
        r = _report(
            f"Induced effects reached 18 to 25% ([s]({_URL})).",
            [{"url": _URL, "quote": "induced effects of 18 to 25%"}],
            {_URL: _SOURCE},
        )
        assert r["citations"][0]["status"] == "verified"
        assert r["citations"][0]["ungrounded_numbers"] == []
        assert r["violations"] == 0

    def test_mismatched_quote_is_a_violation(self):
        r = _report(
            f"HSR induces more ridership ([s]({_URL})).",
            [{"url": _URL, "quote": "HSR induces 18-25% more ridership"}],
            {_URL: _SOURCE},
        )
        assert r["citations"][0]["status"] == "mismatch"
        assert r["violations"] == 1

    def test_number_moved_out_of_quote_is_a_violation(self):
        # The live 2026-09-05 distortion class: a figure attached to a source
        # whose quoted text does not state it in the citing sentence.
        r = _report(
            f"The study shows a 30% ridership rise ([s]({_URL})).",
            [{"url": _URL, "quote": "induced effects of 18 to 25%"}],
            {_URL: _SOURCE},
        )
        assert r["citations"][0]["ungrounded_numbers"] == ["30"]
        assert r["violations"] == 1

    def test_unverifiable_source_is_not_a_violation(self):
        r = _report(
            f"Emissions fell 18% ([s]({_URL})).",
            [{"url": _URL, "quote": "an 18% decline in emissions"}],
            {_URL: ""},   # pooled, bot-walled: no stored text
        )
        assert r["citations"][0]["status"] == "unverifiable"
        assert r["violations"] == 0

    def test_headline_quote_is_title_only_not_verified(self):
        # The quote-the-headline loophole (first judged run, 2026-09-05): a
        # title is stored text, so a headline quote passed the substring
        # check while substantiating nothing. It now gets its own status.
        r = _report(
            f"Bike lanes cut emissions ([s]({_URL})).",
            [{"url": _URL, "quote": "City study on bike lanes and emissions"}],
            {_URL: _SOURCE},
            titles={_URL: "City study on bike lanes and emissions"},
        )
        assert r["citations"][0]["status"] == "title_only"
        assert r["violations"] == 0

    def test_headline_quote_on_bodyless_source_is_still_title_only(self):
        r = _report(
            "Point ([s]({0})).".format(_URL),
            [{"url": _URL, "quote": "City study on bike lanes"}],
            {_URL: ""},    # bot-walled: no body text at all
            titles={_URL: "City study on bike lanes"},
        )
        assert r["citations"][0]["status"] == "title_only"

    def test_body_match_verifies_even_when_title_also_matches(self):
        r = _report(
            f"Induced effects reached 18 to 25% ([s]({_URL})).",
            [{"url": _URL, "quote": "induced effects of 18 to 25%"}],
            {_URL: _SOURCE},
            titles={_URL: "induced effects of 18 to 25%"},
        )
        assert r["citations"][0]["status"] == "verified"

    def test_array_only_act_grounds_numbers_against_quote_union(self):
        # Some models cite only through the structured array with no inline
        # URL (observed live 2026-09-06: claude-sonnet-5, gpt-4.1) — the
        # act's numbers must then appear in the union of its quotes.
        r = _report(
            "Induced effects reached 18 to 25%, and unemployment fell 30%.",
            [{"url": _URL, "quote": "induced effects of 18 to 25%"}],
            {_URL: _SOURCE},
        )
        extra = [c for c in r["citations"] if c["status"] == "ungrounded_act_numbers"]
        assert len(extra) == 1
        assert extra[0]["ungrounded_numbers"] == ["30"]
        assert r["violations"] == 1

    def test_array_only_act_with_grounded_numbers_passes(self):
        r = _report(
            "Induced effects reached 18 to 25%.",
            [{"url": _URL, "quote": "induced effects of 18 to 25%"}],
            {_URL: _SOURCE},
        )
        assert all(c["status"] != "ungrounded_act_numbers" for c in r["citations"])
        assert r["violations"] == 0

    def test_inline_citing_act_skips_act_level_grounding(self):
        # Per-sentence grounding already covers inline citers; the act-level
        # pass must not double-flag numbers from other sentences.
        r = _report(
            f"Induced effects reached 18 to 25% ([s]({_URL})). Unrelated aside about 7 things.",
            [{"url": _URL, "quote": "induced effects of 18 to 25%"}],
            {_URL: _SOURCE},
        )
        assert all(c["status"] != "ungrounded_act_numbers" for c in r["citations"])

    def test_unpooled_and_missing_quote_statuses(self):
        r = _report(
            "Point.",
            [{"url": "https://nowhere.org/", "quote": "q"},
             {"url": _URL, "quote": ""}],
            {_URL: _SOURCE},
        )
        statuses = {c["status"] for c in r["citations"]}
        assert statuses == {"unpooled", "missing_quote"}
        assert r["violations"] == 0


# ---------------------------------------------------------------------------
# Enforcement pass (_enforce_quotes)
# ---------------------------------------------------------------------------

class _FakePool:
    def __init__(self, texts):
        self.texts = texts
        self.fetch_calls: list[list[str]] = []

    def ensure_full_text(self, urls):
        self.fetch_calls.append(list(urls))
        return 0

    def source_text_for(self, url):
        return self.texts.get(url, None)

    def source_title_for(self, url):
        return "" if url in self.texts else None


class _FakeTrace:
    def __init__(self):
        self.steps: list[str] = []
        self.outputs: list[dict] = []

    def step(self, msg):
        self.steps.append(msg)

    def output(self, data):
        self.outputs.append(data)


def _act(content, citations):
    return Act(
        act_id=str(uuid.uuid4()), run_id="r", turn=1, agent="Thesis",
        agent_role="proposition", act_type="ASSERT", claim_id=None,
        target_act_id=None, content=content, reason=None,
        input_tokens=1, output_tokens=1, model_used="m",
        timestamp=datetime.utcnow().isoformat(), citations=citations,
    )


def _agent():
    from agents.proposition import PropositionAgent
    return PropositionAgent(provider="openai")


class TestEnforceQuotes:
    def test_mismatch_marks_the_act(self):
        pool = _FakePool({_URL: _SOURCE})
        act = _act(
            f"HSR induces more ridership ([s]({_URL})).",
            [{"url": _URL, "quote": "HSR induces 18-25% more ridership"}],
        )
        _agent()._enforce_quotes(_FakeTrace(), act, pool)
        assert act.citations[0]["status"] == "mismatch"
        assert "[citation check:" in act.content

    def test_verified_leaves_content_alone(self):
        pool = _FakePool({_URL: _SOURCE})
        content = f"Induced effects reached 18 to 25% ([s]({_URL}))."
        act = _act(content, [{"url": _URL, "quote": "induced effects of 18 to 25%"}])
        _agent()._enforce_quotes(_FakeTrace(), act, pool)
        assert act.citations[0]["status"] == "verified"
        assert act.content == content

    def test_full_text_fetch_requested_for_cited_urls(self):
        pool = _FakePool({_URL: _SOURCE})
        act = _act("x", [{"url": _URL, "quote": "induced effects of 18 to 25%"}])
        _agent()._enforce_quotes(_FakeTrace(), act, pool)
        assert pool.fetch_calls == [[_URL]]

    def test_no_citations_is_a_no_op(self):
        pool = _FakePool({})
        act = _act("plain reasoning, no sources", None)
        _agent()._enforce_quotes(_FakeTrace(), act, pool)
        assert act.citations is None
        assert pool.fetch_calls == []


# ---------------------------------------------------------------------------
# Pool full-text storage
# ---------------------------------------------------------------------------

class TestPoolFullText:
    def _pool(self, monkeypatch, fetched_text):
        from core import search as _search
        from core.sources import Source, SourcePool
        calls = []

        def fake_fetch(url, max_chars):
            calls.append(url)
            return fetched_text

        monkeypatch.setattr(_search, "fetch_page_markdown", fake_fetch)
        pool = SourcePool("run-x")
        pool.add_many([Source(url=_URL, title="T", snippet="S")])
        return pool, calls

    def test_fetches_once_and_stores(self, monkeypatch):
        pool, calls = self._pool(monkeypatch, "full body text")
        pool.ensure_full_text([_URL])
        pool.ensure_full_text([_URL])   # second call must not refetch
        assert calls == [_URL]
        assert "full body text" in pool.source_text_for(_URL)

    def test_failed_fetch_is_not_retried(self, monkeypatch):
        pool, calls = self._pool(monkeypatch, "")
        pool.ensure_full_text([_URL])
        pool.ensure_full_text([_URL])
        assert calls == [_URL]
        # The snippet remains checkable body text even with no page body;
        # the title is served separately (headline quotes never verify).
        assert pool.source_text_for(_URL) == "S"
        assert pool.source_title_for(_URL) == "T"

    def test_unpooled_url_returns_none(self, monkeypatch):
        pool, _ = self._pool(monkeypatch, "x")
        assert pool.source_text_for("https://other.org/") is None
        assert pool.source_title_for("https://other.org/") is None
