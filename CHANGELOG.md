# Changelog

Agora ships from its main branch; each entry below is one pushed release. Versioning starts at 1.0.0 with the first entry cut from this changelog; the dated history before that predates versioning.

## Unreleased

### Added

- Experiment engine: stored designs (base config × factors × replicates) with deterministic matrix expansion, per-row cost preview, launch and extend (replicate numbering continues across launches), condition labels stamped on every generated run, and a manifest (package versions, price snapshot date, git commit) recorded at first launch.
- Durable batch execution: jobs and rows persist in the registry database, concurrency capped by `batch.concurrency` (1–3, default 2), rows left mid-flight by a dead server are marked interrupted, a retry button re-runs unfinished rows, and an optional spend ceiling stops launching new rows once recorded cost reaches it.
- Run metrics, computed at close from recorded data and backfilled for older runs: completion, closure reason, challenge count and taxonomy coverage, concessions, citation coverage, correction retries, argument-map presence, search calls and tiers. Acts now record `challenge_type` and `retries`.
- Condition comparison table (means with ranges across replicates) and a tidy `dataset.csv` export per experiment.
- Dollar-cost tracking on every surface, priced at generation time via [rates](https://pypi.org/project/rates): per-act cost chips, live in-debate total, pre-debate and per-CSV-row estimates, History and Experiments totals, lifetime spend in Settings, cost lines in exports. Unpriceable calls show as unknown, never zero; partial totals say they're minimums, and every figure states its price-snapshot date.
- xAI (Grok) as a sixth provider.
- CSV imports are saved onto their experiment as re-runnable designs.
- Quote contract on citations: every cited figure or attributed position carries a verbatim quote from the source, checked mechanically as a substring of the source's stored text (loose on surface form: "18%" matches "eighteen per cent", dash and quote variants are equivalent). A quote the source doesn't contain marks the citation unsourced, the same way a fabricated URL is stripped. On first citation of a source, its full page text is fetched once and stored in the pool for these checks.
- Number grounding: every number in a sentence that cites a source must appear inside that citation's quote, so a figure can't drift away from the context it came from without the mismatch being flagged.
- Citation-check results stay attached to the act (recorded in `debate.db`, shown to the moderator and the opposition), and the opposition's sourcing duty now includes comparing each citing sentence against its attached quote.
- Challenge lapsing: a challenge that was defended and then ignored by the opposition for 10 debater turns retires as lapsed — recorded, counted, and out of every prompt.
- Chapter summaries now reach the debaters and the moderator as the carrier for context older than the recent-history window, and the chapter list itself is capped at 10 (the oldest half collapses into one epoch summary).
- Judge scoring engine (`core/judge.py`): model-graded scores recorded data can't compute — citation fidelity (does each citing sentence claim what its verified quote says, about the same referent), argument-map quality against the turn record, and whether sampled concessions were justified by the defences. Single-vote or 3-vote majority; every call traced with its prompt, priced at call time, temperature 0; judgements stored per run and per config in `run_judgements`. Judging bills per run, so it only runs when explicitly invoked.
- Judge scoring wired into the app: defaults in `config/defaults.yaml` (`judge:` block — fidelity on a small model, map on a stronger one, 1 vote), a judge panel on every closed run whose button carries a rates-priced estimate before anything bills, and endpoints for estimate, start, and stored judgements. A run can be judged after the fact, and more than once.
- A mismatched quote now triggers one corrective retry before the act enters the record: the agent is shown each quote its cited source doesn't contain and told to replace it with a verbatim passage or drop the citation. The retry is recorded on the act as `citation_repairs` (its own counter, separate from parse retries), aggregated into run metrics and the experiment comparison table, so quote drift stays measurable whether or not the repair succeeded. A quote still mismatched after the repair is recorded as a violation, as before.
- A quote matching only the source's headline gets its own `title_only` status and no longer counts as verified — a title is stored text, so it passed the substring check while substantiating nothing (40% of one judged run's "verified" citations were headline quotes). Quotes must come from the source's description, excerpt, or page text.

### Changed

