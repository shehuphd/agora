"""SSE streaming endpoint — pushes acts to browser as they are generated."""
import json
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.routers.debates import get_queue

router = APIRouter()


@router.get("/debates/{session_id}/stream")
async def stream_debate(session_id: str):
    """Stream debate acts as Server-Sent Events until the debate closes."""
    try:
        queue = get_queue(session_id)
    except HTTPException:
        raise

    async def event_generator():
        """Yield SSE-formatted events from the session queue until None sentinel."""
        while True:
            act = await asyncio.wait_for(queue.get(), timeout=300)
            if act is None:
                # Sentinel received — debate has closed
                yield 'data: {"type": "close"}\n\n'
                break
            yield f"data: {json.dumps(act)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
