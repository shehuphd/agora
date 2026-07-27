"""Async debate runner — drives the full turn loop for one Agora session."""
import asyncio
import functools
import json as _json
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from traceact import ActionTrace
from core.config import DebateRunConfig
from core.state import DialogueState, TokenUsage, apply_act, legal_acts_for
from core.grammar import validate_act
from core.termination import check_termination
from core.checkpoint import init_db, checkpoint
from core import runs_db as _runs_db
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
# A debater turn is now retrieval (~10-20s of provider-side web search) plus
# composition plus a possible JSON-repair retry, all inside this one limit.
_AGENT_TIMEOUT = 180.0  # seconds


async def run_debate(
    run_id: str,
    config: DebateRunConfig,
    run_dir: Path,
    event_queue: asyncio.Queue,
    pause_event: asyncio.Event | None = None,
    overrides: dict | None = None,
    force_close_event: asyncio.Event | None = None,
    initial_state: "DialogueState | None" = None,
    turn_idx_start: int = 0,
    continued_from: str | None = None,
    experiment_name: str | None = None,
):
    """Entry point: initialise DB + state, build agents, run orchestrator."""
    with ActionTrace.start(
        action="debate.run",
        kind="app",
        actor="user",
        project="agora",
        correlation_id=run_id,
    ) as debate_trace:
        debate_trace.input({
            "run_id": run_id,
            "topic": config.topic,
            "proposition_model": config.proposition.model,
            "opposition_model": config.opposition.model,
            "moderator_model": config.moderator.model,
            "synthesiser_model": config.synthesiser.model,
            "max_turns": config.protocol.max_turns,
            "token_budget": config.protocol.token_budget,
        })
        await _run_debate_inner(
            run_id=run_id, config=config, run_dir=run_dir,
            event_queue=event_queue, pause_event=pause_event,
            overrides=overrides, force_close_event=force_close_event,
            initial_state=initial_state, turn_idx_start=turn_idx_start,
            continued_from=continued_from, experiment_name=experiment_name,
            debate_trace=debate_trace,
        )


