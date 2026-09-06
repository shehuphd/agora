# Agora Usage Guide

The full manual. The [README](README.md) covers the quick start; this walks every screen and feature in the order you'll meet them.

## Before you start

You need Python 3.10+ (`python3 --version`; if that fails, install it from [python.org/downloads](https://www.python.org/downloads/)) and at least one LLM provider API key.

## Install and launch

macOS: double-click `launch.command`. It stops any instance still running, creates the virtual environment (rebuilding one whose interpreter no longer works), installs dependencies when `requirements.txt` changes, picks a free port, and opens the app.

Or manually:

```bash
git clone https://github.com/shehuphd/agora
cd agora
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env          # add at least one LLM key
.venv/bin/uvicorn api.main:app --port 8502
```

Open [http://localhost:8502](http://localhost:8502). On first launch an onboarding wizard walks through keys and search setup.

## API keys

Keys live in `.env`:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
PERPLEXITY_API_KEY=...
MOONSHOT_API_KEY=...
XAI_API_KEY=...
```

At least one is required. The Settings screen shows each key's status, validates it live, and lets you paste a new key inline with no restart. A key that hits a quota error mid-debate gets a warning badge that clears when the key is updated. Model dropdowns only offer models whose provider key is present and valid; the model list itself comes from each provider's live catalog, so it stays current as providers add or retire models.

## Your first debate

1. Open **New Debate** (`#/new`).
2. Enter a motion: a falsifiable statement, for example "Remote work improves software team productivity."
3. Pick a model for each seat (Proposition, Opposition, Moderator, Synthesiser). Any mix of providers works, including the same model on both sides.
4. Leave the protocol settings at their defaults the first time.
5. The **Confirm** screen shows every setting plus an estimated dollar cost with its assumptions and the price-snapshot date. Launch from there.

The debate view streams each act live: assertions with citations, challenges, the moderator's per-turn status, a token budget bar with a running dollar total, and a termination tracker. You can pause, resume, or end at any point; ending triggers an orderly close (the moderator issues a CLOSE act and the synthesiser still runs).

### Configuration fields

| Field | Default | Meaning |
|---|---|---|
| `max_turns` | 100 | Hard turn ceiling. Set high on purpose: the token budget is the intended stop. |
| `max_time_minutes` | 15 | Wall-clock limit. |
| `token_budget` | 100,000 | Aggregate token limit across all agents; the usual closure reason. |
| `min_challenges` | 5 | Challenge floor before a soft stop can fire. |
| `min_concessions` | 2 | Concessions expected before resolution counts. |
| `repetition_tolerance` | 2 | Repeated claim cycles allowed before closure. |
| `aggression` | 0.5 | Opposition posture: 0 cautious, 1 challenge everything. |
| `temperature_*` | per role | Sampling temperature per seat (dropped automatically for models that reject it). |
| `require_steelman` | off | Rapoport mode (see below). |

Defaults are read from `config/defaults.yaml` and editable in Settings.

## The debate protocol

### Speech acts

| Act | Who | Description |
|---|---|---|
| ASSERT | Proposition | Introduce a falsifiable claim |
| CHALLENGE | Opposition | Attack a claim; up to 3 angles per act |
| REVISE | Proposition | Narrow or update a challenged claim |
| DEFEND | Proposition | Justify a challenged claim with new evidence |
| CONCEDE | Opposition | Yield a point, only after 3+ challenge types used |
| PROPOSE | Proposition | Signal readiness to close |
| STEELMAN | Opposition | (Rapoport mode) Restate the claim before challenging |
| ACCEPT_STEELMAN / REJECT_STEELMAN | Proposition | Accept or reject the restatement |
| STATUS | Moderator | Per-turn summary and termination tracking |
| MODERATOR_INTERVENTION | Moderator | Out-of-band note |
| CLOSE | Moderator | End the debate with a reason |
| ARGUMENT_MAP | Synthesiser | Structured post-debate analysis |

The opposition rotates through a challenge taxonomy (`sourcing`, `premise`, `causality`, `significance`, `definition`, `comparison`, `completeness`, `consistency`) and each challenge records which type it used.

### Modes

**Standard**: assert, challenge, defend or revise, repeat. **Rapoport** (`require_steelman`): before every challenge the opposition must restate the claim accurately and the proposition must accept the restatement, enforcing charitable interpretation.

### Long debates

Two mechanisms keep per-turn spend flat instead of growing with debate length:

- **Chapter summaries.** Every 10 debater turns the synthesiser writes a short chapter summary. Debaters and the moderator see the chapters plus a recent-history window, rather than an ever-longer transcript. The chapter list itself is capped at 10; past that, the oldest half collapses into one epoch summary. Chapter calls are traced and their tokens and cost count in the run's totals.
- **Challenge lapsing.** A challenge that received a defence and then no opposition follow-up on that thread (the challenge itself or one of its defences) for 10 debater turns retires as "lapsed": recorded and counted, but out of every prompt. Undefended challenges never lapse. Prompts show the 10 most recent outstanding challenges in full and summarise the rest.

### Termination

Hard stops close immediately: `max_turns`, `max_time_minutes`, `token_budget`. The token budget also closes ahead of itself: when the next turn's projected spend (the larger of the last two turns, doubled to leave room for one correction retry) wouldn't fit, the debate closes under budget instead of overshooting it. Soft stops are moderator-evaluated: challenge rate below the floor, PROPOSE answered with CONCEDE, repetition beyond tolerance. The End button requests an orderly user-initiated close.

## Grounding and citations

Debaters search the web before speaking; each agent's own model writes the query. Search runs through a neutral backend so retrieval stays off the token budget:

| Tier | Config | Cost |
|---|---|---|
| SearXNG (preferred) | `SEARXNG_URL` in `.env`, or self-host via Docker | Free |
| Brave | `BRAVE_API_KEY` | ~$1 per 1k searches |
| Serper | `SERPER_API_KEY` | ~$1 per 1k searches |
| Provider fallback | none needed | Token-billed (warning shown) |

Results go into a shared evidence pool, the only legal source of citations: every URL an agent writes must come from the pool, and any URL that doesn't is stripped before the act is recorded, with the act marked unsourced. The raw search responses are logged per run (`search_log.jsonl`), so every citation is auditable later.

### The quote contract

The same construction applies one level up, to what an agent says a source says. Every cited figure or attributed position must carry a short verbatim quote from the source, and the quote is checked mechanically — no model judges it:

- On the first citation of a source, its full page text is fetched once (one HTTP call, no tokens) and stored in the pool.
- The quote must appear as a substring of the source's stored text. Matching is loose on surface form ("18%" matches "eighteen per cent", en dashes match hyphens, curly quotes match straight ones) and strict on the words and values themselves.
- Every number in a sentence that cites a source must also appear inside that citation's quote, so a figure can't be moved onto a different claim than the source made without the mismatch being flagged.

Each citation records a status: `verified` (the quote is in the source's description, excerpt, or page text), `title_only` (the quote matches only the source's headline, which substantiates nothing — treated as unsourced), `mismatch` (the source's text exists and the quote isn't in it — the act is annotated as unsourced, like a fabricated URL), `unverifiable` (a bot-walled or unreachable source with no stored text — surfaced, never penalised), `unpooled`, or `missing_quote`. An act that cites only through the citations array, with no inline link in its prose, gets act-level grounding instead: its numbers must appear in the union of its quotes, and a shortfall is reported as an `ungrounded_act_numbers` row. The statuses appear in the act record (`debate.db`), in the moderator's per-turn input, and in the opposition's view of the history, where its sourcing duty includes comparing each citing sentence against its attached quote.

A mismatch also triggers one corrective retry before the act is recorded: the agent is shown each failing quote and told to replace it with a verbatim passage or drop the citation. The retry bills one extra call, and the act records it in a `citation_repairs` counter (separate from parse `retries`) so quote drift stays measurable whether or not the repair fixed it. A quote still mismatched after the repair enters the record annotated as a violation, as before.

## Costs

Every model call is priced at the moment it happens, using [rates](https://pypi.org/project/rates), and the recorded figure is what every surface shows: per-act chips, the live in-debate total, History and Experiments totals, lifetime spend in Settings (resettable as a display counter; data stays), and a total line in exports. Estimates appear before a debate (Confirm screen, New Debate form) and before a batch (per row and per job), always stating their assumptions and the date of the price snapshot in use. A model rates doesn't list shows a dash: unknown, not zero, and any total containing unknowns is labelled a minimum.

## History and exports

The History screen (`#/history`) lists every run, sortable by title, turns, tokens, cost, and status, with batch select for delete or export. Per run you can export:

- **JSON** or **Markdown**: the transcript with token and cost totals.
- **Run pack**: the complete auditable record: every call, search query, source, citation check, and a cost breakdown.

A closed debate can be continued: the continuation starts a fresh run seeded with the original's state and sources, linked back via `continued_from`.

## Experiments

The Experiments screen (`#/experiments`) turns runs into comparable sets.

### Groups

Create an experiment, then assign runs to it (or start debates under it). The detail view lists its runs with status, turns, tokens, cost, and computed metrics, plus a spend total and average. Each experiment has its own URL (`#/experiments/{id}`): reloading restores it, back and forward walk between the list and the detail, and the link can be shared. Deleting an experiment unassigns its runs without deleting them, behind a two-click confirm on the button itself.

### Designs

An experiment can store a design: a base config plus factors and replicates.

1. Open an experiment and click **design**.
2. Set the base (topic, token budget), add factors: any config field with comma-separated levels, for example `proposition_model` with `kimi-k3, gpt-4.1`.
3. Set replicates (runs per condition).
4. **Preview matrix** shows every run a launch would enqueue, with a cost estimate per row and a total.
5. **Save design**, then **run design**.

Every combination of factor levels is one condition; each generated run is stamped with its condition and replicate number. **Run design** again adds a fresh replicate set; **extend** adds N more replicates per condition, numbering continued from the last launch. The first launch records a manifest (package versions, price snapshot date, git commit) shown on the experiment.

### CSV import

**Import** takes a CSV (template downloadable in the UI) with one debate per row; `topic` is the only required column and the rest mirror the config fields. Preview shows every row with a cost estimate and lets you tick which to run. The parsed rows are saved onto the experiment as a design, so a CSV batch is re-runnable later.

### Batch execution

Batches run a bounded number of debates at once (`batch.concurrency` in `config/defaults.yaml`, 1 to 3, default 2). Jobs survive restarts: rows a dead server left mid-flight are marked interrupted, and a **retry** button re-runs all unfinished rows as a new batch. An optional spend ceiling (the "ceiling $" field on import and launch) stops launching new rows once the recorded cost of the batch's closed runs reaches it; rows already running finish, and skipped rows say why.

### Metrics, comparison, and export

When a run closes, Agora computes metrics from its own recorded data at no model cost: completion, closure reason, challenge count and distinct taxonomy types used, concessions, citation coverage (share of assert/defend acts carrying pooled sources), correction retries, quote repairs, argument-map presence, search calls and tiers. Older runs are backfilled at startup.

For condition-labelled runs the experiment shows a comparison table: one row per condition with completion rate and means with ranges for turns, tokens, cost, citation coverage, retries, and quote repairs. Ranges, not significance tests: at typical replicate counts they're what the data supports. **Dataset** exports the whole experiment as a tidy CSV, one run per row with condition factors, every metric, and the manifest columns.

## Judge scores

A closed run can be judged: model-graded scores for what the recorded data can't compute mechanically. Open the run and use the judge panel at the bottom of the transcript — the button states the estimated dollar cost (priced by [rates](https://pypi.org/project/rates) from the run's own citation and concession counts) before anything bills. Three scores, each 0–1:

| Score | What it grades |
|---|---|
| Citation fidelity | For each mechanically verified citation: does the citing sentence claim what the quote says, about the same referent? |
| Argument map | The map against the turn record: claims covered, statuses right, summary faithful. |
| Concession chains | Sampled challenge → defence → concession chains: was the concession justified by the defences? |

Judge models and vote count come from the `judge:` block in `config/defaults.yaml` (default: a small model for fidelity and chains, a stronger one for the map, one vote per verdict). Every judge call is traced with its prompt and priced at call time; each judgement is stored with its per-item verdicts, so a score is auditable down to the sentence pair that produced it. A run can be judged again later — each judgement keeps its own row.

## Settings

Settings (`#/settings`) covers API keys (status, inline updates), search backend status, agent defaults (models, temperatures), protocol defaults, the display counters (lifetime tokens and spend, resettable without touching data), and the onboarding wizard.

## Traces

Every agent call, search, and app action is traced via [traceact](https://pypi.org/project/traceact) to `data/traces/traces.jsonl` (50MB rotation). The Traces screen queries them inline; **open viewer** launches traceact's full viewer pre-filtered to the selected run.

## Output on disk

Each run creates a directory under `runs/`:

| File | Contents |
|---|---|
| `debate.db` | SQLite: acts (with tokens, cost, challenge type, retries, quote repairs), claims, state |
| `config.json` | Full config snapshot, including the resolved provider per seat |
| `sources.json` | The evidence pool with extracted page excerpts |
| `search_log.jsonl` | Every raw search response |
| `overrides.json` | Mid-run adjustments, if any |

The cross-run index (experiments, batches, metrics, model registry) lives in `databases/runs.db`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A model is missing from the dropdowns | Its provider key is absent or failed validation. Test the key in Settings; the model list refreshes on a successful test. |
| Debate paused with "returned invalid JSON" or "claimed required context was missing" | The model failed every correction attempt. Resume to retry the turn, or end the debate; consider a different model for that seat. |
| Debate paused on a provider timeout, rate limit, spend limit, or invalid key | Nothing is lost. For a timeout or rate limit, wait a moment and Resume. For a spend limit or key problem, fix it in Settings first: keys are re-read on every call, so Resume continues the run. |
| Cost shows a dash | rates has no listing for that model (rolling aliases like `-latest` can't be pinned to a snapshot). The debate is unaffected. |
| Batch rows marked interrupted | The server stopped mid-batch. Click retry on the batch status to re-run them. |
| Batch rows marked skipped | The job's spend ceiling was reached; the row's note shows the recorded spend at that point. |
| "served by more than one provider" on launch | The same model id is offered by two vendors (for example kimi-k3 direct and resold). Pick the provider explicitly. |
| Search shows the token-billed warning | No neutral backend is configured. Set `SEARXNG_URL`, `BRAVE_API_KEY`, or `SERPER_API_KEY` in `.env`. |
