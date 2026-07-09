# Agora — Structured Multi-Agent Debate System

Agora runs structured debates between two LLM agents using a typed speech-act protocol. A Proposition agent asserts falsifiable claims, an Opposition agent challenges them across multiple dimensions, a Moderator enforces legal act sequences and termination conditions, and a Synthesiser produces an argument map after closure.

Supports any combination of Anthropic and OpenAI models. Runs locally with no external services beyond the LLM APIs.

## Quick start

```bash
git clone https://github.com/shehuphd/agora
cd agora
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY and/or OPENAI_API_KEY
uvicorn api.main:app --reload --port 8502
```

Open [http://localhost:8502](http://localhost:8502).

## Screens

| Screen | Route | Description |
|--------|-------|-------------|
| History | `#/history` | All past sessions with status, tokens, closure reason, and per-row export |
| New Debate | `#/new` | Full config: topic, models, nicknames, temperature, aggression, protocol thresholds |
| Confirm | `#/confirm` | Review all settings before launching |
| Debate View | `#/debate/:id` | Live act stream via SSE; token budget bar; termination tracker |
| Settings | `#/settings` | API key status; default protocol config; lifetime token counter |

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
| CHALLENGE | Opposition | Attack a claim; multi-angle (up to 3 per act) |
| REVISE | Proposition | Narrow or update a challenged claim |
| DEFEND | Proposition | Justify a challenged claim with new evidence |
| CONCEDE | Opposition | Yield a point — only after ≥3 challenge types used |
| PROPOSE | Proposition | Signal readiness to close |
| STEELMAN | Opposition | (Rapoport mode) Accurately restate proposition's claim before challenging |
| ACCEPT_STEELMAN | Proposition | Accept the restatement; challenge may proceed |
| REJECT_STEELMAN | Proposition | Reject the restatement; opposition must re-state |
| STATUS | Moderator | Summarise turn; flag sourcing gaps; track termination conditions |
| CLOSE | Moderator | End the debate with a closure summary |
| ARGUMENT_MAP | Synthesiser | Structured post-debate analysis |

### Challenge taxonomy

The Opposition is required to rotate through challenge types and is explicitly prompted with randomly selected unused types each turn:

`sourcing` · `premise` · `causality` · `significance` · `definition` · `comparison` · `completeness` · `consistency`

### Modes

**Standard** — Proposition asserts, Opposition challenges, Proposition defends or revises, repeat.

**Rapoport** (steelman required) — Before every challenge, Opposition must accurately restate the proposition's claim. Proposition may accept or reject the restatement. Enforces charitable interpretation.

### Termination

Hard stops (immediate): `max_turns`, `max_time_minutes`, `token_budget`, all claims resolved with no outstanding challenges.

Soft stops (Moderator-evaluated): challenge rate floor below `min_challenges` across the session, PROPOSE met with CONCEDE, repetition of previously revised claim content.

## Configuration

All thresholds are set per-run in the New Debate form and stored in the session record:

| Field | Default | Description |
|-------|---------|-------------|
| `max_turns` | 15 | Hard turn ceiling |
| `max_time_minutes` | 30 | Wall-clock limit |
| `token_budget` | 40,000 | Aggregate token limit across all agents |
| `min_challenges` | 3 | Minimum challenge acts before soft-stop |
| `min_concessions` | 1 | Minimum concessions expected |
| `repetition_tolerance` | 1 | Max repeated claim cycles before closure |
| `aggression` | 0.8 | Opposition aggression: 0 = cautious, 1 = challenge everything |

## Output

Each run creates a directory under `runs/` containing:

- `debate.db` — SQLite database with `sessions`, `acts`, and `claims` tables
- `state.json` — latest DialogueState snapshot

Export a run (JSON or Markdown) from the history table or the debate view.

## Security

All agent prompts use a system/user message split. Agent content never appears in the system prompt. An allowlist (`_ALLOWED_ACT_TYPES`) rejects any act type a role is not permitted to emit. Input is sanitised to strip structural tags before insertion into prompts.

## Tests

```bash
pytest tests/
```

80 tests covering: ActType enum, state transitions, `apply_act` for all act types, `_strip_and_parse`, allowlist enforcement, content truncation, and rolling history compaction.

## Requirements

- Python 3.10+
- `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in `.env`
- No database server — SQLite per session under `runs/`
