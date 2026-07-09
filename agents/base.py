"""Base agent class for Agora debate participants."""
import os
import re
import json
import time
import uuid
from datetime import datetime
from core.state import Act, ActType, DialogueState

# Per-role allowlist of legal act types.  Any output outside this set is
# rejected before apply_act — catches cross-role injection and model errors.
_ALLOWED_ACT_TYPES: dict[str, frozenset] = {
    "proposition": frozenset({ActType.ASSERT, ActType.REVISE, ActType.DEFEND, ActType.PROPOSE}),
    "opposition":  frozenset({ActType.CHALLENGE, ActType.CONCEDE}),
    "moderator":   frozenset({ActType.STATUS, ActType.CLOSE, ActType.MODERATOR_INTERVENTION}),
    "synthesiser": frozenset({ActType.ARGUMENT_MAP}),
}

# Tags that could confuse section-boundary parsing if injected into debate content.
_INJECTION_TAG_RE = re.compile(
    r'</?(?:system|instruction|prompt|agora_data|debate_data|user)[^>]{0,80}>',
    re.IGNORECASE,
)

# How many recent acts to include verbatim in the prompt history window.
_HISTORY_WINDOW = 10

# LLM call retry settings (runs inside thread-pool executor, so time.sleep is safe).
_MAX_ATTEMPTS = 3
_BACKOFF_BASE  = 5   # seconds; attempt n waits _BACKOFF_BASE * 2^n


class BaseAgent:
    """Abstract base for all debate agents. Handles LLM dispatch and Act parsing."""

    def __init__(self, role: str, nickname: str, model: str, temperature: float, config: dict):
        self.role = role
        self.nickname = nickname
        self.model = model
        self.temperature = temperature
        self.config = config
        self._provider = "anthropic" if model.startswith("claude") else "openai"
        self._temperature_deprecated = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate(self, state: DialogueState) -> Act:
        """Build prompt, call LLM, parse and validate response into Act."""
        system, user = self._build_prompt(state)
        if self._provider == "anthropic":
            raw, input_tok, output_tok = self._call_anthropic(system, user)
        else:
            raw, input_tok, output_tok = self._call_openai(system, user)
        return self._parse_response(raw, state, input_tok, output_tok)

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    def _build_prompt(self, state: DialogueState) -> tuple[str, str]:
        """Return (system_prompt, user_message). Must be overridden in every subclass."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared utilities
    # ------------------------------------------------------------------

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
                f"[Turn {act.turn}] {act.agent} ({act.agent_role}) — "
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
            session_id=state.session_id,
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
    # LLM calls with retry + exponential backoff
    # ------------------------------------------------------------------

    def _call_anthropic(self, system: str, user: str) -> tuple[str, int, int]:
        """Call Anthropic Messages API. Retries on rate-limit and overload (runs in thread pool)."""
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        last_exc: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                kwargs: dict = {
                    "model": self.model,
                    "max_tokens": 2048,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
                if not self._temperature_deprecated:
                    kwargs["temperature"] = self.temperature

                response = client.messages.create(**kwargs)
                text = next(b.text for b in response.content if b.type == "text")
                return text, response.usage.input_tokens, response.usage.output_tokens

            except anthropic.BadRequestError as e:
                if "temperature" in str(e) and not self._temperature_deprecated:
                    self._temperature_deprecated = True
                    last_exc = e
                    continue  # retry immediately without temperature
                raise

            except anthropic.RateLimitError as e:
                last_exc = e
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))

            except anthropic.APIStatusError as e:
                if e.status_code in (529, 500, 502, 503) and attempt < _MAX_ATTEMPTS - 1:
                    last_exc = e
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

        assert last_exc is not None
        raise last_exc

    def _call_openai(self, system: str, user: str) -> tuple[str, int, int]:
        """Call OpenAI Chat Completions API. Retries on rate-limit and server errors."""
        from openai import OpenAI, RateLimitError, APIStatusError
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        last_exc: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                content = response.choices[0].message.content
                usage = response.usage
                return content, usage.prompt_tokens, usage.completion_tokens

            except RateLimitError as e:
                last_exc = e
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))

            except APIStatusError as e:
                if e.status_code in (500, 502, 503) and attempt < _MAX_ATTEMPTS - 1:
                    last_exc = e
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))
                else:
                    raise

        assert last_exc is not None
        raise last_exc
