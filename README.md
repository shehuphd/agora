# Agora — Structured Multi-Agent Debate System

Agora runs structured debates between LLM agents using a typed speech-act protocol. A Proposition agent asserts falsifiable claims, an Opposition agent challenges them across a rotating taxonomy, a Moderator enforces legal act sequences and termination conditions, and a Synthesiser produces an argument map after closure. Claims are grounded through live web search, every citation is verified against the actual search results, and every act records its tokens and dollar cost.

Free-form "debate me" prompts drift, repeat, and invent sources; Agora exists to make model-vs-model argument measurable. Any seat can run any model from Anthropic, OpenAI, Google, Perplexity, Moonshot, or xAI, including cross-provider matchups, and an experiment engine runs whole condition matrices (models × settings × replicates) as durable batches with per-condition comparison and a tidy dataset export. Everything runs locally with no services beyond the LLM APIs.

## Quick start

Check you have Python 3.10+ (`python3 --version`; install from [python.org/downloads](https://www.python.org/downloads/) if not).

macOS: double-click `launch.command`. It stops any instance still running, creates the virtual environment, installs dependencies when they change, picks a free port, and starts the server.

Or manually:

```bash
git clone https://github.com/shehuphd/agora
cd agora
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
cp .env.example .env          # add at least one LLM provider key
.venv/bin/uvicorn api.main:app --port 8502
```

Open [http://localhost:8502](http://localhost:8502), follow the onboarding wizard, and launch a debate from the New Debate screen. The debate view streams every act live with a token budget bar, a running dollar total, and pause/end controls.

## Documentation

- [USAGE.md](USAGE.md): the full manual, covering every screen, the protocol, grounding and citations, experiments, costs, exports, and troubleshooting
- [ARCHITECTURE.md](ARCHITECTURE.md): how it's built, from the turn loop to the data stores, batch engine, and module contracts
- [MANIFEST.md](MANIFEST.md): every source file and what it does
- [CHANGELOG.md](CHANGELOG.md): dated release history

By [Mo Shehu](https://mohammedshehu.com)
