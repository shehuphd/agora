"""Base agent class for Agora debate participants."""
import os
import re
import json
import uuid
from datetime import datetime
from traceact import ActionTrace
from core.state import Act, ActType, DialogueState
from providers.base import QuotaExhaustedError  # re-exported so runners can import from here

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
    "acceptable; an invented citation is not."
)

_KEY_ENV_MAP = {
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}


class BaseAgent:
    """Abstract base for all debate agents. Handles LLM dispatch and Act parsing."""

    # Only the two debating agents search the web. Moderator and Synthesiser read
    # the pool the debaters filled — they never need their own retrieval.
    RETRIEVES = False

    def __init__(self, role: str, nickname: str, model: str, temperature: float, config: dict):
        self.role = role
        self.nickname = nickname
        self.model = model
        self.temperature = temperature
        self.config = config
        if model.startswith("claude"):
            self._provider = "anthropic"
        elif model.startswith("gemini"):
            self._provider = "google"
        elif model.startswith("sonar") or model == "r1-1776":
            self._provider = "perplexity"
        else:
            self._provider = "openai"
        # Look up endpoint_type from the provider_models DB; adapters use this to
        # pick Chat Completions vs Responses API vs generate_content, etc.
        self._endpoint_type = self._resolve_endpoint_type()

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

        Returns (input_tokens, output_tokens) so retrieval stays inside the run's
        token budget. Never raises: a failed search leaves the pool as-is and the
        agent argues from what is already there.
        """
        from core.sources import Source
        from providers import research as _research

        query = self._research_query(state)
        if not query:
            return 0, 0
        try:
            key = os.environ[_KEY_ENV_MAP[self._provider]]
            _findings, sources, in_tok, out_tok = _research(
                self._provider, key, self.model, query,
            )
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            print(f"[sources] retrieval failed ({self.role}): {exc}", flush=True)
            return 0, 0

        pool.add_many([
            Source(
                url=s.url, title=s.title, snippet=s.snippet,
                published=getattr(s, "published", ""),
                provider=self._provider, harvested_by=self.role,
                query=query, turn=state.turn,
            )
            for s in sources
        ])
        return in_tok, out_tok

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
        """Open a trace, call the provider, parse via _parse_result, retry once on JSON failure."""
        with ActionTrace.start(
            action="agent.generate",
            kind="model",
            actor=self.role,
            project="agora",
            correlation_id=state.run_id,
            meta={"model": self.model, "turn": state.turn},
        ) as trace:
            if extra_in or extra_out:
                trace.step(f"retrieve: {extra_in}+{extra_out} tokens, pool={len(pool) if pool else 0}")
            raw, input_tok, output_tok = self._call_provider(system, user)
            # Retrieval is billed to this turn so run token budgets stay honest.
            input_tok += extra_in
            output_tok += extra_out
            trace.model(operation="completion", target=self.model, tokens_in=input_tok, tokens_out=output_tok)
            try:
                act = self._parse_result(raw, state, input_tok, output_tok)
                trace.step(f"parse: {act.act_type}")
                trace.output({"act_type": act.act_type})
                self._enforce_citations(trace, act, pool)
                self._trace_cite(trace, act)
                return act
            except json.JSONDecodeError:
                trace.step("parse failed — retrying with correction prompt")
                fix_user = (
                    f"Your previous response was not valid JSON. Here is what you returned:\n\n"
                    f"{raw}\n\n"
                    f"Return ONLY the corrected JSON object. No prose, no markdown fences, no other text."
                )
                raw2, i2, o2 = self._call_provider(system, fix_user)
                trace.model(operation="completion", target=self.model, tokens_in=i2, tokens_out=o2)
                act2 = self._parse_result(raw2, state, input_tok + i2, output_tok + o2)
                trace.step(f"parse retry: {act2.act_type}")
                trace.output({"act_type": act2.act_type})
                self._enforce_citations(trace, act2, pool)
                self._trace_cite(trace, act2)
                return act2

    def _enforce_citations(self, trace, act, pool) -> None:
        """Strip any URL the agent wrote that no search engine actually returned.

        This is the assertion that closes the loop. Constraining the prompt to the
        pool makes fabrication unlikely; checking membership afterwards makes it
        ineffective. HTTP status deliberately plays no part — publishers such as
        autonomy.work answer 403 to bots, and a live-but-blocked URL is still a
        real source, while a soft-404 page returns 200 and is not.
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
        was a poor fabrication signal anyway (bot-blocked 403s are real sources,
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
        )

    # ------------------------------------------------------------------
    # Provider dispatch — all LLM calls go through here
    # ------------------------------------------------------------------

    def _resolve_endpoint_type(self) -> str:
        """Look up endpoint_type for this model from the provider_models DB.

        Falls back to 'default' if the DB is empty or the model isn't listed yet
        (e.g. first run before any key has been tested).
        """
        try:
            from core.runs_db import connect as _db_connect
            conn = _db_connect()
            row = conn.execute(
                "SELECT endpoint_type FROM provider_models WHERE provider=? AND model_id=?",
                (self._provider, self.model),
            ).fetchone()
            conn.close()
            return (row["endpoint_type"] or "default") if row else "default"
        except Exception:
            return "default"

    def _call_provider(self, system: str, user: str) -> tuple[str, int, int]:
        """Route inference to the correct provider adapter via the central router."""
        from providers import generate as _router_generate
        key_env = _KEY_ENV_MAP.get(self._provider)
        if not key_env:
            raise ValueError(f"No key env mapping for provider '{self._provider}'")
        key = os.environ[key_env]
        return _router_generate(
            self._provider, key, self.model, self._endpoint_type,
            system, user, self.temperature,
        )
