"""Tests for TurnOrchestrator._call_with_pause_on_failure.

Before this fix, a ResponseParseError (raised by agents/base.py once its own
bounded retry is exhausted) fell into the runner's generic exception handler
and ended the debate outright — status "error", no CLOSE, no synthesis. Two
of three debates in the 2026-08-30 capability audit died this way, and two
of three in the 2026-09-05 batch died of the provider-failure equivalents (a
Moonshot read timeout, an Anthropic spend limit), which is why the helper
now also pauses on transient KeyCallErrors and human-fixable credential
failures.

The helper reuses the existing pause/resume primitive (the same one /pause
and /resume already drive) instead of a new mechanism: the debate pauses and
waits for a human, then retries the same call once resumed. If the human
instead ends the debate while paused, /end sets both the pause and
force-close events — the retry loop must notice that and propagate the error
rather than attempting one more call against a run the user chose to end.
Unattended runs (batch rows) never pause: nobody would ever resume them.
"""
import asyncio

import pytest

from keycall import ErrorCode, KeyCallError

from agents.base import ResponseParseError
from runners.debate import TurnOrchestrator


def _keycall_error(code: ErrorCode, retryable: bool = False) -> KeyCallError:
    return KeyCallError("boom", code=code, provider="moonshot", retryable=retryable)


def _orchestrator(unattended: bool = False) -> TurnOrchestrator:
    orch = object.__new__(TurnOrchestrator)  # skip __init__: needs a running loop
    orch._pause_event = asyncio.Event()
    orch._pause_event.set()  # not paused at start, same as a fresh run
    orch._force_close_event = asyncio.Event()
    orch.event_queue = asyncio.Queue()
    orch._loop = asyncio.get_event_loop()
    orch._unattended = unattended
    return orch


async def _drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class TestRecoversAfterResume:
    def test_retries_the_same_call_once_resumed(self):
        async def scenario():
            orch = _orchestrator()
            calls = {"n": 0}

            def flaky():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ResponseParseError("opposition (gemini-3.5-flash): invalid JSON 3 times in a row")
                return "recovered"

            async def resume_shortly():
                # Wait for the helper to clear the event before
                # setting it again — proves this is a working pause/resume
                # round trip, not a no-op.
                while orch._pause_event.is_set():
                    await asyncio.sleep(0)
                orch._pause_event.set()

            resumer = asyncio.create_task(resume_shortly())
            result = await orch._call_with_pause_on_failure(flaky)
            await resumer

            assert result == "recovered"
            assert calls["n"] == 2  # failed once, succeeded on the resumed retry

        asyncio.run(scenario())

    def test_emits_an_error_event_and_a_paused_event(self):
        async def scenario():
            orch = _orchestrator()
            calls = {"n": 0}

            def flaky():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ResponseParseError("moderator (gpt-4.1): invalid JSON 3 times in a row")
                return "ok"

            async def resume_shortly():
                while orch._pause_event.is_set():
                    await asyncio.sleep(0)
                orch._pause_event.set()

            resumer = asyncio.create_task(resume_shortly())
            await orch._call_with_pause_on_failure(flaky)
            await resumer

            events = await _drain(orch.event_queue)
            types = [e.get("type") for e in events]
            assert "error" in types  # fail loudly
            assert "paused" in types
            assert "resumed" in types
            error_event = next(e for e in events if e.get("type") == "error")
            assert "invalid JSON" in error_event["message"]

        asyncio.run(scenario())


class TestProviderFailuresPause:
    def _pause_and_recover(self, exc) -> tuple:
        """Drive one failure through the helper with a resume, returning
        (result, call_count, events)."""
        async def scenario():
            orch = _orchestrator()
            calls = {"n": 0}

            def flaky():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise exc
                return "recovered"

            async def resume_shortly():
                while orch._pause_event.is_set():
                    await asyncio.sleep(0)
                orch._pause_event.set()

            resumer = asyncio.create_task(resume_shortly())
            result = await orch._call_with_pause_on_failure(flaky)
            await resumer
            return result, calls["n"], await _drain(orch.event_queue)

        return asyncio.run(scenario())

    def test_transient_timeout_pauses_and_retries(self):
        result, n, events = self._pause_and_recover(
            _keycall_error(ErrorCode.TIMEOUT, retryable=True))
        assert result == "recovered"
        assert n == 2
        assert "paused" in [e.get("type") for e in events]

    def test_rate_limit_pauses_and_retries(self):
        result, n, _ = self._pause_and_recover(
            _keycall_error(ErrorCode.RATE_LIMITED, retryable=True))
        assert result == "recovered"
        assert n == 2

    def test_spend_limit_pauses_with_a_fix_hint(self):
        """The 2026-09-05 case: an Anthropic spend limit killed a run at 94k
        tokens; removing the limit and resuming should have continued it."""
        result, n, events = self._pause_and_recover(
            _keycall_error(ErrorCode.PERMISSION_DENIED))
        assert result == "recovered"
        assert n == 2
        err = next(e for e in events if e.get("type") == "error")
        assert "Settings" in err["message"]

    def test_invalid_key_pauses(self):
        result, n, _ = self._pause_and_recover(
            _keycall_error(ErrorCode.INVALID_API_KEY))
        assert result == "recovered"
        assert n == 2

    def test_nonretryable_code_raises_immediately(self):
        async def scenario():
            orch = _orchestrator()
            def fails():
                raise _keycall_error(ErrorCode.MODEL_NOT_AVAILABLE)
            with pytest.raises(KeyCallError):
                await orch._call_with_pause_on_failure(fails)
            # Never paused: the event stayed set.
            assert orch._pause_event.is_set()
        asyncio.run(scenario())