async def _run_debate_inner(
    run_id, config, run_dir, event_queue, pause_event, overrides,
    force_close_event, initial_state, turn_idx_start, continued_from,
    experiment_name, debate_trace,
):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(config.to_json())

    # Bind the shared evidence pool to this run so it persists to sources.json.
    from core.sources import get_pool
    pool = get_pool(run_id, run_dir)
    if continued_from:
        # The inherited transcript cites the original run's sources; seed them
        # so re-citing one is not stripped as fabricated.
        try:
            idx = _runs_db.connect()
            row = idx.execute(
                "SELECT run_dir FROM runs WHERE run_id=?", (continued_from,)
            ).fetchone()
            idx.close()
            if row and row["run_dir"]:
                seeded = pool.load_from(run_dir.parent / row["run_dir"] / "sources.json")
                if seeded:
                    print(f"[sources] seeded {seeded} source(s) from {continued_from}", flush=True)
        except Exception as exc:
            print(f"[sources] continuation seed failed: {exc}", flush=True)
    db_path = run_dir / "debate.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)

    now = datetime.utcnow().isoformat()

    # Write to the runs index — do this early so the run appears in history immediately.
    try:
        idx = _runs_db.connect()
        _runs_db.init(idx)
        _runs_db.insert_run(
            idx,
            run_id=run_id,
            run_dir=run_dir.name,
            created_at=now,
            debate_title=config.debate_title or "",
            topic=config.topic,
            steelman_mode=config.steelman_mode,
            proposition_nickname=config.proposition.nickname,
            opposition_nickname=config.opposition.nickname,
            continued_from=continued_from,
            config_json=config.to_json(),
        )
        idx.close()
    except Exception as exc:
        print(f"[runs_db] insert_run failed: {exc}", flush=True)

    if experiment_name:
        try:
            import uuid as _uuid
            eidx = _runs_db.connect()
            _runs_db.init(eidx)
            exp = _runs_db.find_experiment_by_name(eidx, experiment_name)
            if exp is None:
                eid = str(_uuid.uuid4())
                _runs_db.create_experiment(eidx, experiment_id=eid, name=experiment_name, description=None, created_at=now)
            else:
                eid = exp["experiment_id"]
            _runs_db.assign_run(eidx, run_id, eid)
            eidx.close()
        except Exception as exc:
            print(f"[runs_db] experiment assign failed: {exc}", flush=True)

    if initial_state is not None:
        # Continuation: reuse historical state with a fresh run identity.
        state = initial_state
        state.run_id      = run_id
        state.created_at  = now       # reset clock so max_time_minutes starts fresh
        state.closure_reason = None
        state.closed_at      = None
        # Keep historical token_usage — termination check fires at the original
        # budget ceiling, and the UI can show "X / original" by reading the offset.
        # Rebind claim run_ids so checkpoint() writes them under the new run.
        for claim in state.claims.values():
            claim.run_id = run_id
        conn.execute(
            "INSERT INTO runs "
            "(run_id, created_at, status, debate_title, topic, closure_reason, config, continued_from, steelman_mode) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, now, "running", state.debate_title, state.topic,
             None, config.to_json(), continued_from, int(state.steelman_mode)),
        )
        # Persist historical token totals so the frontend can display "spent / original".
        import json as _json_rt
        _offset = {
            "total":       sum(u.input_tokens + u.output_tokens for u in state.token_usage.values()),
            "proposition": state.token_usage["proposition"].input_tokens + state.token_usage["proposition"].output_tokens,
            "opposition":  state.token_usage["opposition"].input_tokens  + state.token_usage["opposition"].output_tokens,
            "moderator":   state.token_usage["moderator"].input_tokens   + state.token_usage["moderator"].output_tokens,
        }
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     ("token_offset", _json_rt.dumps(_offset)))
    else:
        state = DialogueState(
            run_id=run_id,
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
            "INSERT INTO runs "
            "(run_id, created_at, status, debate_title, topic, closure_reason, config, steelman_mode) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, now, "running", config.debate_title, config.topic,
             None, config.to_json(), int(config.steelman_mode)),
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
    synthesiser = SynthesiserAgent(
        model=config.synthesiser.model,
        temperature=config.synthesiser.temperature,
        config={},
    )

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
        turn_idx_start=turn_idx_start,
        continued_from=continued_from,
    )
    await orchestrator.run()
    debate_trace.output({
        "closure_reason": state.closure_reason,
        "turns": state.turn,
        "total_tokens": sum(u.input_tokens + u.output_tokens for u in state.token_usage.values()),
    })


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
        turn_idx_start: int = 0,
        continued_from: str | None = None,
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
        self._turn_idx_start = turn_idx_start
        self._continued_from = continued_from

    def _effective_token_budget(self) -> int:
        return self._overrides.get("token_budget", self.config.protocol.token_budget)

    async def _maybe_summarise_chapter(self) -> None:
        """Every K debater turns (agent_settings.chapter_every, 0 = off), have
        the synthesiser write a chapter summary and store it on state. Amortises
        the synthesiser's close-time work across the run; failures cost detail,
        never the debate."""
        try:
            from api.routers.settings import _load_config
            k = int((_load_config().get("agent_settings") or {}).get("chapter_every", 0) or 0)
        except Exception:
            k = 0
        if k <= 0 or self.state.turn == 0 or self.state.turn % k != 0:
            return
        chapters = getattr(self.state, "chapters", None)
        if chapters is None:
            self.state.chapters = chapters = []
        start = self.state.turn - k + 1
        if any(f"[Turns {start}-" in c for c in chapters):
            return  # already summarised (e.g. after a pause/resume on the same turn)
        summary = await self._loop.run_in_executor(
            None, self.synthesiser.summarise_chapter, self.state, start, self.state.turn,
        )
        if summary:
            chapters.append(summary)

    async def _wait_if_paused(self) -> None:
        if not self._pause_event.is_set():
            await self.event_queue.put({"type": "paused"})
            await self._pause_event.wait()
            await self.event_queue.put({"type": "resumed"})

    async def run(self) -> None:
        max_turns = self.config.protocol.max_turns
        turn_idx = self._turn_idx_start

        await asyncio.sleep(0.3)
        intro: dict = {
            "type": "intro",
            "topic": self.state.topic,
            "proposition_nickname": self.agents[0].nickname,
            "opposition_nickname":  self.agents[1].nickname,
            "moderator_nickname":   self.moderator.nickname,
            "steelman_mode":        self.state.steelman_mode,
        }
        if self._continued_from:
            intro["is_continuation"] = True
            intro["continued_from"]  = self._continued_from
            intro["turn_start"]      = self.state.turn
        await self.event_queue.put(intro)

        try:
            while self.state.turn < max_turns * 2 + 4:  # safety ceiling
                await self._wait_if_paused()

                agent = self.agents[turn_idx % 2]
                self.state.next_agent = agent.role

                with ActionTrace.start(
                    action="debate.turn",
                    kind="app",
                    actor="orchestrator",
                    project="agora",
                    correlation_id=self.state.run_id,
                    meta={"turn": self.state.turn, "agent_role": agent.role},
                ) as turn_trace:
                    turn_trace.input({
                        "turn": self.state.turn,
                        "agent": agent.nickname,
                        "agent_role": agent.role,
                    })

                    await self.event_queue.put({"type": "thinking", "agent": agent.nickname, "role": agent.role})
                    act = None
                    try:
                        act = await self._call(agent.generate, self.state)
                        validate_act(self.state, act.act_type)
                    except asyncio.TimeoutError:
                        turn_trace.step(f"timeout: {agent.role} after 90s")
                        turn_trace.output({"error": "timeout", "agent_role": agent.role})
                        await self.event_queue.put({
                            "type": "error",
                            "message": "Agent timed out (90 s) — the AI provider may be overloaded. Try again.",
                        })
                        break
                    except QuotaExhaustedError as e:
                        turn_trace.step(f"quota_exhausted: {e.provider}")
                        turn_trace.output({"error": "quota_exhausted", "provider": e.provider})
                        _write_quota_warning(e.provider)
                        self.state.closure_reason = f"quota_exhausted_{e.provider}"
                        await self.event_queue.put({"type": "error", "message": _friendly_quota_error(e.provider)})
                        break
                    except Exception as e:
                        turn_trace.step(f"agent_error: {type(e).__name__}")
                        turn_trace.output({"error": str(e), "agent_role": agent.role})
                        print(f"[runner] {agent.role.upper()} ERROR ({agent.model}): {e}\n{traceback.format_exc()}", flush=True)
                        label = f"{agent.role} ({agent.model}): "
                        await self.event_queue.put({"type": "error", "message": label + _friendly_error(e)})
                        break

                    turn_trace.step(f"agent: {act.act_type}")
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

                    if should_close:
                        turn_trace.step(f"termination: {closure_reason}")

                    # Sync moderator's displayed budget to the current effective value.
                    # The attribute is _token_budget (private) — must use the correct name.
                    self.moderator._token_budget = self._effective_token_budget()

                    await self.event_queue.put({"type": "thinking", "agent": "Moderator", "role": "moderator"})
                    mod_act = None
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
                        turn_trace.step(f"moderator: {mod_act.act_type}")
                    except QuotaExhaustedError as e:
                        turn_trace.step(f"moderator_quota_exhausted: {e.provider}")
                        _write_quota_warning(e.provider)
                        print(f"[runner] MODERATOR QUOTA ERROR: {e}", flush=True)
                        await self.event_queue.put({
                            "type": "error",
                            "message": f"Moderator: {_friendly_quota_error(e.provider)}",
                        })
                        should_close = True
                        if not self.state.closure_reason:
                            self.state.closure_reason = f"quota_exhausted_{e.provider}"
                    except Exception as e:
                        turn_trace.step(f"moderator_error: {type(e).__name__}")
                        print(f"[runner] MODERATOR ERROR: {e}\n{traceback.format_exc()}", flush=True)
                        await self.event_queue.put({
                            "type": "error",
                            "message": f"moderator ({self.moderator.model}): {_friendly_error(e)}",
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
                            turn_trace.step(f"synthesiser: {synth_act.act_type}")
                        except QuotaExhaustedError as e:
                            turn_trace.step(f"synthesiser_quota_exhausted: {e.provider}")
                            _write_quota_warning(e.provider)
                            await self.event_queue.put({
                                "type": "error",
                                "message": f"Synthesiser: {_friendly_quota_error(e.provider)}",
                            })
                        except Exception as e:
                            turn_trace.step(f"synthesiser_error: {type(e).__name__}")
                            await self.event_queue.put({
                                "type": "error",
                                "message": f"synthesiser ({self.synthesiser.model}): {_friendly_error(e)}",
                            })
                        turn_trace.output({
                            "agent_act": act.act_type if act else None,
                            "mod_act": mod_act.act_type if mod_act else None,
                            "closed": True,
                            "closure_reason": closure_reason or self.state.closure_reason,
                        })
                        break

                    turn_trace.output({
                        "agent_act": act.act_type,
                        "mod_act": mod_act.act_type if mod_act else None,
                        "closed": False,
                    })

                await self._maybe_summarise_chapter()
                turn_idx += 1

        finally:
            status = "closed" if self.state.closure_reason else "error"
            try:
                self.conn.execute(
                    "UPDATE runs SET status=?, closure_reason=? WHERE run_id=?",
                    (status, self.state.closure_reason, self.state.run_id),
                )
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass
            # Update the runs index with final status + counts.
            try:
                final_tokens = sum(
                    u.input_tokens + u.output_tokens
                    for u in self.state.token_usage.values()
                )
                idx = _runs_db.connect()
                _runs_db.update_on_close(
                    idx,
                    run_id=self.state.run_id,
                    status=status,
                    closure_reason=self.state.closure_reason,
                    debate_title=self.state.debate_title or "",
                    turn=self.state.turn,
                    total_tokens=final_tokens,
                )
                idx.close()
            except Exception as exc:
                print(f"[runs_db] update_on_close failed: {exc}", flush=True)
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
        "run_id":       act.run_id,
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
    if "insufficient_permission" in low or "insufficient permissions" in low:
        return "Insufficient permissions for this model — your API key or plan may not have inference access to it, even if it appears in the model list. Try a different model or check your OpenAI project permissions."
    if "model_not_found" in low:
        return "Model not found — the selected model doesn't exist. Pick a different model in Settings."
    if "invalid_api_key" in low or "incorrect api key" in low or "api key" in low:
        return "Invalid API key — check your key in Settings."
    if "auth" in low or "401" in msg:
        return "Authentication failed — check your API key in Settings."
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
