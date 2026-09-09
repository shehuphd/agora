"""Base agent class for Agora debate participants."""
import os
import re
import json
import time
import uuid
from datetime import datetime
from traceact import ActionTrace
from core.state import Act, ActType, DialogueState
from core import cost as _cost
from keycall import KeyCallError  # re-exported so runners can import from here

# Per-role allowlist of legal act types.  Any output outside this set is
# rejected before apply_act — catches cross-role injection and model errors.
_ALLOWED_ACT_TYPES: dict[str, frozenset] = {
    "proposition": frozenset({ActType.ASSERT, ActType.REVISE, ActType.DEFEND, ActType.PROPOSE}),
    "opposition":  frozenset({ActType.CHALLENGE, ActType.CONCEDE}),
    "moderator":   frozenset({ActType.STATUS, ActType.CLOSE, ActType.MODERATOR_INTERVENTION}),
    "synthesiser": frozenset({ActType.ARGUMENT_MAP}),
}

# Matches http/https URLs in agent content for citation extraction.
# Trailing markdown/sentence punctuation is excluded so links lifted out of
# "[Name](https://x)." don't carry ")." into the validation request.
_URL_RE = re.compile(r'https?://[^\s<>\]]+[^\s<>\]).,;:!?\'"]')

# Tags that could confuse section-boundary parsing if injected into debate content.
_INJECTION_TAG_RE = re.compile(
    r'</?(?:system|instruction|prompt|agora_data|debate_data|user)[^>]{0,80}>',
    re.IGNORECASE,
)

# How many recent acts to include verbatim in the prompt history window.
# Older acts are replaced by a compaction summary; claim state is always in the dialogue_state JSON.
_HISTORY_WINDOW = 6

# Total attempts _traced_generate makes at getting a valid, substantive
# response: the original generation plus this many correction re-prompts.
# The last attempt uses a stricter correction prompt (see _correction_prompt)
# that spells out the two JSON failure modes seen in production (unterminated
# strings, unescaped control characters from quoted source text) rather than
# just "that wasn't JSON."
_MAX_PARSE_ATTEMPTS = 3


class AgentResponseError(Exception):
    """A model failed to produce a usable response after every correction
    attempt — either the JSON never parsed, or it parsed but the content
    itself was invalid. Distinct from a bare exception so the runner can
    treat this as pause-worthy rather than fatal: see
    TurnOrchestrator._call_with_pause_on_failure in runners/debate.py.
    """


class ResponseParseError(AgentResponseError):
    """The response never became valid JSON, even after every correction attempt."""


class InvalidContentError(AgentResponseError):
    """The response was syntactically valid JSON with a legal act_type, but
    its content fails a validity check — e.g. the model falsely claims the
    prompt omitted context that was always included. Legality (is this act
    type allowed for this role) and validity (is this a substantive
    response) are checked separately; this is the second one."""


# Phrases indicating a model is denying it received context (the debate
# topic, dialogue state, or evidence pool) that _build_prompt always
# includes. Observed across four different providers and two different
# roles in production — Moonshot's kimi-k3 in proposition, and Anthropic's
# claude-sonnet-5, OpenAI's gpt-4.1, and Perplexity's sonar-reasoning-pro all
# independently in the synthesiser role — so this is checked for every role,
# not just the ones it was first caught on. Kept as literal phrases rather
# than a broader heuristic: false positives here silently discard a
# legitimate argument, so precision counts for more than recall.
_MISSING_CONTEXT_MARKERS = (
    "were not included in this turn", "was not supplied", "please resupply",
    "nothing honest to assert", "no debate topic", "no debate record",
    "cannot identify the motion", "were not provided", "was not provided in this",
    "not included with this turn",
)


def _claims_missing_context(content: str) -> bool:
    low = (content or "").lower()
    return any(marker in low for marker in _MISSING_CONTEXT_MARKERS)


# Every correction prompt is APPENDED to the original user message, never
# substituted for it. Replacing the user message was the root cause of the
# "context wasn't supplied" acts found in the 2026-08-30 investigation: a
# reasoning model whose first call ran out of completion budget mid-reasoning
# returned empty text, the old replacement-style retry then carried no topic,
# dialogue state, or evidence pool at all, and the model's honest report of
# that absence was recorded as an ordinary debate act. The retry must see
# everything the original call saw, plus the correction.

