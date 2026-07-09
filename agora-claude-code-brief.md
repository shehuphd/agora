# Agora — Claude Code scaffolding brief

## What this is

Agora is a structured multi-agent debate system. Two LLM agents argue a proposition using a formal typed-act protocol, overseen by a Moderator agent that enforces legal act sequences and termination conditions, then closed by a Synthesiser agent that produces an argument map. The core research question: does structured inter-agent communication with typed acts, explicit challenges, and enforced concessions produce measurably better epistemic outputs than a simple sequential pipeline?

This is not a content writing tool. It's an argumentation system. The output of a debate is an argument map showing which claims survived, which were revised under pressure, and which remained contested at closure — not a synthesised essay.

The protocol is grounded in Rapoport's Rules (formulated by game theorist Anatol Rapoport, later popularised by Daniel Dennett): before the Opposition can challenge a claim, it must first restate that claim so accurately and charitably that the Proposition would say "yes, that's exactly what I meant." This is expressed in the grammar as a STEELMAN act, which is togglable via config.

GitHub: [https://github.com/shehuphd/agora](https://github.com/shehuphd/agora)

---

## Tech stack

| Layer | Choice | Notes |
| :---- | :---- | :---- |
| Backend | FastAPI \+ Uvicorn | Python, async-native |
| Realtime updates | Server-Sent Events (SSE) | Push act log updates to browser; no WebSockets needed |
| Frontend | Vanilla HTML \+ CSS \+ JS | Single static/ folder; no React, no npm, no build step |
| Persistent storage | SQLite via sqlite3 (stdlib) | One dialogue.db per debate run |
| Config | PyYAML \+ python-dotenv | YAML for thresholds/defaults; .env for API keys |
| State | Python dataclasses | Pure, no ORM |
| Validation | Pydantic | FastAPI already requires it |
| Testing | Pytest | Core state machine tests need no LLM or API key |
| LLM clients | anthropic, openai | Add google-generativeai when Gemini support lands |

Full requirements.txt:

fastapi

uvicorn\[standard\]

python-dotenv

pyyaml

pydantic

anthropic

openai

Zero npm. Zero build step. Zero framework beyond FastAPI.

---

## Project structure

agora/

├── core/

│   ├── state.py          \# DialogueState dataclass; apply\_act(); legal\_acts()

│   ├── grammar.py        \# LEGAL\_TRANSITIONS dict; act validation logic; steelman mode

│   ├── termination.py    \# All termination condition checks (hard \+ soft stops)

│   └── checkpoint.py     \# SQLite read/write; markdown export; session resume

│

├── agents/

│   ├── base.py           \# BaseAgent: receives DialogueState, returns Act

│   ├── proposition.py    \# Proposition agent (asserter)

│   ├── opposition.py     \# Opposition agent (critic/challenger)

│   ├── moderator.py      \# Watches state each turn; emits STATUS and CLOSE acts

│   └── synthesiser.py    \# Reads closed state; produces argument map

│

├── runners/

│   └── debate.py         \# Orchestrates the turn loop; calls agents; checkpoints after every act

│

├── api/

│   ├── main.py           \# FastAPI app; mounts static/; registers routers

│   ├── routers/

│   │   ├── debates.py    \# POST /debates; GET /debates/{id}; GET /debates (history list)

│   │   ├── stream.py     \# GET /debates/{id}/stream (SSE live act feed)

│   │   └── settings.py   \# GET /settings; POST /settings

│   └── models.py         \# Pydantic request/response schemas

│

├── static/

│   ├── index.html        \# Single-page shell; JS handles hash routing

│   ├── style.css         \# All styles; fully commented per section

│   └── app.js            \# View logic: routing, SSE connection, form handling

│

├── runs/                 \# Auto-created; one folder per debate run

│   └── 20260702\_a3f9k2\_are-ai-agents-reliable/

│       ├── config.yaml

│       ├── dialogue.db

│       ├── dialogue.md

│       └── state.json

│

├── config/

│   ├── schema.py         \# Pydantic model for config validation

│   └── defaults.yaml     \# Default thresholds, models, feature toggles

│

├── tests/

│   ├── test\_grammar.py       \# State machine unit tests; no LLM needed

│   ├── test\_termination.py   \# Termination condition unit tests

│   └── test\_checkpoint.py    \# SQLite read/write round-trip tests

│

├── .env                  \# NEVER commit; contains API keys

├── .env.example          \# Commit this; shows required key names

├── .gitignore            \# Must include: .env, runs/, \_\_pycache\_\_, \*.pyc, .DS\_Store

├── requirements.txt

└── README.md

---

## Core concepts

### The dialogue state

Every agent call receives the full DialogueState object and returns a single Act. The state is complete and self-contained at every turn; any agent joining mid-debate can reconstruct full context from it alone.

@dataclass

class DialogueState:

    session\_id: str

    turn: int

    phase: str                         \# assert / steelman / challenge / revise / concede / propose / closed

    claims: dict\[str, Claim\]           \# claim\_id \-\> Claim

    acts: list\[Act\]                    \# ordered full act log

    outstanding\_challenges: list\[str\]  \# act\_ids of unresolved CHALLENGE acts

    next\_agent: str                    \# "proposition" | "opposition" | "moderator" | "synthesiser"

    legal\_acts: list\[str\]              \# enforced by grammar.py

    token\_usage: dict\[str, TokenUsage\] \# per-agent running totals

    steelman\_mode: bool                \# whether STEELMAN acts are required before CHALLENGE

### Typed acts

The protocol grammar defines act types and the legal transitions between them. Each act must reference what it targets. The state machine in grammar.py enforces transitions; an illegal act raises a ValueError immediately. No LLM can bypass the protocol.

Two grammar modes exist, selected by the `require_steelman` config toggle.

**Standard mode** (require\_steelman: false):

LEGAL\_TRANSITIONS \= {

    "ASSERT":    \["CHALLENGE", "CONCEDE"\],

    "CHALLENGE": \["REVISE", "DEFEND", "CONCEDE"\],

    "REVISE":    \["CHALLENGE", "CONCEDE", "PROPOSE"\],

    "DEFEND":    \["CHALLENGE", "CONCEDE"\],

    "CONCEDE":   \["PROPOSE", "ASSERT"\],

    "PROPOSE":   \[\]  \# terminal; triggers Moderator closure check

}

**Rapoport mode** (require\_steelman: true):

LEGAL\_TRANSITIONS \= {

    "ASSERT":            \["STEELMAN"\],

    "STEELMAN":          \["ACCEPT\_STEELMAN", "REJECT\_STEELMAN"\],

    "ACCEPT\_STEELMAN":   \["CHALLENGE", "CONCEDE"\],

    "REJECT\_STEELMAN":   \["STEELMAN"\],       \# Opposition must restate and try again

    "CHALLENGE":         \["REVISE", "DEFEND", "CONCEDE"\],

    "REVISE":            \["CHALLENGE", "CONCEDE", "PROPOSE"\],

    "DEFEND":            \["CHALLENGE", "CONCEDE"\],

    "CONCEDE":           \["PROPOSE", "ASSERT"\],

    "PROPOSE":           \[\]

}

In Rapoport mode, the Opposition must emit a STEELMAN act before any CHALLENGE. The STEELMAN contains the Opposition's restatement of the Proposition's claim in its strongest possible form. The Proposition responds with ACCEPT\_STEELMAN (yes, that's my position accurately) or REJECT\_STEELMAN (that misrepresents me; here's the correction), which forces the Opposition to try again. Only after ACCEPT\_STEELMAN can the Opposition proceed to CHALLENGE.

This catches the most common failure mode in LLM debates: a model pattern-matching on surface features of a claim and attacking a slightly wrong version of it.

The STEELMAN/ACCEPT/REJECT exchange is stored in the act log as first-class data. This enables post-run analysis: how often does the initial steelman get rejected? Which models produce more accurate steelmans? Does a rejected steelman correlate with weaker subsequent challenges?

The Moderator enforces a max\_steelman\_attempts ceiling (default: 2). If the Opposition's steelman is rejected more than max\_steelman\_attempts times on a single claim, the Moderator emits a MODERATOR\_INTERVENTION act and advances the phase, logging the failure.

### The Moderator

The Moderator is a non-partisan agent. It never asserts a position. It runs at the end of every turn and emits a STATUS act summarising: open challenges, conceded claims, legal next acts, steelman rejection counts (in Rapoport mode), and whether any termination condition is met. If a termination condition is met it emits a CLOSE act with a closure\_reason field, then hands off to the Synthesiser.

### Termination conditions

Hard stops (any one triggers immediate closure):

- max\_turns reached  
- max\_time\_minutes elapsed  
- token\_budget exhausted  
- All claims closed (no open challenges remain)

Soft stops (Moderator calls closure when a configurable threshold of these are met):

- Outstanding challenges fall below min\_open\_challenges floor  
- Challenge rate drops below floor (fewer than N challenges in last 2 turns)  
- A PROPOSE act receives a CONCEDE response  
- Repetition detected: new claim is semantically equivalent to a previously challenged and revised claim (Moderator flags loop and forces closure)

Repetition detection uses a hash of normalised claim text for the lightweight version. A cosine similarity check on embeddings is a v2 upgrade.

### The Synthesiser

The Synthesiser only activates after the Moderator emits CLOSE. It reads the full act log and produces:

1. An argument map: per-claim resolution status (survived, revised, contested-at-closure)  
2. A prose summary explaining why each claim landed where it did  
3. A list of unresolved contested claims flagged for further evidence  
4. In Rapoport mode: a steelman quality assessment noting which steelmans were accepted first-try vs rejected, and whether rejection correlated with challenge quality

The Synthesiser never participates in the debate itself and cannot influence the outcome it summarises.

---

## Run ID format

YYYYMMDD\_XXXXXX\_shortened-debate-title

XXXXXX is a 6-character alphanumeric random string. The slug comes from the Moderator's auto-generated title (if enabled) or the user-entered debate title at setup. Example: 20260702\_a3f9k2\_are-ai-agents-reliable.

Each run gets its own folder under runs/ containing config.yaml, dialogue.db, dialogue.md, and state.json.

---

## Persistent storage schema

Three SQLite tables per dialogue.db. The state is fully reconstructable from these tables.

CREATE TABLE sessions (

    session\_id     TEXT PRIMARY KEY,

    created\_at     TEXT,

    status         TEXT,    \-- 'running' | 'paused' | 'closed'

    debate\_title   TEXT,

    topic          TEXT,

    closure\_reason TEXT,

    steelman\_mode  INTEGER  \-- 0 | 1; whether Rapoport mode was active for this run

);

CREATE TABLE acts (

    act\_id           TEXT PRIMARY KEY,

    session\_id       TEXT,

    turn             INTEGER,

    agent            TEXT,    \-- nickname of agent

    agent\_role       TEXT,    \-- 'proposition' | 'opposition' | 'moderator' | 'synthesiser'

    act\_type         TEXT,    \-- ASSERT | STEELMAN | ACCEPT\_STEELMAN | REJECT\_STEELMAN |

                              \-- CHALLENGE | REVISE | DEFEND | CONCEDE | PROPOSE |

                              \-- STATUS | CLOSE | MODERATOR\_INTERVENTION

    claim\_id         TEXT,

    target\_act\_id    TEXT,

    content          TEXT,

    reason           TEXT,

    input\_tokens     INTEGER,

    output\_tokens    INTEGER,

    model\_used       TEXT,

    timestamp        TEXT

);

CREATE TABLE claims (

    claim\_id       TEXT PRIMARY KEY,

    session\_id     TEXT,

    author         TEXT,

    content        TEXT,

    status         TEXT,    \-- 'open' | 'challenged' | 'revised' | 'conceded' | 'survived' | 'contested'

    last\_updated   TEXT,

    steelman\_attempts INTEGER DEFAULT 0  \-- count of REJECT\_STEELMAN acts targeting this claim

);

Token data lives on the acts table, per act, per model. This enables per-agent token breakdown, per-run totals, and cross-run cross-model analysis with simple SQL queries. The steelman\_attempts column on claims enables post-run analysis of steelman quality without joining the full act log.

### Checkpointing

Checkpoint after every act, not after every turn. A dropped connection mid-turn resumes from the last completed act.

def checkpoint(conn, state, act):

    write\_act\_to\_db(conn, act)

    update\_claim\_statuses(conn, state)

    write\_state\_json(state)       \# runs/{session\_id}/state.json

    append\_act\_to\_markdown(act)   \# runs/{session\_id}/dialogue.md

---

## API key handling

Keys load from .env at startup via python-dotenv. The YAML config never touches keys; it only references model names. Agent instantiation resolves keys from os.environ at runtime.

On startup, the settings endpoint checks which keys are present and returns their status. The frontend disables model dropdown options that lack a valid key and shows (key missing) alongside them.

.env.example (commit this):

ANTHROPIC\_API\_KEY=your\_key\_here

OPENAI\_API\_KEY=your\_key\_here

GOOGLE\_API\_KEY=your\_key\_here

.env (never commit; add to .gitignore on day one).

The GET /settings endpoint also returns the resolved absolute path to the .env file so the frontend can display it and build the correct OS-specific "open in Finder / Explorer" link.

---

## Token tracking

Capture input\_tokens and output\_tokens from every API response at the agent layer and write them to the acts table immediately.

- Anthropic: response.usage.input\_tokens / response.usage.output\_tokens  
- OpenAI: response.usage.prompt\_tokens / response.usage.completion\_tokens

Three display levels:

1. Per-agent breakdown in the debate header (live, via SSE)  
2. Per-run total in the history table  
3. All-time global total in the nav bar and settings page, with a resettable display counter (resets counter only; SQLite data persists)

The GET /settings endpoint returns { total\_input\_tokens, total\_output\_tokens } aggregated across all sessions since the last counter reset. The reset writes a token\_reset\_event to a separate meta table so the display counter resets without destroying underlying data.

---

## Five frontend screens

Hash-based client-side routing in app.js. No server-side routing.

| Route | Screen | Notes |
| :---- | :---- | :---- |
| \#/history | Debate history | Table of all runs; columns: run ID, title, participants, turns, tokens, status. Empty state invites first debate. |
| \#/new | New debate setup | Topic textarea, auto-title toggle, participant nicknames, model dropdowns per role, threshold sliders, token budget slider, Rapoport mode toggle |
| \#/confirm | Confirm and start | Summary card showing full config before run starts; "go back" or "start debate" |
| \#/debate/:id | Live debate view | SSE-fed act log; stat cards; termination progress bars; token header strip (total \+ per-agent); mid-run override bar; STEELMAN acts rendered with distinct purple pill in Rapoport mode |
| \#/settings | Settings | API key status; env path with OS-specific open link; global token total with input/output breakdown; default thresholds; default models; feature toggles including require\_steelman |

### Act log rendering in Rapoport mode

STEELMAN acts render with a distinct agent pill colour (purple, matching the Moderator palette) and an explanatory label: "steelman · restating proposition's claim". ACCEPT\_STEELMAN renders in green with "accepted · challenge may proceed". REJECT\_STEELMAN renders in amber with "rejected · Opposition must restate". MODERATOR\_INTERVENTION renders in the Moderator pill style with the reason for intervention.

---

## Configurable parameters (defaults.yaml)

protocol:

  min\_challenges: 2

  min\_concessions: 1

  max\_turns: 8

  max\_time\_minutes: 15

  token\_budget: 100000

  repetition\_tolerance: 1

  require\_full\_resolution: false

  require\_steelman: false        \# set true to enforce Rapoport mode (STEELMAN before CHALLENGE)

  max\_steelman\_attempts: 2       \# max REJECT\_STEELMAN before Moderator intervenes

agents:

  proposition:

    model: claude-sonnet-4-6

    temperature: 0.7

    max\_claims: 5

  opposition:

    model: gpt-4o

    temperature: 0.4

    aggression: 0.8              \# 0.0-1.0; affects how many borderline claims get challenged

  moderator:

    model: claude-opus-4-6

    temperature: 0.3

    auto\_generate\_title: true

output:

  generate\_markdown: true

  store\_argument\_trace: true

  score\_final\_output: true

Mid-run overrides write a one-shot overrides.yaml the Moderator checks at the start of each turn, applies, logs as a typed act, then deletes. Note: require\_steelman cannot be toggled mid-run; it's fixed at debate start.

All parameters are tunable via the settings UI and the per-debate setup screen. Sliders and dropdowns write to defaults.yaml on save. Per-debate config saves to runs/{session\_id}/config.yaml at run start.

---

## CSS and code comments

Every CSS rule carries a comment marking what it controls and flagging values worth tweaking. Every Python module carries a docstring. Every function carries an inline comment explaining its role in the protocol. The codebase should be readable by someone encountering it cold on GitHub.

---

## Architecture constraints

- core/ has zero UI dependencies and zero LLM dependencies. All tests in tests/ run without an API key.  
- agents/ is the only layer that touches the Anthropic or OpenAI SDK.  
- api/ is the only layer that touches FastAPI or SSE.  
- static/ is the only layer that touches the DOM.  
- Swapping the UI from SSE to WebSockets is a single-file change in api/routers/stream.py.  
- Adding a new model provider means adding one new agent subclass in agents/ without touching the state machine.  
- Rapoport mode is a grammar-level flag, not a separate code path. The same state machine, act log, checkpoint, and Synthesiser handle both modes. The only difference is which LEGAL\_TRANSITIONS dict is active and which act types are rendered in the UI.

---

## Scaffold priority order

1. core/grammar.py and core/state.py with full unit tests in tests/ — implement both standard and Rapoport mode transitions; test all legal and illegal act sequences for both  
2. core/termination.py and core/checkpoint.py  
3. agents/base.py then each agent subclass  
4. runners/debate.py  
5. api/main.py and routers  
6. static/index.html, style.css, app.js  
7. config/defaults.yaml and .env.example  
8. README.md with setup instructions (install, add keys to .env, run uvicorn api.main:app \--reload)

---

## Theoretical grounding

The protocol draws from three formal traditions:

Argumentation theory (Dung, 1995): a set of arguments and attacks between them; an argument is accepted only if it survives all attacks. Agora operationalises this as the act log and claim status system.

Speech act theory (Austin/Searle): agents perform typed communicative acts (ASSERT, CHALLENGE, CONCEDE, etc.) rather than passing untyped text. The typed-act grammar constrains which act can follow which, making the exchange a genuine negotiation rather than a round-robin comment thread.

Rapoport's Rules / principle of charity: before critique, demonstrate genuine comprehension. Implemented as the optional STEELMAN exchange, togglable via require\_steelman. The accept/reject cycle is first-class data in the act log, enabling empirical measurement of whether the Opposition actually understood the position it challenged.

