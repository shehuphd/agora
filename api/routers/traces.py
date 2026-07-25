"""Traces router — serves the traceact JSONL file."""
import json
from pathlib import Path
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from traceact import ActionTrace

router = APIRouter()

_TRACES_PATH = Path(__file__).parent.parent.parent / "data" / "traces" / "traces.jsonl"


@router.get("/api/launch-viewer")
async def launch_viewer():
    """
    Check for a running TraceAct viewer. If one is already up, return its URL.
    If not, start one in the background (pointing at Agora's traces file) and
    return the URL once it is ready.

    Returns {"url": "...", "ready": true} on success, or
    {"url": null, "ready": false, "error": "..."} on failure.
    """
    import asyncio

    def _launch() -> tuple[str, bool]:
        from traceact.viewer.instance import launch_or_connect, probe
        url = launch_or_connect(source=str(_TRACES_PATH))
        host_port = url.rstrip("/").rsplit(":", 1)
        port = int(host_port[-1]) if len(host_port) > 1 else 8765
        alive = probe("127.0.0.1", port) is not None
        return url, alive

    with ActionTrace.start(action="viewer.launch", kind="app", actor="user",
                           project="agora") as t:
        t.input({"source": str(_TRACES_PATH)})
        try:
            loop = asyncio.get_event_loop()
            url, ready = await loop.run_in_executor(None, _launch)
        except Exception as exc:
            t.output({"ready": False, "error": str(exc)})
            return JSONResponse({"url": None, "ready": False, "error": str(exc)})

        if not ready:
            msg = "Viewer did not start. Try: traceact view data/traces/traces.jsonl"
            t.output({"ready": False, "error": msg})
            return JSONResponse({"url": None, "ready": False, "error": msg})

        t.output({"url": url, "ready": True})

    return JSONResponse({"url": url, "ready": True})


@router.get("/api/traces")
async def get_traces(
    run_id: str = Query(default=None),
    action: str = Query(default=None),
    status: str = Query(default=None),
    limit: int = Query(default=500, le=2000),
):
    if not _TRACES_PATH.exists():
        return JSONResponse({"traces": [], "total": 0})

    traces = []
    with _TRACES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id and record.get("correlation_id") != run_id:
                continue
            if action and record.get("action") != action:
                continue
            if status and record.get("status") != status:
                continue
            traces.append(record)

    traces.sort(key=lambda t: t.get("started_at", ""), reverse=True)
    total = len(traces)
    return JSONResponse({"traces": traces[:limit], "total": total})
