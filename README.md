# Agora — Structured Multi-Agent Debate System

Agora runs structured debates between two LLM agents using a typed speech-act protocol. A Proposition agent asserts falsifiable claims, an Opposition agent challenges them across multiple dimensions, a Moderator enforces legal act sequences and termination conditions, and a Synthesiser produces an argument map after closure.

Supports any combination of Anthropic, OpenAI, and Google Gemini models — including cross-provider debates (e.g. Claude vs Gemini). Runs locally with no external services beyond the LLM APIs.

## Quick start

```bash
git clone https://github.com/shehuphd/agora
cd agora
pip install -r requirements.txt
cp .env.example .env          # add at least one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY
uvicorn api.main:app --reload --port 8502
```

Open [http://localhost:8502](http://localhost:8502).

## Screens

| Screen | Route | Description |
|--------|-------|-------------|
| History | `#/history` | All past sessions sorted by start time; click-sortable by title, turns, tokens, status; per-row export |
| New Debate | `#/new` | Full config: topic, models, nicknames, temperature, aggression, protocol thresholds |
| Confirm | `#/confirm` | Review all settings before launching |
| Debate View | `#/debate/:id` | Live act stream via SSE; token budget bar; termination tracker; pause and end controls |
| Settings | `#/settings` | API key status with inline editing; quota-exhaustion warnings; agent model defaults; protocol defaults; lifetime token counter |

## Architecture

| Layer | Directory | Responsibility |
|-------|-----------|----------------|
| Protocol | `core/` | State, grammar, termination, checkpointing, typed config |
| Agents | `agents/` | LLM wrappers: Proposition, Opposition, Moderator, Synthesiser |
| Runner | `runners/` | Async turn loop; SSE event queue; TurnOrchestrator |
| API | `api/` | FastAPI routers and Pydantic models |
| Frontend | `static/` | Hash-routed SPA — ES modules, no build step |

## Debate protocol

### Speech acts

| Act | Who | Description |
|-----|-----|-------------|
| ASSERT | Proposition | Introduce a falsifiable claim |
| CHALLENGE | Opposition | Attack a claim; multi-angle (up to 3 per act, one paragraph each) |
| REVISE | Proposition | Narrow or update a challenged claim |
| DEFEND | Proposition | Justify a challenged claim with new evidence |
| CONCEDE | Opposition | Yield a point — only after ≥3 challenge types used |
| PROPOSE | Proposition | Signal readiness to close |
| STEELMAN | Opposition | (Rapoport mode) Accurately restate proposition's claim before challenging |
| ACCEPT_STEELMAN | Proposition | Accept the restatement; challenge may proceed |
| REJECT_STEELMAN | Proposition | Reject the restatement; opposition must re-state |
| STATUS | Moderator | Summarise turn; flag sourcing gaps; track termination conditions |
| CLOSE | Moderator | End the debate with a closure summary and reason |
| ARGUMENT_MAP | Synthesiser | Structured post-debate analysis |

### Challenge taxonomy

The Opposition is required to rotate through challenge types and is explicitly prompted with randomly selected unused types each turn:

`sourcing` · `premise` · `causality` · `significance` · `definition` · `comparison` · `completeness` · `consistency`

### Modes

**Standard** — Proposition asserts, Opposition challenges, Proposition defends or revises, repeat.

**Rapoport** (steelman required) — Before every challenge, Opposition must accurately restate the proposition's claim. Proposition may accept or reject the restatement. Enforces charitable interpretation.

### Termination

Hard stops (immediate): `max_turns`, `max_time_minutes`, `token_budget`.

Soft stops (Moderator-evaluated): challenge rate floor below `min_challenges` across the session, PROPOSE met with CONCEDE, repetition of previously revised claim content.

User-requested end: the End button triggers an orderly close — the Moderator receives a `user_requested_end` signal, issues a CLOSE act, and the Synthesiser runs normally.

## Supported models

| Provider | Models |
|----------|--------|
| Anthropic | `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5` |
| OpenAI (GPT-5) | `gpt-5.6` (Sol), `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5` |
| OpenAI (GPT-4 / o-series) | `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o`, `gpt-4o-mini`, `o3`, `o3-mini`, `o4-mini`, `o1` |
| Google | `gemini-2.0-flash`, `gemini-1.5-pro`, `gemini-1.5-flash` |

Any agent role can be assigned any model from any provider. Model dropdowns are gated by key presence — a model whose provider key is absent is shown as disabled.

## Configuration

All thresholds are set per-run in the New Debate form and stored in the session record. Defaults are read from `config/defaults.yaml` and configurable in Settings.

| Field | Default | Description |
|-------|---------|-------------|
| `max_turns` | 20 | Hard turn ceiling |
| `max_time_minutes` | 15 | Wall-clock limit |
| `token_budget` | 100,000 | Aggregate token limit across all agents |
| `min_challenges` | 5 | Minimum challenge acts before soft-stop |
| `min_concessions` | 2 | Minimum concessions expected |
| `repetition_tolerance` | 2 | Max repeated claim cycles before closure |
| `aggression` | 0.5 | Opposition aggression: 0 = cautious, 1 = challenge everything |

## API keys

Keys are set in `.env`. The Settings screen shows whether each key is present and lets you paste a new key inline — no server restart needed. If a key causes a quota-exhaustion error mid-debate, a warning badge appears on that key in Settings and clears when the key is updated.

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
```

At least one key is required. Only providers with a key present will have their models available in the debate form.

## Output

Each run creates a directory under `runs/` containing:

- `debate.db` — SQLite database with `sessions`, `acts`, and `claims` tables
- `config.json` — full run config snapshot
- `overrides.json` — log of any mid-run adjustments

Export a run (JSON or Markdown) from the history table or the debate view.

## Security

All agent prompts use a system/user message split. Agent content never appears in the system prompt. An allowlist (`_ALLOWED_ACT_TYPES`) rejects any act type a role is not permitted to emit. Input is sanitised to strip structural tags before insertion into prompts. API keys are written only to the local `.env` file and never returned in API responses.

## Tests

```bash
pytest tests/
```

80+ tests covering: ActType enum, state transitions, `apply_act` for all act types, `_strip_and_parse`, allowlist enforcement, content truncation, and rolling history compaction.

## Requirements

- Python 3.10+
- At least one API key in `.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GOOGLE_API_KEY`
- No database server — SQLite per session under `runs/`
