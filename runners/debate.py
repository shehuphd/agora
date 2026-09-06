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
from core.state import DialogueState, TokenUsage, apply_act, lapse_stale_challenges, legal_acts_for
from core.grammar import validate_act
from core.termination import check_termination
from core.checkpoint import init_db, checkpoint
from core import runs_db as _runs_db
from agents.proposition import PropositionAgent
from agents.opposition import OppositionAgent
from agents.moderator import ModeratorAgent
from agents.synthesiser import SynthesiserAgent
from agents.base import KeyCallError, AgentResponseError

_WARNINGS_PATH = Path(__file__).parent.parent / "config" / "key_warnings.json"


def _retire_unknown_model(agent, exc: Exception) -> None:
    """Retire a model the provider says does not exist, so it stops being offered.

    Only fires on the provider's own "unknown model" verdict — never on rate
    limits, auth, or transient failures, which say nothing about whether the
    model exists. KeyCallError carries this as a typed code
    (MODEL_NOT_AVAILABLE); any other exception falls back to the old
    string check, kept for whatever isn't routed through keycall yet.
    """
    if isinstance(exc, KeyCallError):
        if exc.code.name != "MODEL_NOT_AVAILABLE":
            return
    else:
        low = str(exc).lower()
        if not ("model_not_found" in low or "invalid model" in low
                or "does not exist" in low or "invalid_model" in low):
            return
    try:
        idx = _runs_db.connect()
        _runs_db.mark_model_unservable(idx, agent._provider, agent.model)
        idx.close()
        print(f"[models] retired {agent._provider}/{agent.model} — provider "
              f"reports it does not exist", flush=True)
    except Exception as e:
        print(f"[models] could not retire {agent.model}: {e}", flush=True)


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
# Sized above the transport read timeout (providers/keycall_backend.py:
# _READ_TIMEOUT, 240s) so the transport's typed, retryable error fires first;
# kimi-k3 measurably spends over 150s of reasoning on a mid-debate prompt, so
# both limits sit well above that.
_AGENT_TIMEOUT = 300.0  # seconds


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
    condition: str | None = None,
    unattended: bool = False,
):
    """Entry point: initialise DB + state, build agents, run orchestrator.

    unattended=True means nobody is watching to click Resume (batch rows),
    so failures that would pause an interactive debate fail the run instead
    — a paused batch row would hold its concurrency slot forever.
    """
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
            condition=condition, unattended=unattended, debate_trace=debate_trace,
        )


