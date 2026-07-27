"""FastAPI application entry point for Agora."""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv
from traceact import configure as _configure_tracing, TraceConfig, JsonlSink, TraceBudget, TraceActASGIMiddleware

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

from api.routers import debates, stream, settings as settings_router, experiments as experiments_router, traces as traces_router, batch as batch_router

RUNS_DIR = Path(__file__).parent.parent / "runs"
_TRACES_PATH = Path(__file__).parent.parent / "data" / "traces" / "traces.jsonl"


@asynccontextmanager
async def lifespan(app: FastAPI):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _TRACES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _configure_tracing(
        config=TraceConfig(
            enabled=True,
            sink_mode="blocking",
            # Explicit rather than relying on the library default.
            capture_inputs=True,
            capture_outputs=True,
            redaction_presets=["api_keys"],
        ),
        budget=TraceBudget(max_events=500, max_steps=100, always_trace_errors=True),
        sinks=[JsonlSink(str(_TRACES_PATH), max_bytes=50_000_000)],
    )
    # Apply persisted settings at startup.
    from agents.base import set_history_window
    from providers import configure as _configure_providers
    cfg = settings_router._load_config()
    hw = cfg.get("agent_settings", {}).get("history_window", 6)
    set_history_window(hw)
    _configure_providers(cfg)
    # Initialise and backfill the runs index.
    import asyncio
    from core import runs_db as _runs_db
    await asyncio.get_running_loop().run_in_executor(None, _runs_db.backfill, RUNS_DIR)
    # Start the batch job worker.
    from core import batch as _batch
    await _batch.start_worker()
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
app.add_middleware(TraceActASGIMiddleware)

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
app.include_router(experiments_router.router)
app.include_router(traces_router.router)
app.include_router(batch_router.router)

# Serve static frontend at root — must be last
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
