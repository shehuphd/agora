# Manifest

Last updated: 2026-09-09 19:37:37 UTC

Every current source file, with what it defines and what it touches. A map for orienting in the codebase, not a copy of the docstrings.

## Entry points and configuration

| File | What it is |
|---|---|
| `launch.command` | macOS double-click launcher: stops a running instance by matching the server's command line, checks for python3, provisions and validates the venv, reinstalls dependencies only when `requirements.txt` changes, picks a free port (skipping browser-restricted ones), starts uvicorn from the venv. |
| `requirements.txt` | Python dependencies. FastAPI/uvicorn for the server, keycall for provider calls, traceact for tracing, rates for pricing, trafilatura for source excerpts. |
| `.env.example` | Template for `.env`: one key slot per LLM provider plus optional search-backend keys (Brave, Serper, SearXNG URL). |
| `config/defaults.yaml` | Default agent models/temperatures, protocol thresholds (max_turns, token_budget), batch concurrency, provider display order. Read by `core/config.py` and the settings router; edited by Settings saves. |
| `config/schema.py` | Pydantic validation for the structure of `defaults.yaml`. |
| `shiplock.toml` | Release-gate config for [shiplock](https://pypi.org/project/shiplock): public doc set, banned-word sweep over docs and source, architecture and manifest coverage, ActType-vs-USAGE coverage. |

## Agents (`agents/`)

| File | What it is |
|---|---|
| `base.py` | `BaseAgent`: prompt assembly (history window, chapter summaries, evidence pool, citation and quote contracts, bounded challenge views), provider calls with an 8192-token completion cap, JSON parsing with bounded correction retries that always carry the original context (each retry recorded as an attempt-tagged model event), act legality (per-role allowlist) and content-validity checks, URL and quote enforcement against the pool, per-act cost pricing, tracing with full prompt bodies. Defines `AgentResponseError` / `ResponseParseError` / `InvalidContentError`. |
| `proposition.py` | Proposition agent: asserts, revises, and defends falsifiable claims; structured citations with verbatim quotes. |
| `opposition.py` | Opposition agent: challenges across a rotating taxonomy, concedes under the protocol's conditions; bounded concede audit and URL-freshness set; referent checks on cited quotes. |
| `moderator.py` | Moderator agent: per-turn status (with the latest act's citation-check results), interventions, closure decisions, debate-title generation; windowed turn cards; own response parser. |
| `synthesiser.py` | Synthesiser agent: post-closure argument map, chapter and epoch summaries (traced and billed to state); own response parser. |

## Core protocol and services (`core/`)

| File | What it is |
|---|---|
| `state.py` | `DialogueState`, `Act`, `Claim`, `TokenUsage`, `ActType`, `apply_act` state transitions, and `lapse_stale_challenges` (defended-and-ignored challenges retire after 10 debater turns). The protocol's data model. |
| `citations.py` | Mechanical citation-fidelity checks: normalisation ("18%" ≡ "eighteen per cent"), verbatim-quote matching against stored source text, number grounding per citing sentence, and the per-act check report. |
| `grammar.py` | Legal act-transition rules; validates every move against the protocol. |
| `termination.py` | Hard stops (turns, time, token budget) and soft stops (challenge floor, PROPOSE/CONCEDE, repetition). |
| `checkpoint.py` | Per-run SQLite persistence (`debate.db`: runs, acts, claims, meta) and state snapshots; act rows carry tokens, cost, challenge_type, retry counts, and citation checks; state.json carries chapters, lapsed challenges, and auxiliary spend. |
| `config.py` | Typed per-run config (`DebateRunConfig`); single source of truth for the max_turns default, mirrored from `defaults.yaml`. |
| `runs_db.py` | The registry database (`databases/runs.db`): runs index, experiments (with spec + manifest columns), batch jobs and rows, run metrics, provider model registry, `resolve_model` routing. |
| `batch.py` | Batch engine: DB-backed jobs, semaphore-capped concurrency (1–3), interrupted-row stamping at startup, retry jobs, per-batch spend ceiling checked between row launches. |
| `spec.py` | Experiment specs: factor grid × replicates (or explicit rows) validated and deterministically expanded into condition-labelled batch rows; canonical field list shared with the CSV import. |
| `metrics.py` | Per-run metrics computed at close from recorded data (acts, sources, search log) into `run_metrics`; startup backfill for older runs. |
| `judge.py` | Judge scoring: model-graded citation fidelity (citing sentence vs verified quote), argument-map quality, and concession-earned verdicts on sampled chains; 1-vote or 3-vote majority; traced, priced, stored per judgement in `run_judgements`. Defaults from `config/defaults.yaml` (`judge:` block) with pre-spend estimates; explicit invocation only — judging bills per run. |
| `cost.py` | Dollar pricing seam over the `rates` registry: memoized price lookup, per-call cost, snapshot date; every miss degrades to None. |
| `search.py` | Neutral web-search layer: SearXNG → Brave → Serper → provider-fallback chain, plus page-excerpt enrichment. |
| `sources.py` | Shared per-run evidence pool; persists to `sources.json`; the only legal source of citations. Fetches and stores a cited source's full text once, on first citation, for quote checks. |
| `export.py` | JSON and Markdown transcript exports, including the recorded-cost total. |
| `runpack.py` | Run pack: the complete auditable record of one debate (calls, queries, sources, citation checks, cost breakdown). |

## Providers (`providers/`)

| File | What it is |
|---|---|
| `__init__.py` | Provider registry facade: `get_key_env`, dispatch into the keycall backend. |
| `base.py` | Shared value types for the provider layer (generation results, errors). |
| `keycall_backend.py` | The sole provider router: key validation, model listing, and generation for all six providers through keycall, with sampling-parameter retry fallbacks and web-search wiring. |

## Runner (`runners/`)

| File | What it is |
|---|---|
| `debate.py` | Async turn loop (`run_debate`, `TurnOrchestrator`): drives agents through the protocol, checkpoints every act, emits SSE events, pauses on recoverable failures (parse, transient provider, fixable credentials; unattended batch rows fail fast instead), closes ahead of a projected budget overrun, lapses stale challenges each turn, writes chapter summaries every 10 debater turns (capped at 10, oldest half collapsing into an epoch summary), always ends with a moderator close and argument map where possible, records final tokens/cost (including auxiliary calls) and run metrics at close. |

## API (`api/`)

| File | What it is |
|---|---|
| `main.py` | FastAPI app: routers, static file serving with no-store caching, startup (trace config, runs-index backfill, batch worker, price-registry warm, metrics backfill). |
| `models.py` | Pydantic request/response models (`DebateConfig`, cost-estimate request, and friends). |
| `routers/debates.py` | Create/read/continue/end debates, pre-debate cost estimate, judge endpoints (estimate, start, list judgements), batch delete/export, overrides, alive checks. |
| `routers/stream.py` | SSE stream of acts to the browser. |
| `routers/experiments.py` | Experiment CRUD, run assignment, spec save/preview/launch (with manifest recording), condition comparison, dataset CSV export. |
| `routers/batch.py` | CSV import (template, parse, row selection, budget ceiling), batch status polling, retry. |
| `routers/settings.py` | Key status and inline key updates, config save, model registry refresh, lifetime token/spend totals, model price enrichment. |
| `routers/traces.py` | Query the traceact log; launch the full viewer. |

## Frontend (`static/`)

| File | What it is |
|---|---|
| `index.html` | Single page shell: nav, screen containers, templates for all screens. |
| `app.js` | Router, settings screen, model pickers with price hints, new-debate form with live cost estimate, confirm screen. |
| `debate.js` | Live debate view: SSE consumption, token/cost accumulation, pause/end controls, continuation, judge panel on closed runs (priced button, score display). |
| `render.js` | Shared renderers: act bubbles, token strip, cost formatting. |
| `history.js` | History table: sorting, selection, exports, spend footer. |
| `experiments.js` | Experiments screen: list/detail, design panel and spec builder, matrix preview with estimates, launch/extend, comparison table, dataset export, CSV import with pre-flight estimates, batch polling and retry. |
| `onboarding.js` | First-launch wizard: key setup and search-backend guidance. |
| `traces.js` | Traces screen: inline queries and viewer launch. |
| `style.css` | All styling, light and dark themes via CSS tokens. |

## Tests (`tests/`)

| File | Covers |
|---|---|
| `test_state.py` | `apply_act` mutations, `ActType`. |
| `test_grammar.py` | Act-transition validation. |
| `test_termination.py` | Stop conditions. |
| `test_checkpoint.py` | SQLite persistence round-trips. |
| `test_base.py` | JSON parsing, correction retries carrying context, legality + validity checks, history window. |
| `test_adversarial.py` | Injection, malformed input, edge cases. |
| `test_keycall_backend.py` | Provider dispatch, retry fallbacks, key handling. |
| `test_model_routing.py` | Registry-only model resolution, unservable retirement. |
| `test_config.py` | Single-source max_turns resolution. |
| `test_pause_on_parse_failure.py` | Pause-and-retry on parse failures, transient provider errors, and fixable credential errors; unattended runs raise instead; projected budget stop. |
| `test_error_close_parity.py` | A debater failure still gets a moderator close and an argument map; clean runs stay clean. |
| `test_cost.py` | Price lookup, miss degradation, stable-tier preference, snapshot date. |
| `test_run_cost.py` | Per-run cost totals and partial semantics. |
| `test_cost_surfaces.py` | Lifetime spend, estimate provider resolution, SSE cost events, export cost line. |
| `test_batch_durable.py` | Batch persistence, interruption stamping, retry, budget ceiling, concurrency clamp. |
| `test_spec.py` | Spec validation, deterministic expansion, condition keys, CSV field alignment. |
| `test_metrics.py` | Metric computation, old-schema degradation, store and backfill. |
| `test_comparison.py` | Condition grouping/aggregation, condition threading, spec/manifest round-trip. |
| `test_export_formatting.py` | JSON-bearing act renderers in exports. |
| `test_runpack.py` | Full-record export. |
| `test_citations.py` | Quote contract and number grounding: normalisation equivalences, quote matching, per-act check statuses, enforcement pass, pool full-text fetch-once behaviour. |
| `test_context_bounds.py` | The fixed-size turn-context frame: audit window, defence-list caps, bounded URL freshness, challenge lapsing, chapters in prompts, turn-card window, chapter cap with epoch collapse. |
| `test_observability.py` | Prompt bodies in traces, the attempt convention on retried model events (clean first attempt untagged, retries grouped with reasons and failed status), chapters/lapsed/aux-cost in state.json, chapter and epoch calls traced and billed to state, aux cost in run totals. |
| `test_judge.py` | Judge scoring: verified-only item selection, sentence pairing, majority and median vote aggregation, chain assembly, JSON repair retry, failure holes, judgement storage round-trips. |
| `test_event_queues.py` | Created debates get an unbounded event queue, so a run with no SSE consumer can't wedge on a full queue. |
| `test_judge_endpoints.py` | Judge endpoints: estimate counts and prices, closed-runs-only guard, background judging stored and listed, double-start refusal, in-flight marker cleared on failure. |
| `test_docs.py` | Docs hygiene: the full shiplock gate plus an internal-reference sweep over tracked source. |
