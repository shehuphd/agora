"""Traces router — serves the traceact trace log."""
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from traceact import ActionTrace

router = APIRouter()

_TRACES_PATH = Path(__file__).parent.parent.parent / "data" / "traces" / "traces.jsonl"


@router.get("/api/launch-viewer")
async def launch_viewer(
    run_id: str = Query(default=None),
    action: str = Query(default=None),
    status: str = Query(default=None),
):
    """
    Check for a running TraceAct viewer. If one is already up, return its URL.
    If not, start one in the background (pointing at Agora's traces folder) and
    return the URL once it is ready.

    Any filters active in Agora's traces tab are passed through as pf_* params;
    since 0.6.0 the viewer answers pre-filters against the full source on disk,
    not just the live tail, so a run that scrolled out of the tail still shows.

    Returns {"url": "...", "ready": true} on success, or
    {"url": null, "ready": false, "error": "..."} on failure.
    """
    import asyncio

    def _launch() -> tuple[str, bool]:
        from traceact.viewer.instance import launch_or_connect, probe
        # Folder source: the viewer merges rotated segments with the active file.
        url = launch_or_connect(source=str(_TRACES_PATH.parent), name="agora")
        host_port = url.rstrip("/").rsplit(":", 1)
        port = int(host_port[-1]) if len(host_port) > 1 else 8765
        alive = probe("127.0.0.1", port) is not None
        return url, alive

    prefilters = {}
    if run_id:
        prefilters["pf_correlation_id"] = run_id
    if action:
        prefilters["pf_action"] = action
    if status:
        prefilters["pf_status"] = status

    with ActionTrace.start(action="viewer.launch", kind="app", actor="user",
                           ) as t:
        t.input({"source": str(_TRACES_PATH.parent), "prefilters": prefilters})
        try:
            loop = asyncio.get_event_loop()
            url, ready = await loop.run_in_executor(None, _launch)
        except Exception as exc:
            t.output({"ready": False, "error": str(exc)})
            return JSONResponse({"url": None, "ready": False, "error": str(exc)})

        if not ready:
            msg = "Viewer did not start. Try: traceact view data/traces/"
            t.output({"ready": False, "error": msg})
            return JSONResponse({"url": None, "ready": False, "error": msg})

        if prefilters:
            url = url.rstrip("/") + "/?" + urlencode(prefilters)
        t.output({"url": url, "ready": True})

    return JSONResponse({"url": url, "ready": True})


@router.get("/api/traces")
async def get_traces(
    run_id: str = Query(default=None),
    action: str = Query(default=None),
    status: str = Query(default=None),
    limit: int = Query(default=500, le=2000),
):
    """Query traces via TraceLog over the traces *folder*.

    The folder source merges rotated segments (traces.<timestamp>.jsonl) with
    the active file — reading only traces.jsonl would silently lose all history
    past the 50MB rotation point. query() is memory-bounded and reports whether
    the result is complete instead of guessing from its length.
    """
    from traceact import TraceLog

    folder = _TRACES_PATH.parent
    if not folder.exists():
        return JSONResponse({"traces": [], "total": 0, "more": False, "scan_capped": False})

    def _query() -> dict:
        log = TraceLog(str(folder), max_lines_scanned=200_000)
        filters = {}
        if run_id:
            filters["correlation_id"] = run_id
        if action:
            filters["action"] = action
        if status:
            filters["status"] = status
        if filters:
            log = log.filter(**filters)
        return log.query(limit)

    import asyncio
    result = await asyncio.get_running_loop().run_in_executor(None, _query)
    traces = result["traces"]
    return JSONResponse({
        "traces": traces,
        "total": len(traces),
        "more": result["limit_reached"],
        "scan_capped": result["scan_capped"],
    })
