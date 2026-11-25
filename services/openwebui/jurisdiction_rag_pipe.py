"""
title: Jurisdiction-Aware Geotech RAG
author: Caleb Wells
version: 1.0.0
description: Forwards chats to the private RAG API and shows which jurisdictions were searched, with a source chip for every cited passage.
"""
import json
import os

import aiohttp
from pydantic import BaseModel, Field

# (id, label shown in the model picker, mode, answer model)
MODELS = [
    ("scoped-opus", "Jurisdiction-aware RAG · Claude Opus 5.5", "scoped", "opus"),
    ("scoped-fable", "Jurisdiction-aware RAG · Claude Fable 5.2", "scoped", "fable"),
    ("naive-opus", "Baseline: naive RAG · Claude Opus 5.5", "naive", "opus"),
    ("none-opus", "Baseline: no retrieval · Claude Opus 5.5", "none", "opus"),
]


class Pipe:
    class Valves(BaseModel):
        RAG_API_URL: str = Field(default=os.getenv("RAG_API_URL", "http://rag-api:8000"))
        RAG_API_KEY: str = Field(default=os.getenv("RAG_API_KEY", ""))
        ENABLE_FABLE: bool = Field(default=os.getenv("ENABLE_FABLE", "false").lower() == "true",
                                   description="Show the Claude Fable 5.2 model in the picker")

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": mid, "name": name} for mid, name, _, model in MODELS
                if model != "fable" or self.valves.ENABLE_FABLE]

    async def pipe(self, body: dict, __event_emitter__=None, __task__=None):
        headers = {"X-API-Key": self.valves.RAG_API_KEY}
        messages = [{"role": m["role"], "content": m["content"]} for m in body.get("messages", [])
                    if m.get("role") in ("user", "assistant")]
        timeout = aiohttp.ClientTimeout(total=600, sock_read=300)

        # Background chores (chat titles etc.) go to a small model with no retrieval.
        if __task__:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.valves.RAG_API_URL}/v1/task", headers=headers,
                                        json={"messages": messages}) as resp:
                    yield (await resp.json()).get("text", "")
            return

        key = body.get("model", "").split(".")[-1]
        _, _, mode, model = next((m for m in MODELS if m[0] == key), MODELS[0])

        async def emit(kind: str, data: dict):
            if __event_emitter__:
                await __event_emitter__({"type": kind, "data": data})

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.valves.RAG_API_URL}/v1/chat", headers=headers,
                                        json={"mode": mode, "model": model, "messages": messages}) as resp:
                    if resp.status != 200:
                        yield f"The retrieval service returned an error ({resp.status})."
                        return
                    async for raw in resp.content:
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        kind = event["type"]
                        if kind == "delta":
                            yield event["text"]
                        elif kind == "status":
                            await emit("status", {"description": event["text"], "done": event["done"]})
                        elif kind == "sources":
                            for s in event["sources"]:
                                pages = s["pages"][0] if s["pages"][0] == s["pages"][1] else f"{s['pages'][0]}-{s['pages'][1]}"
                                await emit("citation", {
                                    "document": [s["text"]],
                                    "metadata": [{"source": s["url"], "jurisdiction": s["jurisdiction_name"],
                                                  "section": s["section"], "page": pages}],
                                    "source": {"name": f"{s['title']} (p. {pages})", "url": s["url"]},
                                })
                        elif kind == "error":
                            yield f"\n\n**{event['text']}**"
        except aiohttp.ClientError as exc:
            yield f"Could not reach the retrieval service: {type(exc).__name__}"
