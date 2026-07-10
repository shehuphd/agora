"""Async debate runner — drives the full turn loop for one Agora session."""
import asyncio
import functools
import json as _json
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import DebateRunConfig
from core.state import DialogueState, TokenUsage, apply_act, legal_acts_for
from core.grammar import validate_act
from core.termination import check_termination
from core.checkpoint import init_db, checkpoint
from agents.proposition import PropositionAgent
from agents.opposition import OppositionAgent
from agents.moderator import ModeratorAgent
from agents.synthesiser import SynthesiserAgent
from agents.base import QuotaExhaustedError

_WARNINGS_PATH = Path(__file__).parent.parent / "config" / "key_warnings.json"


def _write_quota_warning(provider: str) -> None:
    """Persist a quota-exhaustion timestamp for the given provider."""
    try:
        warnings: dict = {}
        if _WARNINGS_PATH.exists():
            with open(_WARNINGS_PATH) as f:
                warnings = _json.load(f)
        warnings[provider] = datetime.utcnow().isoformat()
        with open(_WARNINGS_PATH, "w") as f:
            _json.dump(warnings, f)
    except Exception:
        pass

# Hard ceiling on a single LLM call. Prevents a hung provider from stalling the
# SSE stream indefinitely. The user sees a timeout error; they can retry.
_AGENT_TIMEOUT = 90.0  # seconds