def _correction_prompt(user: str, raw: str, exc: json.JSONDecodeError, strict: bool) -> str:
    returned = f"Here is what you returned:\n\n{raw[:2000]}\n\n" if raw.strip() else \
        "Your response was empty.\n\n"
    if not strict:
        return (
            f"{user}\n\n---\n"
            f"CORRECTION: your previous response to the message above was not valid JSON. "
            f"{returned}"
            f"Return ONLY the corrected JSON object, answering the original request above. "
            f"No prose, no markdown fences, no other text."
        )
    return (
        f"{user}\n\n---\n"
        f"CORRECTION: your last response to the message above was still not valid JSON — "
        f"the parser said: {exc}.\n\n{returned}"
        f"This is your final attempt. Return ONLY a single valid JSON object answering the "
        f"original request above. Every string must be on one line: replace any literal "
        f"newline inside a string with \\n, and escape every double-quote inside a string "
        f"as \\\". No markdown fences, no commentary, nothing before or after the JSON object."
    )


def _missing_context_correction_prompt(user: str, content: str) -> str:
    return (
        f"{user}\n\n---\n"
        f"CORRECTION: your previous response claimed required context (the debate topic, "
        f"dialogue state, or evidence pool) was missing or not supplied:\n\n{content[:1000]}\n\n"
        f"This is incorrect — the debate topic, dialogue state, and evidence pool are all in "
        f"the message above. Re-read it and respond with a substantive act for your "
        f"role. Do not repeat a claim that context is missing."
    )


def _citation_repair_prompt(user: str, raw: str, mismatches: list) -> str:
    lines = []
    for row in mismatches:
        quote = str(row.get("quote") or "")[:300]
        lines.append(f'- {row.get("url")}: your quote "{quote}" does not appear '
                     f"in that source's stored text.")
    failures = "\n".join(lines)
    return (
        f"{user}\n\n---\n"
        f"CORRECTION: your previous response attributed quotes to sources that "
        f"don't contain them:\n\n{failures}\n\n"
        f"Your previous response:\n{raw[:3000]}\n\n"
        f"Resubmit the same act with each failing citation fixed: replace the "
        f"quote with a verbatim passage from that source, or drop the citation "
        f"and any claim that depended on it. Change nothing else."
    )


def set_history_window(n: int) -> None:
    """Update the module-level history window at runtime (called by settings save)."""
    global _HISTORY_WINDOW
    _HISTORY_WINDOW = max(2, min(10, int(n)))

# Appended to every agent's system prompt. The pool is the only legal source of
# URLs, which is what makes a fabricated link impossible rather than discouraged.
_CITATION_CONTRACT = (
    "CITATION CONTRACT\n"
    "Every URL you write MUST be copied verbatim from the EVIDENCE POOL supplied "
    "in this message. You have no other means of knowing whether a URL exists.\n"
    "Writing a URL that is not in the pool is a protocol violation: it will be "
    "detected and stripped, and your act will be marked unsourced.\n"
    "If the pool holds nothing that supports the point you want to make, say so "
    "plainly and argue from reasoning instead. An honest unsourced argument is "
    "acceptable; an invented citation is not.\n"
    "\n"
    "QUOTE CONTRACT\n"
    "Every empirical figure or attributed position you cite MUST also appear in "
    "your JSON's \"citations\" array as {\"url\": ..., \"quote\": ...}: the URL, "
    "plus a verbatim passage of at most 50 words copied word for word from that "
    "source's description or excerpt text as shown in the pool. The source's "
    "headline does not count: a quote matching only the title is marked "
    "title_only and the citation is treated as unsourced, because a headline "
    "substantiates nothing. Do not paraphrase inside the quote and do not "
    "stitch separate passages together (an ellipsis for an elision within one "
    "passage is fine).\n"
    "The quote is checked mechanically against the source's stored text: a "
    "quote the source does not contain marks the citation unsourced, same as a "
    "fabricated URL. Additionally, every number in a sentence that cites a "
    "source must appear inside that citation's quote — numerals and spelt-out "
    "forms both count (\"18%\" matches \"eighteen per cent\") — so keep each "
    "figure next to the passage it came from, and never attach a figure to a "
    "source whose text does not state it."
)

def _key_env(provider: str) -> str:
    """Env var holding this provider's key, per the adapter that defines it.

    Asked of the provider registry rather than kept as a second list here —
    a parallel copy silently omits any newly registered provider, and the
    failure looks like a missing key rather than a missing entry.
    """
    from providers import get_key_env
    return get_key_env(provider)