- All provider access now routes through [keycall](https://pypi.org/project/keycall) (registry-only routing; the five per-provider adapter modules are gone).
- Model responses that never parse, or that falsely claim their context was missing, now pause the debate for a human decision instead of crashing it, after bounded correction retries that always carry the original prompt content.
- Reasoning models get an 8192-token completion cap (a ceiling, not a spend), fixing empty turns from models whose hidden reasoning exhausted the old 2,048 cap.
- `max_turns` has one source of truth (`config/defaults.yaml`, default 100) across every surface; token budget, not turn count, is the intended stop.
- Documentation set expanded: USAGE.md, ARCHITECTURE.md, MANIFEST.md, this changelog.
- [traceact](https://pypi.org/project/traceact) upgraded to 1.5.0 (from 1.0.0): model events already carry `provider`, so the viewer now prices each call and totals a trace; each correction retry inside a turn (JSON repair, missing-context repair, quote repair) records traceact's attempt convention (`attempt`, `attempt_reason`, and a failed status on the try that was rejected), so the viewer groups the retried calls into one node and each keeps its own cost and timeline bar; cancellations record as `cancelled` rather than `failed`, and the viewer gains a timeline view per trace.
- [shiplock](https://pypi.org/project/shiplock) 0.3.0 release gate wired in (`shiplock.toml`), run by the test suite: docs-vs-code checks including banned-word sweeps over docs and source, architecture and manifest coverage, speech-act documentation coverage, duplicate dependency declarations, and assertion-free test detection.

### Fixed

- Transient provider failures (timeouts, rate limits, outages) and fixable credential failures (spend limits, invalid keys) pause an interactive debate for a fix-and-resume instead of ending it; unattended batch rows fail fast and stay retryable, so a paused row can't wedge a batch.
- A debate killed by a seat failure still closes properly: moderator close where possible, argument map from the record either way.
- Token budgets close ahead of themselves: a turn that would overshoot the budget is never launched, instead of one heavy final turn carrying a run 20%+ past it. The projection carries a retry margin (the heaviest recent turn, doubled), because a correction retry bills a full extra call after the launch decision — an 80k run measurably closed at 112% when one turn needed two retries.
- Double parse failures no longer kill a debate.
- Content-free "context was missing" acts no longer enter the record.
- Batch execution no longer runs every CSV row simultaneously.
- Per-turn token spend no longer grows with debate length: every seat's prompt now shows a bounded view of outstanding challenges (the 10 most recent in full, older ones as a count and taxonomy digest), the opposition's concede audit and URL-freshness set are bounded the same way, defence-turn lists are capped, and the moderator's turn cards are windowed with chapters carrying the earlier context. Measured before the fix: +195 tokens per turn per turn, linear, driven by the unbounded challenge ledger.
- Chapter summaries are no longer invisible: they persist in `state.json`, their provider calls are traced, and their tokens and cost are recorded in the run's totals (previously nine calls in a 100-turn run appeared nowhere).
- Agent traces now record the full system and user prompt bodies and the raw model response, so a trace answers "what did the model see and say" without replaying the run.
- `synthesiser_model` is now an accepted field when creating a debate; previously only the short alias `synth_model` was, and the long form was silently dropped, leaving the synthesiser on a fallback model.
- Chapter and epoch summary calls now carry the same hard timeout as agent calls; a hung provider connection inside one could previously freeze the whole run with no error.
- A debate no one is watching can't wedge any more: the event queue behind a created or continued debate is unbounded, so a run with no browser attached no longer freezes mid-turn once ~200 events accumulate.
- Challenge lapsing now keys on the thread, not the claim: a follow-up only keeps a challenge alive when it targets that challenge or one of its defences. Under the old claim-wide test, a single-claim debate could never lapse anything (measured: zero lapses in 100 turns with 50 challenges outstanding).
- Provider calls now allow 240 seconds of read time (up from the transport default of 60) and the per-call hard stop is 300 seconds (was 180): reasoning models measurably spend over 150 seconds thinking on mid-debate prompts, and the old limits cut them off mid-thought as spurious timeouts.
- An act that cites only through its structured citations array (no inline link in the prose) no longer escapes the checks: its numbers are grounded against the union of its quotes (reported as `ungrounded_act_numbers`), and judge scoring reads the whole act as the citing text.
- The launcher stops an instance that's still running before doing anything else, matching the server's own command line rather than checking whether a port is bound — a stale process from a closed terminal could previously keep serving old code on another port. It also checks for python3 with an install link, rebuilds a virtual environment whose interpreter no longer runs, reinstalls dependencies only when `requirements.txt` changes, skips browser-restricted ports, and calls pip and uvicorn through the virtual environment instead of trusting PATH.
- A user-requested end is now recorded as `user_requested_end`; the moderator's closure schema previously lacked the value and relabelled it.
- The experiment detail view has its own URL (`#/experiments/{id}`): reloading restores it, back/forward walk between list and detail, and a link to a deleted experiment says so instead of rendering a broken pane. Previously the detail was in-page state only and a reload fell back to the list.
- Deleting an experiment now confirms with a two-click in-app button (matching History's batch delete) instead of a browser dialog.

## Pre-versioning history

- **2026-08-09** — traceact 0.14.0 (queue tracing, SQLite viewer source).
- **2026-07-30** — traceact 0.12.0 (tool tracking, value redaction, in-flight streaming).
- **2026-07-28** — Registry-only model routing, Moonshot support, run pack export, traceact 0.10.0.
- **2026-07-27** — Neutral search layer (SearXNG/Brave/Serper) with shared evidence pool and grounded citations, onboarding wizard, CSV batch import, spurious-404 fix on closed debates, favicon.
- **2026-07-25** — traceact integration, experiments screen, multi-provider support, UI overhaul.
- **2026-07-10/11** — API key management with validation on load, debate controls, history improvements (batch delete, reset defaults), Gemini support, SSE restart detection, session→run rename.
- **2026-07-09** — Initial release: structured multi-agent debate system with typed speech-act protocol.
