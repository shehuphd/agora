"""Settings router — API key status, config management, token reset."""
import asyncio
import json
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
import yaml
from fastapi import APIRouter, HTTPException
from dotenv import load_dotenv, set_key as dotenv_set_key

# ------------------------------------------------------------------
# Key validation cache — avoids an API round-trip on every page load.
# Cache entries expire after 60 s or when the key value changes.
# ------------------------------------------------------------------
_key_cache: dict = {}  # {provider: {"key": str, "result": dict, "ts": float}}
_CACHE_TTL = 60  # seconds


def _test_anthropic_key(value: str) -> dict:
    try:
        import anthropic
        anthropic.Anthropic(api_key=value).models.list(limit=1)
        return {"present": True, "valid": True, "error": None}
    except Exception as e:
        msg = str(e).lower()
        friendly = "Invalid API key" if any(w in msg for w in ("auth", "invalid", "unauthorized", "403", "401")) else str(e)[:60]
        return {"present": True, "valid": False, "error": friendly}


def _test_openai_key(value: str) -> dict:
    try:
        from openai import OpenAI
        OpenAI(api_key=value).models.list()
        return {"present": True, "valid": True, "error": None}
    except Exception as e:
        msg = str(e).lower()
        friendly = "Invalid API key" if any(w in msg for w in ("auth", "invalid", "unauthorized", "incorrect", "403", "401")) else str(e)[:60]
        return {"present": True, "valid": False, "error": friendly}


def _test_google_key(value: str) -> dict:
    try:
        from google import genai
        client = genai.Client(api_key=value)
        for _ in client.models.list():
            break
        return {"present": True, "valid": True, "error": None}
    except Exception as e:
        msg = str(e).lower()
        friendly = "Invalid API key" if any(w in msg for w in ("api_key_invalid", "invalid", "unauthorized", "forbidden", "403", "401")) else str(e)[:60]
        return {"present": True, "valid": False, "error": friendly}


async def _validate_all_keys(keys: dict) -> dict:
    """Validate all API keys concurrently; uses a 60 s per-value cache."""
    now = time.time()
    result = {}
    to_validate = {}

    for provider, value in keys.items():
        if not value:
            result[provider] = {"present": False, "valid": False, "error": None}
            continue
        cached = _key_cache.get(provider)
        if cached and cached["key"] == value and now - cached["ts"] < _CACHE_TTL:
            result[provider] = cached["result"]
        else:
            to_validate[provider] = value

    if to_validate:
        _fns = {"anthropic": _test_anthropic_key, "openai": _test_openai_key, "google": _test_google_key}

        async def _run(provider: str, value: str):
            try:
                r = await asyncio.wait_for(asyncio.to_thread(_fns[provider], value), timeout=6.0)
            except asyncio.TimeoutError:
                r = {"present": True, "valid": False, "error": "Connection timed out"}
            _key_cache[provider] = {"key": value, "result": r, "ts": time.time()}
            return provider, r

        for provider, r in await asyncio.gather(*[_run(p, v) for p, v in to_validate.items()]):
            result[provider] = r

    return result

# Domain list for random topic generation. Picked server-side so the LLM
# can't default to AI / social media regardless of training biases.
_TOPIC_DOMAINS = [
    "criminal justice and prison reform",
    "climate change and environmental policy",
    "healthcare access and medical ethics",
    "education reform and schooling",
    "economic inequality and redistribution",
    "immigration and border policy",
    "religion, faith, and secularism",
    "international relations and geopolitics",
    "gender, sexuality, and identity",
    "philosophy of mind and consciousness",
    "bioethics and genetic engineering",
    "animal rights and welfare",
    "democracy, elections, and voting systems",
    "media, journalism, and misinformation",
    "drug legalisation and addiction policy",
    "urban planning, housing, and homelessness",
    "labour rights, unions, and gig work",
    "parenting, childhood, and family structure",
    "professional sport, doping, and fair competition",
    "art, culture, censorship, and public funding",
    "food systems, diet culture, and agriculture",
    "surveillance, privacy, and state power",
    "space exploration and science funding priorities",
    "historical legacy, reparations, and monuments",
    "mental health policy and psychiatric treatment",
    "military intervention, war, and pacifism",
    "taxation, public spending, and austerity",
    "corporate power, antitrust, and regulation",
    "representation, affirmative action, and diversity policy",
    "language, translation, and linguistic rights",
    "nuclear energy and the future of power",
    "capital punishment and the justice system",
    "intellectual property, copyright, and open access",
    "euthanasia, assisted dying, and end-of-life care",
    "universal basic income and welfare reform",
    "gun control and the right to bear arms",
    "free speech, hate speech, and platform governance",
    "obesity, public health mandates, and personal freedom",
    "globalisation, trade, and economic nationalism",
    "colonialism, decolonisation, and foreign aid",
    "celebrity culture, fame, and public influence",
    "gambling, lotteries, and risk-taking policy",
    "zoos, wildlife conservation, and captivity",
    "beauty standards, cosmetic surgery, and body autonomy",
    "organ donation: opt-in vs opt-out systems",
    "compulsory voting and civic duty",
    "inheritance, dynastic wealth, and intergenerational equity",
    "school uniforms, dress codes, and institutional identity",
    "professional sports salaries vs public sector pay",
]