class TestUnattendedNeverPauses:
    """A paused batch row would hold its concurrency slot forever with
    nobody to resume it, so unattended runs propagate every failure."""

    @pytest.mark.parametrize("exc", [
        ResponseParseError("proposition: invalid JSON 3 times in a row"),
        _keycall_error(ErrorCode.TIMEOUT, retryable=True),
        _keycall_error(ErrorCode.PERMISSION_DENIED),
    ])
    def test_raises_instead_of_pausing(self, exc):
        async def scenario():
            orch = _orchestrator(unattended=True)
            def fails():
                raise exc
            with pytest.raises(type(exc)):
                await orch._call_with_pause_on_failure(fails)
            assert orch._pause_event.is_set()  # never cleared
        asyncio.run(scenario())


class TestProjectedBudgetStop:
    def _orch_with_history(self, history, spent, budget):
        orch = object.__new__(TurnOrchestrator)
        orch._turn_token_history = history
        orch._overrides = {}

        class _P:  # minimal stand-ins for config.protocol and token usage
            token_budget = budget
        class _C:
            protocol = _P()
        orch.config = _C()
        orch._spent_tokens = lambda: spent
        return orch

    def test_never_fires_before_a_full_turn_is_measured(self):
        orch = self._orch_with_history([], spent=99_999, budget=100_000)
        assert orch._projected_over_budget() is False

    def test_fires_when_the_next_turn_would_overshoot(self):
        # Last two turns cost 12k and 9k; 45k spent of a 50k budget: the 24k
        # projection (12k plus one retry of it) reaches 69k, so the turn must
        # not launch.
        orch = self._orch_with_history([12_000, 9_000], spent=45_000, budget=50_000)
        assert orch._projected_over_budget() is True

    def test_holds_when_the_budget_still_covers_a_turn(self):
        # 25k spent plus the 24k margin-inclusive projection stays under 50k.
        orch = self._orch_with_history([12_000, 9_000], spent=25_000, budget=50_000)
        assert orch._projected_over_budget() is False

    def test_projects_from_the_larger_of_the_last_two_turns(self):
        # Seats alternate, so the previous turn alone (2k, projecting 4k
        # with margin: under budget) would under-project the pricier seat's
        # turn (20k) that comes next.
        orch = self._orch_with_history([20_000, 2_000], spent=35_000, budget=50_000)
        assert orch._projected_over_budget() is True

    def test_retry_margin_fires_where_a_plain_projection_would_hold(self):
        # 35k spent, 10k turns, 50k budget: without the margin the projection
        # is 45k and the turn launches; one JSON-repair retry would then bill
        # a second full call and overshoot. The margin-inclusive 55k stops it.
        orch = self._orch_with_history([10_000, 10_000], spent=35_000, budget=50_000)
        assert orch._projected_over_budget() is True


class TestEndsInsteadOfRetryingForever:
    def test_reraises_when_force_close_is_set_while_paused(self):
        async def scenario():
            orch = _orchestrator()
            calls = {"n": 0}

            def always_fails():
                calls["n"] += 1
                raise ResponseParseError("proposition (claude-sonnet-5): invalid JSON 3 times in a row")

            async def end_debate_shortly():
                # Mirrors POST /debates/{id}/end: sets both events together.
                while orch._pause_event.is_set():
                    await asyncio.sleep(0)
                orch._pause_event.set()
                orch._force_close_event.set()

            ender = asyncio.create_task(end_debate_shortly())
            with pytest.raises(ResponseParseError):
                await orch._call_with_pause_on_failure(always_fails)
            await ender

            # Ended, not retried: the human chose to stop, not try again.
            assert calls["n"] == 1

        asyncio.run(scenario())
