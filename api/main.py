"""FastAPI application entry point for Agora."""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

load_dotenv()

from api.routers import debates, stream, settings as settings_router

RUNS_DIR = Path(__file__).parent.parent / "runs"


@asynccontextmanager
async def lifespan(app: FastAPI):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    import os
    missing = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if not os.getenv(k)]
    if missing:
        print(f"[agora] WARNING: missing API keys: {missing}. Add them to .env.", flush=True)
    # Apply persisted agent settings at startup.
    from agents.base import set_history_window
    cfg = settings_router._load_config()
    hw = cfg.get("agent_settings", {}).get("history_window", 6)
    set_history_window(hw)
    yield


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Add Cache-Control: no-store to JS/CSS/HTML responses so edits are reflected immediately."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path.split("?")[0]
        if path.endswith((".js", ".css", ".html")) or path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response


app = FastAPI(title="Agora Debate System", lifespan=lifespan)

app.add_middleware(NoCacheStaticMiddleware)

# Allow all origins for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(debates.router)
app.include_router(stream.router)
app.include_router(settings_router.router)

# Serve static frontend at root — must be last
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