_ENV_PATH = Path(".env").resolve()

router = APIRouter()

CONFIG_PATH    = Path(__file__).parent.parent.parent / "config" / "defaults.yaml"

_FACTORY_DEFAULTS = {
    "agent_settings": {"history_window": 6},
    "ui": {"history_page_size": 50},
    "agents": {
        "proposition": {"model": "claude-sonnet-4-6", "temperature": 0.7, "max_claims": 5},
        "opposition":  {"model": "claude-opus-4-8",   "temperature": 0.4, "aggression": 0.8},
        "moderator":   {"model": "claude-haiku-4-5",  "temperature": 0.3, "auto_generate_title": True},
    },
    "output": {
        "generate_markdown": True,
        "score_final_output": True,
        "store_argument_trace": True,
    },
    "protocol": {
        "max_steelman_attempts": 2,
        "max_time_minutes": 15,
        "max_turns": 20,
        "min_challenges": 5,
        "min_concessions": 2,
        "repetition_tolerance": 2,
        "require_full_resolution": False,
        "require_steelman": False,
        "token_budget": 40000,
    },
}
RUNS_DIR       = Path(__file__).parent.parent.parent / "runs"
_WARNINGS_PATH = Path(__file__).parent.parent.parent / "config" / "key_warnings.json"

_KEY_ENV_NAMES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "google":    "GOOGLE_API_KEY",
}


def _load_key_warnings() -> dict:
    try:
        if _WARNINGS_PATH.exists():
            with open(_WARNINGS_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _load_config() -> dict:
    """Load defaults.yaml and return as dict."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def _total_tokens_from_runs() -> dict:
    """Sum token usage across all debate DBs in runs/."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    if not RUNS_DIR.exists():
        return totals
    for run_dir in RUNS_DIR.iterdir():
        db_path = run_dir / "debate.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT SUM(input_tokens), SUM(output_tokens) FROM acts").fetchone()
            conn.close()
            if row and row[0]:
                totals["input_tokens"] += row[0]
                totals["output_tokens"] += (row[1] or 0)
        except Exception:
            continue
    return totals


