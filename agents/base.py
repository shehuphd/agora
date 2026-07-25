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

_KEY_ENV_MAP = {
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}


class BaseAgent:
    """Abstract base for all debate agents. Handles LLM dispatch and Act parsing."""

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
        """Build prompt, call LLM via the central router, parse response into Act."""
        system, user = self._build_prompt(state)
        return self._traced_generate(state, system, user)

    def _parse_result(self, raw: str, state: DialogueState, input_tok: int, output_tok: int) -> Act:
        """Parse LLM response into Act. Override in subclasses to use a role-specific parser."""
        return self._parse_response(raw, state, input_tok, output_tok)

    def _traced_generate(self, state: DialogueState, system: str, user: str) -> Act:
        """Open a trace, call the provider, parse via _parse_result, retry once on JSON failure."""
        with ActionTrace.start(
            action="agent.generate",
            kind="model",
            actor=self.role,
            project="agora",
            correlation_id=state.run_id,
            meta={"model": self.model, "turn": state.turn},
        ) as trace:
            raw, input_tok, output_tok = self._call_provider(system, user)
            trace.model(operation="completion", target=self.model, tokens_in=input_tok, tokens_out=output_tok)
            try:
                act = self._parse_result(raw, state, input_tok, output_tok)
                trace.step(f"parse: {act.act_type}")
                trace.output({"act_type": act.act_type})
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
                return act2

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
            enable_web_search=True,
        )
