"""Settings router — API key status, config management, token reset, model registry."""
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
from traceact import ActionTrace
from providers import get_key_env, list_provider_names, test_key_async, list_models_async
from core import runs_db as _runs_db

# ------------------------------------------------------------------
# Key validation cache — avoids an API round-trip on every page load.
# Entries expire after 60 s or when the key value changes.
# ------------------------------------------------------------------
_key_cache: dict = {}  # {provider: {"key": str, "result": dict, "ts": float}}
_CACHE_TTL = 60  # seconds


async def _refresh_models_for_provider(provider: str, key: str, valid: bool) -> None:
    """After a key test, update provider_models in the DB to match current access."""
    with ActionTrace.start(action="provider.model_sync", kind="app", actor="system",
                           project="agora", meta={"provider": provider}) as t:
        t.input({"provider": provider, "valid": valid})
        conn = _runs_db.connect()
        _runs_db.init(conn)
        if valid:
            models = await list_models_async(provider, key)
            _runs_db.upsert_provider_models(
                conn, provider,
                [{"model_id": m.model_id, "display_name": m.display_name, "endpoint_type": m.endpoint_type}
                 for m in models],
            )
            t.output({"provider": provider, "model_count": len(models)})
        else:
            _runs_db.deactivate_provider_models(conn, provider)
            t.output({"provider": provider, "deactivated": True})
        conn.close()


async def _validate_all_keys(keys: dict) -> dict:
    """Validate all API keys concurrently; uses a 60 s per-value cache.

    On each validation, also refreshes the provider_models DB so the model
    picker stays in sync with what each key can actually access.
    """
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
        async def _run(provider: str, value: str):
            if provider == "serper":
                r = await asyncio.to_thread(_test_serper_key, value)
                _key_cache[provider] = {"key": value, "result": r, "ts": time.time()}
                return provider, r
            if provider == "brave":
                r = await asyncio.to_thread(_test_brave_key, value)
                _key_cache[provider] = {"key": value, "result": r, "ts": time.time()}
                return provider, r
            r = await test_key_async(provider, value)
            _key_cache[provider] = {"key": value, "result": r, "ts": time.time()}
            # Fire-and-forget model refresh (don't block the key status response).
            asyncio.create_task(_refresh_models_for_provider(provider, value, r.get("valid", False)))
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
    "agent_settings": {"history_window": 6, "chapter_every": 10},
    "ui": {"history_page_size": 50},
    "agents": {
        "proposition": {"temperature": 0.7, "max_claims": 5},
        "opposition":  {"temperature": 0.4, "aggression": 0.8},
        "moderator":   {"temperature": 0.3, "auto_generate_title": True},
        "synthesiser": {"temperature": 0.3},
    },
    "openai": {
        "responses_mode": "auto",
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

_SEARCH_KEY_ENVS = {
    "serper": "SERPER_API_KEY",
    "brave":  "BRAVE_API_KEY",
}


def _key_env_name(provider: str) -> str:
    """Return the env var name for a provider's API key. Raises 400 for unknown providers."""
    if provider in _SEARCH_KEY_ENVS:
        return _SEARCH_KEY_ENVS[provider]
    try:
        return get_key_env(provider)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


def _test_serper_key(key: str) -> dict:
    """Validate a Serper key with a minimal search."""
    import httpx
    try:
        r = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": "ping", "num": 1},
            timeout=10.0,
        )
        if r.status_code == 200:
            return {"present": True, "valid": True, "error": None}
        if r.status_code in (401, 403):
            return {"present": True, "valid": False, "error": "Invalid API key"}
        return {"present": True, "valid": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"present": True, "valid": False, "error": str(e)[:60]}