async def _run_debate_inner(
    run_id, config, run_dir, event_queue, pause_event, overrides,
    force_close_event, initial_state, turn_idx_start, continued_from,
    experiment_name, condition, unattended, debate_trace,
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
            condition=condition,
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
        provider=config.proposition.provider,
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
        provider=config.opposition.provider,
        config={},
    )
    moderator = ModeratorAgent(
        nickname="Moderator",
        model=config.moderator.model,
        temperature=config.moderator.temperature,
        max_turns=config.protocol.max_turns,
        token_budget=config.protocol.token_budget,
        provider=config.moderator.provider,
        config={},
    )
    synthesiser = SynthesiserAgent(
        model=config.synthesiser.model,
        provider=config.synthesiser.provider,
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
        unattended=unattended,
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
        unattended: bool = False,
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
        # Nobody watches an unattended run (a batch row), so failures that
        # would pause an interactive debate fail the run instead — a paused
        # batch row would hold its concurrency slot forever.
        self._unattended = unattended
        # Set when a run stops on a failure rather than a protocol
        # termination, so the final status reflects that.
        self._failed = False
        # Whole-turn token totals for the most recent turns, newest last.
        # Feeds the projected-overrun check: a turn is not launched when the
        # budget can't plausibly cover it.
        self._turn_token_history: list[int] = []

    def _effective_token_budget(self) -> int:
        return self._overrides.get("token_budget", self.config.protocol.token_budget)

    def _spent_tokens(self) -> int:
        return sum(u.input_tokens + u.output_tokens for u in self.state.token_usage.values())

    def _projected_over_budget(self) -> bool:
        """Whether launching another turn would plausibly overrun the budget.

        check_termination stops a debate only once spend has already crossed
        the budget, which lets one heavy final turn overshoot it (a 50k run
        finished at 62k this way, 2026-09-05). Project the next turn as the
        larger of the last two whole-turn totals — turns alternate seats, so
        one turn back is the other seat — and close ahead of it instead.
        Never fires before any full turn has been measured.

        The projection carries a retry margin of one retry on that heaviest
        turn (i.e. it is doubled): a JSON-repair retry re-bills a full call
        after the launch decision, so a plain last-two projection undercounts
        any turn that needs one. Measured 2026-09-06: an 80k run closed at
        112% when a projected-11k turn took two retries and cost 31k."""
        if not self._turn_token_history:
            return False
        projected = 2 * max(self._turn_token_history[-2:])
        return self._spent_tokens() + projected > self._effective_token_budget()

    def _final_cost(self) -> tuple[float | None, bool]:
        """Dollar cost of the whole run, summed from each act's own cost_usd
        — priced once, per act, at generation time (agents/base.py) against
        the exact tokens and (provider, model) that produced it. Not
        re-derived here, so this can never disagree with the per-act costs
        the run pack shows.

        Returns (total_usd, partial). total_usd is None only when nothing in
        the run could be priced. partial is True when at least one act
        couldn't be priced — the sum then understates the true cost.
        """
        total = 0.0
        priced_any = False
        partial = False
        for act in self.state.acts:
            if act.cost_usd is None:
                partial = True
            else:
                total += act.cost_usd
                priced_any = True
        # Auxiliary calls (chapter and epoch summaries) produce no act but
        # are priced at call time onto state; the run total includes them.
        aux = getattr(self.state, "aux_cost_usd", 0.0)
        if aux:
            total += aux
            priced_any = True
        return (total if priced_any else None), partial

    # Chapter-list cap: past this, the oldest half collapses into one epoch
    # summary (see _maybe_summarise_chapter), keeping the summaries themselves
    # from growing without bound on very long runs.
    _MAX_CHAPTERS = 10

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
        # The same hard timeout every agent call gets. Without it, one hung
        # provider connection inside a chapter call freezes the whole run
        # (observed live 2026-09-05: a run wedged for two hours at turn 50
        # inside this call). On timeout the chapter is skipped; the debate
        # loses summary detail, never the run.
        try:
            summary = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, self.synthesiser.summarise_chapter,
                    self.state, start, self.state.turn,
                ),
                timeout=_AGENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[runner] chapter summary timed out after {_AGENT_TIMEOUT:.0f}s "
                  f"(turns {start}-{self.state.turn}) — skipped", flush=True)
            return
        if summary:
            chapters.append(summary)
        # Chapters are themselves bounded: past the cap, the oldest half
        # collapses into one epoch summary, so debater context stays
        # fixed-size no matter how long the run gets. A failed epoch call
        # leaves the list as-is; the collapse retries at the next chapter.
        if len(chapters) > self._MAX_CHAPTERS:
            half = len(chapters) // 2
            try:
                epoch = await asyncio.wait_for(
                    self._loop.run_in_executor(
                        None, self.synthesiser.summarise_epoch,
                        self.state, chapters[:half],
                    ),
                    timeout=_AGENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                print(f"[runner] epoch summary timed out after {_AGENT_TIMEOUT:.0f}s "
                      f"— collapse retries at the next chapter", flush=True)
                return
            if epoch:
                self.state.chapters = [epoch] + chapters[half:]

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

                    spent_at_turn_start = self._spent_tokens()

                    # Failure paths below no longer break out of the loop:
                    # they record the reason and fall through, so the run
                    # still gets a moderator close where possible and an
                    # argument map either way — a debate that died at turn 8
                    # still has 8 turns of record to map. skip_debater
                    # covers the projected-overrun close, where the turn is
                    # never launched at all.
                    skip_debater = self._projected_over_budget()
                    if skip_debater:
                        turn_trace.step("termination: token_budget (projected)")

                    act = None
                    if not skip_debater:
                        await self.event_queue.put({"type": "thinking", "agent": agent.nickname, "role": agent.role})
                        try:
                            act = await self._call_with_pause_on_failure(agent.generate, self.state)
                            validate_act(self.state, act.act_type)
                        except asyncio.TimeoutError:
                            act = None
                            turn_trace.step(f"timeout: {agent.role} after {_AGENT_TIMEOUT:.0f}s")
                            turn_trace.output({"error": "timeout", "agent_role": agent.role})
                            self._failed = True
                            self.state.closure_reason = f"timeout_{agent.role}"
                            await self.event_queue.put({
                                "type": "error",
                                "message": f"{agent.role} ({agent.model}) timed out after "
                                           f"{_AGENT_TIMEOUT:.0f}s — the provider may be "
                                           f"overloaded. The debate is closing with what it has.",
                            })
                        except KeyCallError as e:
                            # A generate that returned but failed validation
                            # leaves act assigned; it must not be applied.
                            act = None
                            if e.code.name == "PERMISSION_DENIED":
                                turn_trace.step(f"quota_exhausted: {agent._provider}")
                                turn_trace.output({"error": "quota_exhausted", "provider": agent._provider})
                                _write_quota_warning(agent._provider)
                                self._failed = True
                                self.state.closure_reason = f"quota_exhausted_{agent._provider}"
                                await self.event_queue.put({"type": "error", "message": _friendly_quota_error(agent._provider)})
                            else:
                                turn_trace.step(f"agent_error: {e.code.name}")
                                turn_trace.output({"error": e.message, "agent_role": agent.role})
                                print(f"[runner] {agent.role.upper()} ERROR ({agent.model}): {e.code.name} — {e.message}", flush=True)
                                _retire_unknown_model(agent, e)
                                self._failed = True
                                self.state.closure_reason = f"{agent.role}_error"
                                await self.event_queue.put({"type": "error", "message": f"{agent.role} ({agent.model}): {e.message}"})
                        except Exception as e:
                            act = None
                            turn_trace.step(f"agent_error: {type(e).__name__}")
                            turn_trace.output({"error": str(e), "agent_role": agent.role})
                            print(f"[runner] {agent.role.upper()} ERROR ({agent.model}): {e}\n{traceback.format_exc()}", flush=True)
                            _retire_unknown_model(agent, e)
                            # Recorded so history and the run pack can say why the
                            # run stopped, rather than only that it did.
                            self._failed = True
                            self.state.closure_reason = f"{agent.role}_error"
                            label = f"{agent.role} ({agent.model}): "
                            await self.event_queue.put({"type": "error", "message": label + _friendly_error(e)})

                    if act is not None:
                        turn_trace.step(f"agent: {act.act_type}")
                        apply_act(self.state, act)
                        # Retire challenges the debate has moved past
                        # (defended, then no opposition follow-up for N
                        # turns) so prompts stop growing with them.
                        lapsed = lapse_stale_challenges(self.state)
                        if lapsed:
                            turn_trace.step(f"challenges.lapsed: {len(lapsed)}")
                        checkpoint(self.conn, self.state, act, self.run_dir)
                        await self.event_queue.put(_act_to_dict(act))
                        await asyncio.sleep(0.7)

                    # Build effective termination config (may differ from original if budget overridden)
                    term_cfg = self.config.to_termination_dict()
                    term_cfg["protocol"]["token_budget"] = self._effective_token_budget()
                    should_close, closure_reason = check_termination(self.state, term_cfg)
                    if skip_debater:
                        should_close, closure_reason = True, "token_budget"
                    elif act is None:
                        # The debater failed: close with the recorded reason.
                        should_close = True
                        closure_reason = self.state.closure_reason

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
                        mod_act = await self._call_with_pause_on_failure(mod_fn)
                        apply_act(self.state, mod_act)
                        checkpoint(self.conn, self.state, mod_act, self.run_dir)
                        await self.event_queue.put(_act_to_dict(mod_act))
                        await asyncio.sleep(0.7)
                        # Honour a CLOSE the moderator generated on its own judgement,
                        # even if check_termination didn't instruct it to close.
                        if mod_act.act_type == "CLOSE":
                            should_close = True
                        turn_trace.step(f"moderator: {mod_act.act_type}")
                    except KeyCallError as e:
                        if e.code.name == "PERMISSION_DENIED":
                            turn_trace.step(f"moderator_quota_exhausted: {self.moderator._provider}")
                            _write_quota_warning(self.moderator._provider)
                            print(f"[runner] MODERATOR QUOTA ERROR: {e.message}", flush=True)
                            await self.event_queue.put({
                                "type": "error",
                                "message": f"Moderator: {_friendly_quota_error(self.moderator._provider)}",
                            })
                            should_close = True
                            self._failed = True
                            if not self.state.closure_reason:
                                self.state.closure_reason = f"quota_exhausted_{self.moderator._provider}"
                        else:
                            turn_trace.step(f"moderator_error: {e.code.name}")
                            print(f"[runner] MODERATOR ERROR: {e.code.name} — {e.message}", flush=True)
                            _retire_unknown_model(self.moderator, e)
                            await self.event_queue.put({
                                "type": "error",
                                "message": f"moderator ({self.moderator.model}): {e.message}",
                            })
                            should_close = True
                            self._failed = True
                            if not self.state.closure_reason:
                                self.state.closure_reason = "moderator_error"
                    except Exception as e:
                        turn_trace.step(f"moderator_error: {type(e).__name__}")
                        print(f"[runner] MODERATOR ERROR: {e}\n{traceback.format_exc()}", flush=True)
                        _retire_unknown_model(self.moderator, e)
                        await self.event_queue.put({
                            "type": "error",
                            "message": f"moderator ({self.moderator.model}): {_friendly_error(e)}",
                        })
                        should_close = True
                        self._failed = True
                        if not self.state.closure_reason:
                            self.state.closure_reason = "moderator_error"

                    if should_close:
                        await self.event_queue.put({"type": "thinking", "agent": "Synthesis", "role": "synthesiser"})
                        try:
                            synth_act = await self._call_with_pause_on_failure(self.synthesiser.generate, self.state)
                            apply_act(self.state, synth_act)
                            checkpoint(self.conn, self.state, synth_act, self.run_dir)
                            await self.event_queue.put(_act_to_dict(synth_act))
                            turn_trace.step(f"synthesiser: {synth_act.act_type}")
                        except KeyCallError as e:
                            if e.code.name == "PERMISSION_DENIED":
                                turn_trace.step(f"synthesiser_quota_exhausted: {self.synthesiser._provider}")
                                _write_quota_warning(self.synthesiser._provider)
                                await self.event_queue.put({
                                    "type": "error",
                                    "message": f"Synthesiser: {_friendly_quota_error(self.synthesiser._provider)}",
                                })
                            else:
                                turn_trace.step(f"synthesiser_error: {e.code.name}")
                                await self.event_queue.put({
                                    "type": "error",
                                    "message": f"synthesiser ({self.synthesiser.model}): {e.message}",
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

                self._turn_token_history.append(self._spent_tokens() - spent_at_turn_start)
                del self._turn_token_history[:-3]

                await self._maybe_summarise_chapter()
                turn_idx += 1

        finally:
            # A run that stopped on a failure is not a clean close, even though
            # it records a reason. Reason and outcome are tracked separately so
            # a failure can say why without being filed as a normal ending.
            status = "error" if self._failed else (
                "closed" if self.state.closure_reason else "error"
            )
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
                total_cost, cost_partial = self._final_cost()
                idx = _runs_db.connect()
                _runs_db.update_on_close(
                    idx,
                    run_id=self.state.run_id,
                    status=status,
                    closure_reason=self.state.closure_reason,
                    debate_title=self.state.debate_title or "",
                    turn=self.state.turn,
                    total_tokens=final_tokens,
                    total_cost_usd=total_cost,
                    cost_partial=cost_partial,
                )
                idx.close()
            except Exception as exc:
                print(f"[runs_db] update_on_close failed: {exc}", flush=True)
            # Metrics are computed from the just-updated index row plus the
            # run's own files; compute_and_store never raises.
            from core import metrics as _metrics
            _metrics.compute_and_store(self.state.run_id, self.run_dir)
            await self.event_queue.put(None)

    async def _call(self, fn: Any, *args: Any) -> Any:
        """Run a blocking LLM call in the thread pool with a hard timeout."""
        callable_ = functools.partial(fn, *args) if args else fn
        return await asyncio.wait_for(
            self._loop.run_in_executor(None, callable_),
            timeout=_AGENT_TIMEOUT,
        )

    async def _call_with_pause_on_failure(self, fn: Any, *args: Any) -> Any:
        """Like _call, but recoverable failures pause the debate for a human
        instead of killing the run.

        Three failure classes pause here, all retried on resume through the
        existing pause/resume primitive (same event, same "paused"/"resumed"
        SSE events, same /pause /resume endpoints):

        - AgentResponseError: the model never produced a usable response —
          agents/base.py's own bounded correction retries are exhausted.
        - Transient provider failures: any KeyCallError keycall itself marks
          retryable (timeout, rate limit, provider unavailable, network) —
          waiting is the cure, and a human decides how long to wait.
        - Human-fixable credential failures: PERMISSION_DENIED (a spend limit
          or quota) and INVALID_API_KEY. Both are curable in Settings without
          restarting: keys are re-read from the environment on every call, so
          fixing the key or limit and clicking Resume continues the run. A
          quota pause still writes the Settings warning badge on the way.

        Everything else (model not found, unsupported operation, a hard call
        timeout in unattended mode) propagates to the caller's error
        handling. Unattended runs (batch rows) never pause at all — nobody
        is there to resume them, and a paused row would hold its batch
        concurrency slot forever; they propagate every failure and rely on
        the batch retry mechanism instead.

        If the human clicks "End debate" while paused, /end sets both
        events, and the error propagates rather than looping forever.
        """
        while True:
            try:
                return await self._call(fn, *args)
            except AgentResponseError as e:
                if self._unattended:
                    raise
                print(f"[runner] AGENT RESPONSE RETRIES EXHAUSTED: {e}", flush=True)
                await self._pause_for_failure(
                    f"{e} — debate paused for review. Resume to retry, or end the debate."
                )
                if self._force_close_event.is_set():
                    raise
            except KeyCallError as e:
                fixable = e.code.name in ("PERMISSION_DENIED", "INVALID_API_KEY")
                if self._unattended or not (e.retryable or fixable):
                    raise
                if e.code.name == "PERMISSION_DENIED" and e.provider:
                    _write_quota_warning(e.provider)
                print(f"[runner] PROVIDER FAILURE ({e.code.name}): {e.message}", flush=True)
                hint = ("fix the key or limit in Settings, then Resume to retry"
                        if fixable else "Resume to retry once the provider recovers")
                await self._pause_for_failure(
                    f"{_friendly_error(e)} — debate paused; {hint}, or end the debate."
                )
                if self._force_close_event.is_set():
                    raise

    async def _pause_for_failure(self, message: str) -> None:
        await self.event_queue.put({"type": "error", "message": message})
        self._pause_event.clear()
        await self._wait_if_paused()


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
        "cost_usd":     act.cost_usd,
        "citations":    getattr(act, "citations", None),
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
    names = {
        "anthropic": "Anthropic", "openai": "OpenAI", "google": "Google",
        "perplexity": "Perplexity", "moonshot": "Moonshot", "xai": "xAI",
    }
    name = names.get(provider, provider)
    return (
        f"Your {name} account has run out of credits. "
        f"Check your billing at {name}'s dashboard, or update the key in Agora Settings. "
        f"A warning indicator has been added next to the key."
    )
