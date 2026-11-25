"""HTTP API for the chat frontend. Private: reachable only inside the compose network."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import bedrock, config, db, project as proj
from .generate import _text, answer

MODES = {"scoped", "naive", "none"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    yield
    db.pool().close()


app = FastAPI(title="Jurisdiction-aware RAG API", lifespan=lifespan)


def require_key(x_api_key: str = Header(default="")):
    if x_api_key != config.INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


class ChatRequest(BaseModel):
    mode: str = "scoped"
    model: str = "opus"
    messages: list[dict]
    project_id: str = "wasatch-front-portfolio"


@app.get("/healthz")
def healthz():
    with db.conn() as c:
        n = c.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]
    return {"ok": True, "chunks": n}


@app.get("/v1/project", dependencies=[Depends(require_key)])
def get_project(project_id: str = "wasatch-front-portfolio"):
    try:
        return proj.load(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown project")


@app.get("/v1/usage", dependencies=[Depends(require_key)])
def get_usage():
    return {"tokens_today": db.tokens_used_today(), "daily_limit": config.DAILY_TOKEN_LIMIT}


class TaskRequest(BaseModel):
    messages: list[dict]


@app.post("/v1/task", dependencies=[Depends(require_key)])
async def task(req: TaskRequest):
    """Small background jobs from the chat UI (e.g. naming a chat). No retrieval."""
    if db.tokens_used_today() >= config.DAILY_TOKEN_LIMIT:
        return {"text": ""}
    msgs = [{"role": m["role"], "content": _text(m["content"])[:6000]} for m in req.messages[-6:]
            if m.get("role") in ("user", "assistant") and _text(m["content"]).strip()]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs:
        return {"text": ""}
    resp = await bedrock.claude_async().messages.create(
        model=config.ROUTER_MODEL, max_tokens=1000, messages=msgs)
    db.record_usage(resp.usage.input_tokens, resp.usage.output_tokens)
    return {"text": "".join(b.text for b in resp.content if b.type == "text")}


@app.post("/v1/chat", dependencies=[Depends(require_key)])
async def chat(req: ChatRequest):
    if req.mode not in MODES or req.model not in config.ANSWER_MODELS:
        raise HTTPException(status_code=400, detail="unknown mode or model")
    if not req.messages or req.messages[-1].get("role") != "user":
        raise HTTPException(status_code=400, detail="last message must be from the user")
    if len(req.messages) > config.MAX_REQUEST_MESSAGES:
        raise HTTPException(status_code=413, detail="conversation too long")
    try:
        proj.load(req.project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown project")

    async def events():
        async for event in answer(req.mode, req.model, req.messages, req.project_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