@router.get("/settings")
async def get_settings():
    """Return API key validity, current config, global token totals, and env path."""
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    raw_totals = _total_tokens_from_runs()

    raw_keys = {
        "anthropic": (os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        "openai":    (os.environ.get("OPENAI_API_KEY")    or "").strip(),
        "google":    (os.environ.get("GOOGLE_API_KEY")    or "").strip(),
    }
    key_info   = await _validate_all_keys(raw_keys)
    # key_status stays True only for valid keys — gates model dropdowns everywhere.
    key_status = {p: info["valid"] for p, info in key_info.items()}

    return {
        "key_info":   key_info,
        "key_status": key_status,
        # legacy fields
        "anthropic_key_present": key_status["anthropic"],
        "openai_key_present":    key_status["openai"],
        "config": _load_config(),
        "token_totals": {
            "total":  raw_totals["input_tokens"] + raw_totals["output_tokens"],
            "input":  raw_totals["input_tokens"],
            "output": raw_totals["output_tokens"],
        },
        "env_path": str(Path(".env").resolve()),
        "platform": __import__("sys").platform,
        "key_warnings": _load_key_warnings(),
    }


@router.post("/settings")
async def update_settings(updates: dict):
    """Merge updates into defaults.yaml and persist."""
    config = _load_config()
    _deep_merge(config, updates)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    # Apply agent settings immediately so the running server reflects the change.
    hw = config.get("agent_settings", {}).get("history_window")
    if hw is not None:
        from agents.base import set_history_window
        set_history_window(hw)
    return {"status": "ok", "config": config}


@router.post("/settings/reset-defaults")
async def reset_defaults():
    """Overwrite defaults.yaml with factory values."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(_FACTORY_DEFAULTS, f, default_flow_style=False)
    from agents.base import set_history_window
    set_history_window(_FACTORY_DEFAULTS["agent_settings"]["history_window"])
    return {"status": "ok", "config": _FACTORY_DEFAULTS}


@router.post("/settings/reset-tokens")
async def reset_tokens():
    """Write a token_reset_event to the meta table in all run DBs."""
    now = datetime.utcnow().isoformat()
    count = 0
    if RUNS_DIR.exists():
        for run_dir in RUNS_DIR.iterdir():
            db_path = run_dir / "debate.db"
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path))
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        ("token_reset_event", now),
                    )
                    conn.commit()
                    conn.close()
                    count += 1
                except Exception:
                    continue
    return {"status": "ok", "reset_at": now, "databases_updated": count}


@router.post("/settings/keys")
async def update_key(payload: dict):
    """Write a single API key to .env. Payload: {provider: str, value: str}."""
    provider = payload.get("provider", "").lower()
    value    = (payload.get("value") or "").strip()
    if provider not in _KEY_ENV_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    env_name = _KEY_ENV_NAMES[provider]
    # Create .env if it doesn't exist yet.
    if not _ENV_PATH.exists():
        _ENV_PATH.touch()
    dotenv_set_key(str(_ENV_PATH), env_name, value)
    # Reload so the running process picks up the change immediately.
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    os.environ[env_name] = value
    # Bust the validation cache so the next GET /settings re-tests this key.
    _key_cache.pop(provider, None)
    # If the key is being set (non-empty), clear any existing quota warning for this provider.
    if value:
        warnings = _load_key_warnings()
        if provider in warnings:
            del warnings[provider]
            with open(_WARNINGS_PATH, "w") as f:
                json.dump(warnings, f)
    return {"status": "ok", "provider": provider, "key_present": bool(value)}


@router.post("/settings/clear-key-warning/{provider}")
async def clear_key_warning(provider: str):
    """Clear a quota-exhaustion warning for the given provider."""
    warnings = _load_key_warnings()
    if provider in warnings:
        del warnings[provider]
        with open(_WARNINGS_PATH, "w") as f:
            json.dump(warnings, f)
    return {"status": "ok"}


@router.post("/api/open-env")
async def open_env():
    """Create .env from .env.example if absent, then reveal it in the OS file manager."""
    import subprocess, sys, shutil
    from pathlib import Path
    env_path     = Path(".env").resolve()
    example_path = Path(".env.example").resolve()

    created = False
    try:
        # Create .env from .env.example if it doesn't exist yet
        if not env_path.exists() and example_path.exists():
            shutil.copy(example_path, env_path)
            created = True

        if sys.platform == "darwin":
            # -R reveals and selects the file in Finder
            subprocess.run(["open", "-R", str(env_path)], check=True)
        elif sys.platform == "win32":
            subprocess.run(["explorer", f"/select,{env_path}"], check=True)
        else:
            subprocess.run(["xdg-open", str(env_path.parent)], check=True)

        return {"ok": True, "path": str(env_path), "exists": True, "created": created}
    except Exception as e:
        return {"ok": False, "path": str(env_path), "exists": env_path.exists(), "created": created, "error": str(e)}


@router.post("/api/random-topic")
async def random_topic():
    """Generate a random debate topic using the fastest available LLM."""
    load_dotenv(dotenv_path=_ENV_PATH, override=True)

    domain = random.choice(_TOPIC_DOMAINS)
    prompt = (
        f"Generate exactly one short, specific, debatable proposition in the domain of: {domain}. "
        "Requirements: suitable for a structured academic debate, under 20 words, falsifiable, "
        "genuinely controversial (reasonable people could sincerely argue either side), "
        "and phrased as a positive claim (e.g. 'X should Y' or 'X is Z'). "
        "Return only the proposition. No preamble, no quotation marks, no full stop at the end."
    )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key    = os.environ.get("OPENAI_API_KEY")

    try:
        if anthropic_key:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            topic = resp.content[0].text.strip().strip('"').strip("'")
        elif openai_key:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            topic = resp.choices[0].message.content.strip().strip('"').strip("'")
        else:
            return {"ok": False, "error": "no API key available"}

        return {"ok": True, "topic": topic}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _deep_merge(base: dict, updates: dict) -> None:
    """Recursively merge updates into base dict in-place."""
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
