"""Mechanical citation-fidelity checks: the quote contract and number grounding.

The URL layer is already zero-fabrication by construction: a URL must be copied
from the pool, membership is checked by string comparison after generation, and
failures are stripped. This module extends the same construction one level up,
to the claim made about the source:

- Quote contract: every citation carries a verbatim quote, and the quote must
  appear as a substring of the stored source text. Checked mechanically, no
  model calls.
- Number grounding: every number in a sentence that cites a source must also
  appear inside that citation's quote, so a figure cannot be separated from its
  surrounding context without the mismatch being visible.

Matching is deliberately loose on surface form and strict on substance:
"18%" and "eighteen per cent" are the same number, en dashes and hyphens are
the same dash, curly and straight quotes are the same quote. What cannot vary
is the words and the values themselves.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Unicode dash variants collapsed to a plain hyphen.
_DASHES_RE = re.compile(r"[‐‑‒–—―−]")

# Curly and angled quote variants collapsed to straight quotes.
_SINGLE_QUOTES_RE = re.compile(r"[‘’‚‛‹›]")
_DOUBLE_QUOTES_RE = re.compile(r"[“”„‟«»]")

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

_WORD_NUM_RE = re.compile(
    r"\b(" + "|".join(list(_TENS) + list(_UNITS)) + r")"
    r"(?:[-\s](" + "|".join(_UNITS) + r"))?\b"
)


def _words_to_digits(text: str) -> str:
    """Rewrite spelt-out numbers (zero..ninety-nine) as digits."""
    def repl(m: re.Match) -> str:
        first, second = m.group(1), m.group(2)
        if first in _TENS:
            value = _TENS[first] + (_UNITS.get(second, 0) if second else 0)
        else:
            # A unit word followed by another unit word is two numbers
            # ("four five"), not a compound — only tens compound.
            if second is not None:
                return f"{_UNITS[first]} {_UNITS[second]}"
            value = _UNITS[first]
        return str(value)
    return _WORD_NUM_RE.sub(repl, text)


def normalise(text: str) -> str:
    """Canonical form used for all quote and number comparisons.

    Loose on surface form (case, whitespace, dash and quote variants, percent
    spellings, spelt-out numbers), strict on substance.
    """
    t = (text or "").lower()
    t = _DASHES_RE.sub("-", t)
    t = _SINGLE_QUOTES_RE.sub("'", t)
    t = _DOUBLE_QUOTES_RE.sub('"', t)
    t = re.sub(r"\bper[\s-]?cent(age)?\b", r"percent\1", t)
    t = t.replace("%", " percent")
    t = _words_to_digits(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------------------
# Quote matching
# ---------------------------------------------------------------------------

def quote_matches(quote: str, source_text: str) -> bool:
    """True when the quote appears in the source text, after normalisation.

    An elided quote ("start ... end") matches when every segment appears in
    order. Empty quotes never match.
    """
    if not quote or not source_text:
        return False
    src = normalise(source_text)
    segments = [normalise(s) for s in re.split(r"\.{3,}|…", quote)]
    segments = [s for s in segments if s]
    if not segments:
        return False
    pos = 0
    for seg in segments:
        idx = src.find(seg, pos)
        if idx < 0:
            return False
        pos = idx + len(seg)
    return True


# ---------------------------------------------------------------------------
# Number extraction and grounding
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

# Markdown links and bare URLs, removed before number extraction so a URL's
# own digits are never mistaken for figures the sentence asserts.
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*https?://[^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")


def strip_links(text: str) -> str:
    """Replace markdown links with their label and drop bare URLs."""
    t = _MD_LINK_RE.sub(r"\1", text or "")
    return _BARE_URL_RE.sub("", t)


def extract_numbers(text: str) -> set[str]:
    """Canonical numeric tokens in `text` ("1,200" → "1200", "18.50" → "18.5")."""
    out: set[str] = set()
    for tok in _NUMBER_RE.findall(normalise(text)):
        tok = tok.replace(",", "")
        if "." in tok:
            tok = tok.rstrip("0").rstrip(".")
        out.add(tok)
    return out


def ungrounded_numbers(sentence: str, quote: str) -> list[str]:
    """Numbers asserted in the sentence that the quote does not contain.

    The sentence's links are stripped first so URL digits don't count.
    """
    wanted = extract_numbers(strip_links(sentence))
    have = extract_numbers(quote)
    return sorted(wanted - have)


# ---------------------------------------------------------------------------
# Sentence / citation pairing
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\(\"'0-9])")


def sentences_citing(content: str) -> list[tuple[str, list[str]]]:
    """Split content into sentences and return those that cite a URL,
    as (sentence, [urls]) pairs."""
    out = []
    for sentence in _SENTENCE_SPLIT_RE.split(content or ""):
        urls = re.findall(r"https?://[^\s)\]]+", sentence)
        if urls:
            out.append((sentence, urls))
    return out


# ---------------------------------------------------------------------------
# Act-level check
# ---------------------------------------------------------------------------

def check_act_citations(content: str, citations: list[dict] | None,
                        source_text_for, title_for=None) -> dict:
    """Run the quote contract and number grounding over one act.

    `citations` is the act's structured list of {"url", "quote"} entries.
    `source_text_for(url)` returns the stored body text for a pooled source
    (snippet, excerpt, full text), "" when the source is pooled but has no
    stored body text, or None when the URL is not in the pool.
    `title_for(url)` returns the source's title (or None when unpooled);
    omitted means titles are not checked separately.

    Returns a report:
        {"citations": [{"url", "quote", "status", "ungrounded_numbers"}],
         "violations": int}

    Statuses:
        verified      — quote found in the source's stored body text
        title_only    — quote matches only the source's title; a headline
                        substantiates nothing, so this does not count as
                        sourced (it is also not fabrication, so no violation)
        mismatch      — stored text exists and the quote is not in it
        unverifiable  — pooled source with no stored text to check against
        unpooled      — cited URL is not in the pool (the URL layer handles it)
        missing_quote — citation entry with no quote
    Only "mismatch" counts as a violation: an agent attached source text the
    source does not contain. "unverifiable" is not proof of anything, so it
    never strips or penalises — it is surfaced for the moderator and
    opposition to weigh.

    When an act carries citations but no inline URL in its prose (some models
    cite only through the array), per-sentence pairing has nothing to pair,
    so number grounding runs at act level instead: every number in the act's
    prose must appear in the union of its quotes. A shortfall is reported as
    one extra row with status "ungrounded_act_numbers" and counts as a
    violation when the act has at least one verified quote.
    """
    report: list[dict] = []
    violations = 0
    cited_sentences = sentences_citing(content or "")

    for entry in citations or []:
        url = str(entry.get("url") or "").strip()
        quote = str(entry.get("quote") or "").strip()
        row: dict = {"url": url, "quote": quote, "status": "",
                     "ungrounded_numbers": []}
        src = source_text_for(url) if url else None
        title = title_for(url) if (title_for and url) else None
        if not quote:
            row["status"] = "missing_quote"
        elif src is None:
            row["status"] = "unpooled"
        elif quote_matches(quote, src):
            row["status"] = "verified"
        elif title and quote_matches(quote, title):
            row["status"] = "title_only"
        elif not src.strip():
            row["status"] = "unverifiable"
        else:
            row["status"] = "mismatch"
            violations += 1

        # Number grounding runs whenever there is a quote to ground against:
        # the sentence citing this URL may not assert numbers the quote lacks.
        if quote:
            from core.sources import normalise_url
            key = normalise_url(url)
            for sentence, urls in cited_sentences:
                if any(normalise_url(u) == key for u in urls):
                    missing = ungrounded_numbers(sentence, quote)
                    if missing:
                        row["ungrounded_numbers"] = sorted(
                            set(row["ungrounded_numbers"]) | set(missing)
                        )
            if row["ungrounded_numbers"] and row["status"] == "verified":
                violations += 1

        report.append(row)

    # Act-level number grounding for array-only citers (no inline URL in the
    # prose, so no sentence to pair): the act's numbers must appear in the
    # union of its quotes.
    quoted = [r for r in report if r["quote"]]
    if quoted and not cited_sentences:
        union: set[str] = set()
        for r in quoted:
            union |= extract_numbers(r["quote"])
        missing = sorted(extract_numbers(strip_links(content or "")) - union)
        if missing:
            report.append({
                "url": "(act)", "quote": "",
                "status": "ungrounded_act_numbers",
                "ungrounded_numbers": missing,
            })
            if any(r["status"] == "verified" for r in quoted):
                violations += 1

    return {"citations": report, "violations": violations}