async def run_debate(
    session_id: str,
    config: DebateRunConfig,
    run_dir: Path,
    event_queue: asyncio.Queue,
    pause_event: asyncio.Event | None = None,
    overrides: dict | None = None,
    force_close_event: asyncio.Event | None = None,
):
    """Entry point: initialise DB + state, build agents, run orchestrator."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(config.to_json())
    db_path = run_dir / "debate.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)

    now = datetime.utcnow().isoformat()
    state = DialogueState(
        session_id=session_id,
        turn=0,
        phase="init",
        claims={},
        acts=[],
        outstanding_challenges=[],
        next_agent="proposition",
        legal_acts=["ASSERT"],
        token_usage={
            "proposition": TokenUsage(),
            "opposition":  TokenUsage(),
            "moderator":   TokenUsage(),
            "synthesiser": TokenUsage(),
        },
        debate_title=config.debate_title,
        topic=config.topic,
        config={},
        created_at=now,
        closed_at=None,
        closure_reason=None,
        steelman_mode=config.steelman_mode,
    )

    conn.execute(
        "INSERT INTO sessions (session_id, created_at, status, debate_title, topic, closure_reason, config) "
        "VALUES (?,?,?,?,?,?,?)",
        (session_id, now, "running", config.debate_title, config.topic, None, config.to_json()),
    )
    conn.commit()

    proposition = PropositionAgent(
        nickname=config.proposition.nickname,
        model=config.proposition.model,
        temperature=config.proposition.temperature,
        config={},
    )
    opposition = OppositionAgent(
        nickname=config.opposition.nickname,
        model=config.opposition.model,
        temperature=config.opposition.temperature,
        aggression=config.opposition.aggression,
        min_challenges=config.protocol.min_challenges,
        min_concessions=config.protocol.min_concessions,
        config={},
    )
    moderator = ModeratorAgent(
        nickname="Moderator",
        model=config.moderator.model,
        temperature=config.moderator.temperature,
        max_turns=config.protocol.max_turns,
        token_budget=config.protocol.token_budget,
        config={},
    )
    synthesiser = SynthesiserAgent(config={})

    # Default no-op pause_event (always "running") when not supplied
    if pause_event is None:
        pause_event = asyncio.Event()
        pause_event.set()

    orchestrator = TurnOrchestrator(
        state=state,
        agents=[proposition, opposition],
        moderator=moderator,
        synthesiser=synthesiser,
        conn=conn,
        event_queue=event_queue,
        run_dir=run_dir,
        config=config,
        pause_event=pause_event,
        overrides=overrides or {},
        force_close_event=force_close_event,
    )
    await orchestrator.run()


class TurnOrchestrator:
    """Drives the turn-by-turn debate loop.

    Separated from SSE machinery so it can be tested without an HTTP server.
    Each LLM call runs in a thread-pool executor with a hard timeout; on
    timeout or error the queue receives a user-friendly error event and the
    session is marked closed in the finally block.
    """

    def __init__(
        self,
        state: DialogueState,
        agents: list,
        moderator: ModeratorAgent,
        synthesiser: SynthesiserAgent,
        conn: sqlite3.Connection,
        event_queue: asyncio.Queue,
        run_dir: Path,
        config: DebateRunConfig,
        pause_event: asyncio.Event | None = None,
        overrides: dict | None = None,
        force_close_event: asyncio.Event | None = None,
    ):
        self.state = state
        self.agents = agents
        self.moderator = moderator
        self.synthesiser = synthesiser
        self.conn = conn
        self.event_queue = event_queue
        self.run_dir = run_dir
        self.config = config
        self._pause_event = pause_event or asyncio.Event()
        if not self._pause_event.is_set():
            self._pause_event.set()
        self._overrides = overrides if overrides is not None else {}
        self._force_close_event = force_close_event or asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def _effective_token_budget(self) -> int:
        return self._overrides.get("token_budget", self.config.protocol.token_budget)

    async def _wait_if_paused(self) -> None:
        if not self._pause_event.is_set():
            await self.event_queue.put({"type": "paused"})
            await self._pause_event.wait()
            await self.event_queue.put({"type": "resumed"})

    async def run(self) -> None:
        max_turns = self.config.protocol.max_turns
        turn_idx = 0

        # Short pause so the SSE connection is established before the first event.
        await asyncio.sleep(0.3)
        await self.event_queue.put({
            "type": "intro",
            "topic": self.state.topic,
            "proposition_nickname": self.agents[0].nickname,
            "opposition_nickname":  self.agents[1].nickname,
            "moderator_nickname":   self.moderator.nickname,
            "steelman_mode":        self.state.steelman_mode,
        })

        try:
            while self.state.turn < max_turns * 2 + 4:  # safety ceiling
                await self._wait_if_paused()

                agent = self.agents[turn_idx % 2]
                self.state.next_agent = agent.role

                await self.event_queue.put({"type": "thinking", "agent": agent.nickname, "role": agent.role})
                try:
                    act = await self._call(agent.generate, self.state)
                    validate_act(self.state, act.act_type)
                except asyncio.TimeoutError:
                    await self.event_queue.put({
                        "type": "error",
                        "message": "Agent timed out (90 s) — the AI provider may be overloaded. Try again.",
                    })
                    break
                except QuotaExhaustedError as e:
                    _write_quota_warning(e.provider)
                    await self.event_queue.put({"type": "error", "message": _friendly_quota_error(e.provider)})
                    break
                except Exception as e:
                    await self.event_queue.put({"type": "error", "message": _friendly_error(e)})
                    break

                apply_act(self.state, act)
                checkpoint(self.conn, self.state, act, self.run_dir)
                await self.event_queue.put(_act_to_dict(act))
                await asyncio.sleep(0.7)

                # Build effective termination config (may differ from original if budget overridden)
                term_cfg = self.config.to_termination_dict()
                term_cfg["protocol"]["token_budget"] = self._effective_token_budget()
                should_close, closure_reason = check_termination(self.state, term_cfg)

                # User clicked "end debate" — override termination regardless of turn count.
                if self._force_close_event.is_set():
                    should_close   = True
                    closure_reason = "user_requested_end"

                # Sync moderator's displayed budget to the current effective value.
                # The attribute is _token_budget (private) — must use the correct name.
                self.moderator._token_budget = self._effective_token_budget()

                await self.event_queue.put({"type": "thinking", "agent": "Moderator", "role": "moderator"})
                try:
                    mod_fn = functools.partial(
                        self.moderator.generate, self.state,
                        should_close=should_close, closure_reason=closure_reason,
                    )
                    mod_act = await self._call(mod_fn)
                    apply_act(self.state, mod_act)
                    checkpoint(self.conn, self.state, mod_act, self.run_dir)
                    await self.event_queue.put(_act_to_dict(mod_act))
                    await asyncio.sleep(0.7)
                    # Honour a CLOSE the moderator generated on its own judgement,
                    # even if check_termination didn't instruct it to close.
                    if mod_act.act_type == "CLOSE":
                        should_close = True
                except QuotaExhaustedError as e:
                    _write_quota_warning(e.provider)
                    print(f"[runner] MODERATOR QUOTA ERROR: {e}", flush=True)
                    await self.event_queue.put({
                        "type": "error",
                        "message": f"Moderator: {_friendly_quota_error(e.provider)}",
                    })
                    should_close = True
                    if not self.state.closure_reason:
                        self.state.closure_reason = "moderator_error"
                except Exception as e:
                    print(f"[runner] MODERATOR ERROR: {e}\n{traceback.format_exc()}", flush=True)
                    await self.event_queue.put({
                        "type": "error",
                        "message": f"Moderator: {_friendly_error(e)}",
                    })
                    should_close = True
                    if not self.state.closure_reason:
                        self.state.closure_reason = "moderator_error"

                if should_close:
                    await self.event_queue.put({"type": "thinking", "agent": "Synthesis", "role": "synthesiser"})
                    try:
                        synth_act = await self._call(self.synthesiser.generate, self.state)
                        apply_act(self.state, synth_act)
                        checkpoint(self.conn, self.state, synth_act, self.run_dir)
                        await self.event_queue.put(_act_to_dict(synth_act))
                    except QuotaExhaustedError as e:
                        _write_quota_warning(e.provider)
                        await self.event_queue.put({
                            "type": "error",
                            "message": f"Synthesiser: {_friendly_quota_error(e.provider)}",
                        })
                    except Exception as e:
                        await self.event_queue.put({
                            "type": "error",
                            "message": f"Synthesiser: {_friendly_error(e)}",
                        })
                    break

                turn_idx += 1

        finally:
            status = "closed" if self.state.closure_reason else "error"
            try:
                self.conn.execute(
                    "UPDATE sessions SET status=?, closure_reason=? WHERE session_id=?",
                    (status, self.state.closure_reason, self.state.session_id),
                )
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass
            await self.event_queue.put(None)

    async def _call(self, fn: Any, *args: Any) -> Any:
        """Run a blocking LLM call in the thread pool with a hard timeout."""
        callable_ = functools.partial(fn, *args) if args else fn
        return await asyncio.wait_for(
            self._loop.run_in_executor(None, callable_),
            timeout=_AGENT_TIMEOUT,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _act_to_dict(act) -> dict:
    return {
        "act_id":       act.act_id,
        "session_id":   act.session_id,
        "turn":         act.turn,
        "agent":        act.agent,
        "agent_role":   act.agent_role,
        "act_type":     act.act_type,
        "claim_id":     act.claim_id,
        "target_act_id": act.target_act_id,
        "content":      act.content,
        "reason":       act.reason,
        "input_tokens": act.input_tokens,
        "output_tokens": act.output_tokens,
        "model_used":   act.model_used,
        "timestamp":    act.timestamp,
    }


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "rate_limit" in low or "rate limit" in low or "429" in msg:
        return "Rate limit reached — wait a moment and try again, or check your API subscription."
    if "auth" in low or "401" in low or "invalid_api_key" in low or "incorrect api key" in low:
        return "Invalid API key — check your key in Settings."
    if "context_length" in low or "context length" in low or "too many tokens" in low:
        return "Context length exceeded — reduce the token budget or turn count and try again."
    if "overloaded" in low or "529" in msg:
        return "The AI provider is currently overloaded — try again in a few minutes."
    return msg


def _friendly_quota_error(provider: str) -> str:
    names = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google"}
    name = names.get(provider, provider)
    return (
        f"Your {name} account has run out of credits. "
        f"Check your billing at {name}'s dashboard, or update the key in Agora Settings. "
        f"A warning indicator has been added next to the key."
    )