def _test_brave_key(key: str) -> dict:
    """Validate a Brave Search key with a minimal search."""
    import httpx
    try:
        r = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params={"q": "ping", "count": 1},
            timeout=10.0,
        )
        if r.status_code == 200:
            return {"present": True, "valid": True, "error": None}
        if r.status_code in (401, 403):
            return {"present": True, "valid": False, "error": "Invalid API key"}
        return {"present": True, "valid": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"present": True, "valid": False, "error": str(e)[:60]}


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
    """Sum token usage across all debate DBs in runs/, respecting any reset event."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    if not RUNS_DIR.exists():
        return totals
    for run_dir in RUNS_DIR.iterdir():
        db_path = run_dir / "debate.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT SUM(input_tokens), SUM(output_tokens) FROM acts"
                " WHERE timestamp > COALESCE("
                "  (SELECT value FROM meta WHERE key='token_reset_event'), '')"
            ).fetchone()
            conn.close()
            if row and row[0]:
                totals["input_tokens"] += row[0]
                totals["output_tokens"] += (row[1] or 0)
        except Exception:
            continue
    return totals


@router.get("/api/search-status")
async def search_status():
    """Which search tier will answer the next retrieval.

    'searxng' and 'serper' are flat-cost neutral tiers; 'provider' means
    retrieval falls back to token-billed vendor search — the UI warns on that.
    """
    from core import search as _search
    tier = await asyncio.to_thread(_search.active_tier)
    return {"tier": tier, "neutral": tier != "provider"}


@router.get("/settings")
async def get_settings():
    """Return API key validity, current config, global token totals, and env path."""
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    raw_totals = _total_tokens_from_runs()

    raw_keys = {
        p: (os.environ.get(get_key_env(p)) or "").strip()
        for p in list_provider_names()
    }
    for name, env in _SEARCH_KEY_ENVS.items():
        raw_keys[name] = (os.environ.get(env) or "").strip()
    key_info   = await _validate_all_keys(raw_keys)
    # key_status stays True only for valid keys — gates model dropdowns everywhere.
    key_status = {p: info["valid"] for p, info in key_info.items()}

    return {
        "key_info":   key_info,
        "key_status": key_status,
        # Env var per key, so the settings screen renders whatever providers are
        # registered instead of keeping its own list that a new adapter misses.
        "key_envs": {
            **{p: get_key_env(p) for p in list_provider_names()},
            **_SEARCH_KEY_ENVS,
        },
        # legacy fields
        "anthropic_key_present": key_status.get("anthropic", False),
        "openai_key_present":    key_status.get("openai", False),
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
    with ActionTrace.start(action="settings.save", kind="app", actor="user",
                           project="agora") as t:
        t.input({"keys_updated": list(updates.keys())})
        config = _load_config()
        _deep_merge(config, updates)
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        hw = config.get("agent_settings", {}).get("history_window")
        if hw is not None:
            from agents.base import set_history_window
            set_history_window(hw)
        from providers import configure as _configure_providers
        _configure_providers(config)
        t.output({"status": "ok"})
    return {"status": "ok", "config": config}


@router.post("/settings/reset-defaults")
async def reset_defaults():
    """Overwrite defaults.yaml with factory values."""
    with ActionTrace.start(action="settings.reset_defaults", kind="app", actor="user",
                           project="agora") as t:
        t.input({})
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(_FACTORY_DEFAULTS, f, default_flow_style=False)
        from agents.base import set_history_window
        set_history_window(_FACTORY_DEFAULTS["agent_settings"]["history_window"])
        t.output({"status": "ok"})
    return {"status": "ok", "config": _FACTORY_DEFAULTS}


@router.post("/settings/reset-tokens")
async def reset_tokens():
    """Write a token_reset_event to the meta table in all run DBs."""
    with ActionTrace.start(action="settings.reset_tokens", kind="app", actor="user",
                           project="agora") as t:
        t.input({})
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
        t.output({"reset_at": now, "databases_updated": count})
    return {"status": "ok", "reset_at": now, "databases_updated": count}


@router.post("/settings/keys")
async def update_key(payload: dict):
    """Write a single API key to .env. Payload: {provider: str, value: str}.

    Requires an explicit non-empty ``value``; omitting it is a 400, not a
    silent wipe of the existing key.
    """
    provider = payload.get("provider", "").lower()
    value    = (payload.get("value") or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Missing or empty 'value' field")
    env_name = _key_env_name(provider)  # raises 400 for unknown providers
    with ActionTrace.start(action="settings.key_save", kind="app", actor="user",
                           project="agora", meta={"provider": provider}) as t:
        t.input({"provider": provider, "key_present": bool(value)})
        if not _ENV_PATH.exists():
            _ENV_PATH.touch()
        dotenv_set_key(str(_ENV_PATH), env_name, value, quote_mode="never")
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        os.environ[env_name] = value
        _key_cache.pop(provider, None)
        if value:
            warnings = _load_key_warnings()
            if provider in warnings:
                del warnings[provider]
                with open(_WARNINGS_PATH, "w") as f:
                    json.dump(warnings, f)
        t.output({"status": "ok", "provider": provider, "key_present": bool(value)})
    return {"status": "ok", "provider": provider, "key_present": bool(value)}


@router.post("/settings/keys/{provider}/test")
async def test_key(provider: str):
    """Force re-validation of one API key, bypassing the cache.

    Also refreshes the provider_models DB so the model picker updates immediately.
    Returns key_info dict.
    """
    env_name = _key_env_name(provider)  # raises 400 for unknown providers
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    value = (os.environ.get(env_name) or "").strip()
    if not value:
        return {"present": False, "valid": False, "error": None}
    _key_cache.pop(provider, None)
    with ActionTrace.start(action="settings.key_validate", kind="app", actor="user",
                           project="agora", meta={"provider": provider}) as t:
        t.input({"provider": provider})
        results = await _validate_all_keys({provider: value})
        result = results[provider]
        t.output({"provider": provider, "valid": result.get("valid"), "error": result.get("error")})
    return result


@router.get("/api/models")
async def list_models():
    """Return all active models from the provider_models DB.

    The model picker fetches this instead of using a hardcoded list.
    Returns an empty list when no keys have been tested yet.
    """
    conn = _runs_db.connect()
    _runs_db.init(conn)
    cfg = _load_config()
    provider_order = cfg.get("providers", {}).get("model_order")
    models = _runs_db.list_available_models(conn, provider_order=provider_order)
    conn.close()
    return {"models": models}


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
    """Generate a random debate topic using the first available model from the DB.

    Rotates through all confirmed-available models in provider_models order until one
    succeeds. No provider or model is hardcoded — whoever the user has a working key
    for will be used.
    """
    load_dotenv(dotenv_path=_ENV_PATH, override=True)

    domain = random.choice(_TOPIC_DOMAINS)
    prompt = (
        f"Generate exactly one short, specific, debatable proposition in the domain of: {domain}. "
        "Requirements: suitable for a structured academic debate, under 20 words, falsifiable, "
        "genuinely controversial (reasonable people could sincerely argue either side), "
        "and phrased as a positive claim (e.g. 'X should Y' or 'X is Z'). "
        "Return only the proposition. No preamble, no quotation marks, no full stop at the end."
    )

    conn = _runs_db.connect()
    _runs_db.init(conn)
    _cfg = _load_config()
    available = _runs_db.list_available_models(conn, provider_order=_cfg.get("providers", {}).get("model_order"))
    conn.close()

    if not available:
        return {"ok": False, "error": "no models available — test an API key in Settings first"}

    import providers as _providers
    for model_row in available:
        prov  = model_row["provider"]
        mid   = model_row["model_id"]
        etype = model_row["endpoint_type"]
        key   = (os.environ.get(_providers.get_key_env(prov)) or "").strip()
        if not key:
            continue
        try:
            text, _, _ = _providers.generate(
                provider=prov, key=key, model_id=mid, endpoint_type=etype,
                system="", user=prompt, temperature=0.9, max_tokens=60,
            )
            topic = text.strip().strip('"').strip("'").strip()
            if topic:
                return {"ok": True, "topic": topic}
        except Exception:
            pass

    return {"ok": False, "error": "all available models failed to generate a topic"}


def _deep_merge(base: dict, updates: dict) -> None:
    """Recursively merge updates into base dict in-place."""
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