class BaseAgent:
    """Abstract base for all debate agents. Handles LLM dispatch and Act parsing."""

    # Only the two debating agents search the web. Moderator and Synthesiser read
    # the pool the debaters filled — they never need their own retrieval.
    RETRIEVES = False

    def __init__(self, role: str, nickname: str, model: str, temperature: float,
                 config: dict, provider: str):
        self.role = role
        self.nickname = nickname
        self.model = model
        self.temperature = temperature
        self.config = config
        # Told, not derived. Routing is resolved once against the registry when
        # the debate is created and carried in the run config, so an agent has
        # no lookup to do and a mid-run registry change cannot silently
        # redirect a call to a different vendor than the run recorded.
        if not provider:
            raise ValueError(
                f"{role} agent needs a provider for model '{model}'. Routing is "
                f"resolved at debate creation via runs_db.resolve_model."
            )
        self._provider = provider

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate(self, state: DialogueState) -> Act:
        """Retrieve (if this agent searches), then compose against the evidence pool.

        Retrieval and composition are deliberately separate calls. Enabling search
        on the composing call makes the model interleave tool blocks and preamble
        with its JSON, which breaks parsing — and the repair path then re-prompts
        without the search results, so the model reconstructs URLs from memory.
        That is precisely how fabricated links get in.
        """
        research_in = research_out = 0
        if self.RETRIEVES:
            from core.sources import get_pool
            research_in, research_out = self._retrieve(state, get_pool(state.run_id))

        system, user = self._build_prompt(state)
        return self._compose_with_pool(
            state, system, user, extra_in=research_in, extra_out=research_out,
        )

    def _compose_with_pool(self, state: DialogueState, system: str, user: str,
                           extra_in: int = 0, extra_out: int = 0) -> Act:
        """The single chokepoint every citable act must pass through.

        Injects the evidence pool and citation contract, then generates with
        enforcement attached. Agents that override generate() (e.g. Moderator's
        extra kwargs) MUST route their call through here — composing via
        _traced_generate directly would skip pool injection and let a fabricated
        URL through unchecked.
        """
        from core.sources import get_pool
        pool = get_pool(state.run_id)
        user = f"{user}\n\n{pool.as_prompt_block()}"
        system = f"{system}\n\n{_CITATION_CONTRACT}"
        return self._traced_generate(
            state, system, user, pool=pool,
            extra_in=extra_in, extra_out=extra_out,
        )

    def _retrieve(self, state: DialogueState, pool) -> tuple[int, int]:
        """Search the web for this turn and pool whatever comes back.

        Neutral search first (SearXNG → Serper): the model writes the query
        (~300 tokens), a search service executes it at flat cost, and the SERP
        never touches the context window. Only when no neutral tier is up does
        this fall back to token-billed provider search.

        Returns (input_tokens, output_tokens) so retrieval stays inside the run's
        token budget. Never raises (except quota): a failed search leaves the
        pool as-is and the agent argues from what is already there.
        """
        from core import search as _search
        from core.sources import Source

        query, q_in, q_out = self._write_search_query(state)
        if not query:
            return 0, 0

        provider_label = self._provider
        in_tok, out_tok = q_in, q_out
        run_dir = getattr(pool, "_path", None)
        run_dir = run_dir.parent if run_dir else None

        tier, sources = _search.search(query, max_results=8, run_dir=run_dir)
        if tier != "none":
            provider_label = tier
        else:
            # Token-billed vendor search — the expensive path. active_tier()
            # already reports 'provider' so the UI can warn before this happens.
            from providers import research as _research
            try:
                key = os.environ[_key_env(self._provider)]
                _findings, sources, r_in, r_out = _research(
                    self._provider, key, self.model, query,
                )
                in_tok += r_in
                out_tok += r_out
            except KeyCallError as e:
                if e.code.name == "PERMISSION_DENIED":
                    raise
                print(f"[sources] retrieval failed ({self.role}): {e.code.name} — {e.message}", flush=True)
                return in_tok, out_tok
            except Exception as exc:
                print(f"[sources] retrieval failed ({self.role}): {exc}", flush=True)
                return in_tok, out_tok

        _search.enrich_sources(sources, sample_k=3)

        pool.add_many([
            Source(
                url=s.url, title=s.title, snippet=s.snippet,
                published=getattr(s, "published", ""),
                excerpt=getattr(s, "excerpt", ""),
                provider=provider_label, harvested_by=self.role,
                query=query, turn=state.turn,
            )
            for s in sources
        ])
        return in_tok, out_tok

    def _write_search_query(self, state: DialogueState) -> tuple[str, int, int]:
        """Have this agent's own model write the search query.

        Query formulation is the model skill that merits measuring once execution is
        neutral — a tiny call (~300 tokens) preserves it as an experimental
        variable. Falls back to the mechanical query on any failure.

        Returns (query, input_tokens, output_tokens).
        """
        mechanical = self._research_query(state)
        try:
            raw, q_in, q_out = self._call_provider(
                "You write web search queries. Reply with ONE query of at most "
                "12 words. No quotes, no prose, no explanation.",
                f"Find evidence for your side of this debate.\n{mechanical[:600]}",
                max_tokens=40,
            )
            query = (raw or "").strip().strip('"').splitlines()[0][:140]
            return (query or mechanical), q_in, q_out
        except KeyCallError as e:
            if e.code.name == "PERMISSION_DENIED":
                raise
            return mechanical, 0, 0
        except Exception:
            return mechanical, 0, 0

    def _research_query(self, state: DialogueState) -> str:
        """What this agent should go and look up before speaking.

        Includes the opposing debater's most recent act so a CHALLENGE can hunt
        for counter-evidence. Filtered to the opposing debater specifically —
        turn order is debater → moderator → debater, so "the last act that isn't
        mine" is almost always the moderator's STATUS summary, which is the
        wrong thing to research against.
        """
        opponent = "opposition" if self.role == "proposition" else "proposition"
        parts = [state.topic]
        recent = [a for a in state.acts if a.agent_role == opponent]
        if recent:
            parts.append(f"Specifically address this claim: {recent[-1].content[:400]}")
        return " ".join(parts)

    def _parse_result(self, raw: str, state: DialogueState, input_tok: int, output_tok: int) -> Act:
        """Parse LLM response into Act. Override in subclasses to use a role-specific parser."""
        return self._parse_response(raw, state, input_tok, output_tok)

    def _traced_generate(self, state: DialogueState, system: str, user: str,
                         pool=None, extra_in: int = 0, extra_out: int = 0) -> Act:
        """Open a trace, call the provider, parse via _parse_result.

        Two checks gate a successful return, both retried up to
        _MAX_PARSE_ATTEMPTS total attempts before giving up: legality (does
        this parse as JSON with an act_type this role may emit — the
        original check) and validity (is the content substantive,
        not a false claim that required context was missing — see
        _claims_missing_context). The final JSON-repair attempt uses a
        stricter correction prompt; a content-validity failure gets a prompt
        pointing out the context was in fact provided. If every attempt
        fails either check, raises ResponseParseError or InvalidContentError
        rather than letting the failure escape silently — the runner treats
        both as pause-worthy, not fatal.
        """
        with ActionTrace.start(
            action="agent.generate",
            kind="model",
            actor=self.role,
            project="agora",
            correlation_id=state.run_id,
            meta={"model": self.model, "turn": state.turn},
        ) as trace:
            # Full prompt bodies, so a trace answers "what did the model see"
            # without replaying the run. Rotation (50MB) bounds the cost.
            trace.input({"system": system, "user": user})
            if extra_in or extra_out:
                trace.step(f"retrieve: {extra_in}+{extra_out} tokens, pool={len(pool) if pool else 0}")
            t0 = time.perf_counter()
            raw, input_tok, output_tok = self._call_provider(system, user)
            # Retrieval is billed to this turn so run token budgets stay honest.
            input_tok += extra_in
            output_tok += extra_out
            # Per-attempt call figures for the model event (the loop below keeps
            # input_tok/output_tok as running totals for act billing). call_in and
            # call_out are the tokens of the one call whose output is being judged
            # this iteration; call_dur is that call's wall time.
            call_in, call_out = input_tok, output_tok
            call_dur = (time.perf_counter() - t0) * 1000.0

            attempt = 1
            repairs = 0
            last_raw = raw
            # Reason the current attempt was launched (what the previous one hit).
            # None for the first attempt; set before each retry. Recorded as
            # traceact's attempt convention so the viewer groups the retried
            # calls into one sequence, each keeping its own cost and timeline bar.
            launch_reason: str | None = None

            def _emit_call(status: str | None = None) -> None:
                # A clean first attempt carries no attempt number, so a normal
                # turn stays a single plain model event; only a call that is part
                # of a retry sequence (this one failed, or a later attempt) is
                # tagged, so the viewer collapses only actual retries into ×N.
                kw: dict = {
                    "operation": "completion", "target": self.model,
                    "provider": self._provider, "tokens_in": call_in,
                    "tokens_out": call_out, "duration_ms": round(call_dur, 1),
                }
                if status == "failed" or attempt > 1:
                    kw["attempt"] = attempt
                    if launch_reason:
                        kw["attempt_reason"] = launch_reason
                if status:
                    kw["status"] = status
                trace.model(**kw)

            def _retry(fix_user: str) -> tuple[str, int, int, float]:
                start = time.perf_counter()
                r, i, o = self._call_provider(system, fix_user)
                return r, i, o, (time.perf_counter() - start) * 1000.0

            while True:
                try:
                    act = self._parse_result(last_raw, state, input_tok, output_tok)
                except json.JSONDecodeError as exc:
                    _emit_call(status="failed")
                    if attempt >= _MAX_PARSE_ATTEMPTS:
                        trace.step(f"parse failed permanently after {attempt} attempts")
                        trace.output({"error": str(exc), "raw_preview": last_raw[:300]})
                        raise ResponseParseError(
                            f"{self.role} ({self.model}) returned invalid JSON "
                            f"{attempt} times in a row: {exc}"
                        ) from exc
                    strict = attempt >= _MAX_PARSE_ATTEMPTS - 1
                    trace.step(f"parse failed — retrying with correction prompt (attempt {attempt + 1}, strict={strict})")
                    fix_user = _correction_prompt(user, last_raw, exc, strict=strict)
                    last_raw, i2, o2, call_dur = _retry(fix_user)
                    input_tok += i2
                    output_tok += o2
                    call_in, call_out = i2, o2
                    launch_reason = "invalid JSON"
                    attempt += 1
                    continue

                if _claims_missing_context(act.content):
                    _emit_call(status="failed")
                    if attempt >= _MAX_PARSE_ATTEMPTS:
                        trace.step(f"content invalid permanently after {attempt} attempts")
                        trace.output({"error": "claims_missing_context", "content_preview": act.content[:300]})
                        raise InvalidContentError(
                            f"{self.role} ({self.model}) claimed required context was missing "
                            f"{attempt} times in a row — the prompt always includes it"
                        )
                    trace.step(f"content invalid (claims missing context) — retrying (attempt {attempt + 1})")
                    fix_user = _missing_context_correction_prompt(user, act.content)
                    last_raw, i2, o2, call_dur = _retry(fix_user)
                    input_tok += i2
                    output_tok += o2
                    call_in, call_out = i2, o2
                    launch_reason = "claimed context missing"
                    attempt += 1
                    continue

                label = "parse" if attempt == 1 else f"parse retry {attempt - 1}"
                trace.step(f"{label}: {act.act_type}")
                trace.output({"act_type": act.act_type, "response_raw": last_raw})
                self._enforce_citations(trace, act, pool)
                mismatches = self._enforce_quotes(trace, act, pool)
                if mismatches and repairs == 0:
                    # One corrective retry per act: a mismatched quote left in
                    # the record poisons every downstream reader, so the model
                    # gets a single chance to fix or drop it before the act is
                    # recorded (annotated) as a violation.
                    _emit_call(status="failed")
                    repairs = 1
                    trace.step(f"quote.repair: {len(mismatches)} mismatched "
                               f"quote(s) — one corrective retry")
                    fix_user = _citation_repair_prompt(user, last_raw, mismatches)
                    last_raw, i2, o2, call_dur = _retry(fix_user)
                    input_tok += i2
                    output_tok += o2
                    call_in, call_out = i2, o2
                    launch_reason = "quote not in source"
                    attempt += 1
                    continue
                _emit_call()
                # The repair attempt re-enters the parse loop, so it is
                # subtracted here to keep retries a pure parse-retry count.
                act.retries = attempt - 1 - repairs
                act.citation_repairs = repairs
                self._trace_cite(trace, act)
                return act

    def _enforce_citations(self, trace, act, pool) -> None:
        """Strip any URL the agent wrote that no search engine returned.

        This is the assertion that closes the loop. Constraining the prompt to the
        pool makes fabrication unlikely; checking membership afterwards makes it
        ineffective. HTTP status deliberately plays no part — publishers such as
        autonomy.work answer 403 to bots, and a live-but-blocked URL is still a
        legitimate source, while a soft-404 page returns 200 and is not.
        """
        if pool is None or not act.content:
            return
        in_pool, fabricated = pool.verify_citations(act.content)
        trace.step(f"cite.check: {len(in_pool)} pooled, {len(fabricated)} fabricated")
        if not fabricated:
            return

        cleaned = act.content
        for url in fabricated:
            # Collapse "[Label](bad-url)" to "Label [unverified source removed]".
            cleaned = re.sub(
                r'\[([^\]]*)\]\(\s*' + re.escape(url) + r'\s*\)',
                r'\1 [unverified source removed]',
                cleaned,
            )
            cleaned = cleaned.replace(url, "[unverified source removed]")
        act.content = cleaned

        trace.step(f"cite.fabricated: {', '.join(u[:70] for u in fabricated[:3])}")
        trace.output({"fabricated_urls": fabricated, "pooled_urls": in_pool})
        print(f"[sources] stripped {len(fabricated)} fabricated URL(s) from "
              f"{self.role} turn {act.turn}", flush=True)

    def _enforce_quotes(self, trace, act, pool) -> list:
        """The quote contract: the URL check, one level up.

        Every structured citation the act carries is checked mechanically —
        the quote must appear as a substring of the stored source text
        (normalised: case, whitespace, dash and quote variants, "18%" vs
        "eighteen per cent"), and every number in a sentence citing that URL
        must appear inside the quote. On the first citation of a source its
        full text is fetched once (one HTTP call, zero tokens) so there is
        something to check against.

        Statuses are recorded on act.citations for the record, the moderator,
        and the opposition. A "mismatch" — stored text exists and the quote is
        not in it — is a violation by construction and is flagged in the
        act's content the way a fabricated URL is. "unverifiable" (a
        bot-walled source with no stored text) proves nothing and is only
        surfaced, never penalised.

        Returns the mismatch rows so the caller can trigger a corrective
        retry before the act enters the record.
        """
        if pool is None or not act.citations:
            return []
        from core.citations import check_act_citations

        urls = [str(c.get("url") or "") for c in act.citations if c.get("url")]
        if urls:
            try:
                pool.ensure_full_text(urls)
            except Exception as exc:
                trace.step(f"quote.fetch failed: {exc}")

        report = check_act_citations(act.content, act.citations,
                                     pool.source_text_for, pool.source_title_for)
        act.citations = report["citations"]
        counts: dict[str, int] = {}
        for row in report["citations"]:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        trace.step("quote.check: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        trace.output({"citation_checks": report["citations"]})

        mismatches = [r for r in report["citations"] if r["status"] == "mismatch"]
        ungrounded = [r for r in report["citations"] if r["ungrounded_numbers"]]
        if mismatches:
            act.content += (
                "\n\n[citation check: "
                f"{len(mismatches)} quote(s) not found in the cited source's text — "
                "the affected citation(s) are unsourced]"
            )
        if mismatches or ungrounded:
            print(
                f"[sources] quote check failed for {self.role} turn {act.turn}: "
                f"{len(mismatches)} mismatched quote(s), "
                f"{len(ungrounded)} citation(s) with ungrounded numbers",
                flush=True,
            )
        return mismatches

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    def _build_prompt(self, state: DialogueState) -> tuple[str, str]:
        """Return (system_prompt, user_message). Must be overridden in every subclass."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared utilities
    # ------------------------------------------------------------------

    def _trace_cite(self, trace, act) -> None:
        """Log which URLs the act cites, plus a content preview.

        Observability only — no network calls. Reachability probing used to run
        here (5 URLs x 3s, inside the agent timeout); pool membership via
        _enforce_citations has superseded it as the actual guard, and HTTP status
        was a poor fabrication signal anyway (bot-blocked 403s are legitimate sources,
        soft-404s return 200).
        """
        if not act.content:
            return
        urls = _URL_RE.findall(act.content)[:8]
        trace.step(f"cite.extract: {len(urls)} URL(s) found")
        trace.output({"urls_found": urls, "content_preview": act.content[:300]})

    def _sanitize(self, text: str) -> str:
        """Strip structural tags that could shift section boundaries in the prompt."""
        return _INJECTION_TAG_RE.sub("", str(text or ""))

    def _format_turn_cards(self, state: DialogueState, limit: int | None = None) -> str:
        """Mechanical one-liner per act — the whole debate at ~25 tokens/turn.

        Built from structured fields, never by an LLM: zero cost, deterministic,
        cannot hallucinate the state it reports. Replaces the full transcript for
        auxiliary agents (moderator, synthesiser), whose input otherwise grows
        with the square of debate length.

        `limit` bounds the cards to the most recent acts, with a one-line note
        for what was cut; earlier turns are then carried by chapter summaries.
        The synthesiser's close-time map passes no limit and reads everything.
        """
        if not state.acts:
            return "(no acts yet)"
        acts = state.acts
        header = ""
        if limit is not None and len(acts) > limit:
            omitted = len(acts) - limit
            header = (
                f"[{omitted} earlier act(s) omitted — through turn "
                f"{acts[-limit - 1].turn}; see chapter summaries]\n"
            )
            acts = acts[-limit:]
        cards = []
        for a in acts:
            target = f"→{a.claim_id}" if a.claim_id else ""
            cards.append(
                f"T{a.turn} {a.agent_role} {a.act_type}{target}: "
                f"{self._sanitize(a.content)[:80]}"
            )
        return header + "\n".join(cards)

    # How many outstanding challenges appear in full in any prompt. Older ones
    # collapse to a count (and, in the opposition's audit, an aggregate line):
    # without a bound, every seat's prompt grows linearly with unresolved
    # challenges, which was the measured driver of per-turn token growth.
    _CHALLENGE_WINDOW = 10

    def _bounded_challenges(self, state: DialogueState) -> dict:
        """Bounded view of outstanding challenges for dialogue-state JSON:
        total count, the most recent _CHALLENGE_WINDOW act_ids in full, and
        how many lapsed. The full list lives in state; prompts get this."""
        ids = list(state.outstanding_challenges)
        return {
            "count": len(ids),
            "active": ids[-self._CHALLENGE_WINDOW:],
            "older_omitted": max(0, len(ids) - self._CHALLENGE_WINDOW),
            "lapsed_count": len(getattr(state, "lapsed_challenges", []) or []),
        }

    def _format_chapters(self, state: DialogueState) -> str:
        """Chapter summaries as a prompt block — the carrier for context that
        predates the act-history window. Empty string when there are none."""
        chapters = getattr(state, "chapters", None) or []
        if not chapters:
            return ""
        return (
            "<chapter_summaries>\n" + "\n\n".join(chapters) + "\n</chapter_summaries>\n"
        )

    @staticmethod
    def _strip_and_parse(raw: str) -> dict:
        """Strip optional markdown fences and parse JSON. Single source of truth for all agents."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            text = "\n".join(inner)
        return json.loads(text)

    def _format_act_history(self, state: DialogueState) -> str:
        """Format recent act log for the user message.

        Acts beyond _HISTORY_WINDOW are replaced by a one-line summary derived
        from dialogue state — the same compaction pattern used here in Claude Code.
        The full claim registry is always in dialogue_state above.
        """
        acts = state.acts
        if not acts:
            return "(no acts yet)"

        if len(acts) > _HISTORY_WINDOW:
            omitted = len(acts) - _HISTORY_WINDOW
            open_c = sum(1 for c in state.claims.values() if c.status == "open")
            challenged_c = sum(1 for c in state.claims.values() if c.status == "challenged")
            summary = (
                f"[{omitted} earlier act(s) omitted — "
                f"turns 0–{acts[-_HISTORY_WINDOW - 1].turn}. "
                f"State: {len(state.claims)} claims total, "
                f"{open_c} open, {challenged_c} challenged, "
                f"{len(state.outstanding_challenges)} unresolved. "
                f"Full claim registry in dialogue_state above.]\n"
            )
            acts = acts[-_HISTORY_WINDOW:]
        else:
            summary = ""

        lines = []
        for act in acts:
            lines.append(
                f"[Turn {act.turn} | act_id:{act.act_id}] {act.agent} ({act.agent_role}) — "
                f"{act.act_type}: {self._sanitize(act.content)}"
            )
            if act.reason:
                lines.append(f"  Reason: {self._sanitize(act.reason)}")
            # Quote/paraphrase pairs with their mechanical check results, so
            # the opposition can compare what a source says against what the
            # citing sentence claims it says.
            for c in getattr(act, "citations", None) or []:
                status = c.get("status") or "unchecked"
                note = f" — ungrounded numbers: {c['ungrounded_numbers']}" if c.get("ungrounded_numbers") else ""
                lines.append(
                    f"  Cited [{status}]{note}: {c.get('url', '')} — "
                    f"\"{self._sanitize(c.get('quote', ''))[:300]}\""
                )

        return summary + "\n".join(lines)

    def _format_claims(self, state: DialogueState) -> str:
        if not state.claims:
            return "(no claims yet)"
        lines = []
        for cid, claim in state.claims.items():
            lines.append(f"  [{cid}] ({claim.status}) {claim.author}: {self._sanitize(claim.content)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str, state: DialogueState, input_tokens: int, output_tokens: int) -> Act:
        """Parse JSON → Act with role-level allowlist validation."""
        data = self._strip_and_parse(raw)

        act_type_str = str(data.get("act_type", "")).upper()
        allowed = _ALLOWED_ACT_TYPES.get(self.role, frozenset())
        # In Rapoport/steelman mode, extend the allowlist with steelman acts.
        if getattr(state, "steelman_mode", False):
            if self.role == "opposition":
                allowed = allowed | frozenset({ActType.STEELMAN})
            elif self.role == "proposition":
                allowed = allowed | frozenset({ActType.ACCEPT_STEELMAN, ActType.REJECT_STEELMAN})
        if act_type_str not in allowed:
            raise ValueError(
                f"Role '{self.role}' emitted forbidden act_type '{act_type_str}'. "
                f"Allowed: {sorted(a.value for a in allowed)}. Possible injection or model error."
            )
        act_type = ActType(act_type_str)

        content = str(data.get("content", ""))
        if len(content) > 3000:
            content = content[:3000]

        # Opposition schema may use target_claim_id; normalise to claim_id.
        claim_id = data.get("claim_id") or data.get("target_claim_id")

        # Structured citations ({"url", "quote"} entries). Statuses are filled
        # in by _enforce_quotes after generation; malformed entries are dropped.
        raw_citations = data.get("citations")
        citations = None
        if isinstance(raw_citations, list):
            citations = [
                {"url": str(c.get("url") or ""), "quote": str(c.get("quote") or "")}
                for c in raw_citations if isinstance(c, dict)
            ] or None

        return Act(
            act_id=str(uuid.uuid4()),
            run_id=state.run_id,
            turn=state.turn,
            agent=self.nickname,
            agent_role=self.role,
            act_type=act_type,
            claim_id=claim_id,
            target_act_id=data.get("target_act_id"),
            content=content,
            reason=data.get("reason"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_used=self.model,
            timestamp=datetime.utcnow().isoformat(),
            challenge_type=(str(data["challenge_type"]).lower()
                            if data.get("challenge_type") else None),
            citations=citations,
            cost_usd=_cost.cost_usd(self._provider, self.model, input_tokens, output_tokens),
        )

    # ------------------------------------------------------------------
    # Provider dispatch — all LLM calls go through here
    # ------------------------------------------------------------------

    # Completion cap for composition calls. A cap is not a spend: non-reasoning
    # models answer these prompts in ~350-450 tokens regardless of the ceiling
    # (measured 2026-08-30, gpt-4.1 across 5 trials). It must clear a reasoning
    # model's hidden reasoning, which bills against the same budget: kimi-k3
    # spends 1,300-2,045+ reasoning tokens on a mid-debate proposition prompt
    # and returned EMPTY text 7 of 9 times under the old 2048 cap — the
    # reasoning consumed the whole allowance before a word of answer appeared.
    _MAX_COMPLETION_TOKENS = 8192

    def _call_provider(self, system: str, user: str, max_tokens: int | None = None) -> tuple[str, int, int]:
        """Route inference to the correct provider adapter via the central router."""
        if max_tokens is None:
            max_tokens = self._MAX_COMPLETION_TOKENS
        from providers import generate as _router_generate
        key = os.environ[_key_env(self._provider)]
        return _router_generate(
            self._provider, key, self.model,
            system, user, self.temperature, max_tokens,
        )
