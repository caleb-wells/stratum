"""Answer generation in three modes, streamed as simple events.

    scoped  - jurisdiction-partitioned retrieval (the design under test)
    naive   - one pooled search across all jurisdictions (typical RAG baseline)
    none    - no retrieval at all (model memory only)

Events yielded: {"type": "status"|"sources"|"delta"|"cite"|"done"|"error", ...}
"""
from __future__ import annotations

import time
from typing import AsyncIterator

from . import bedrock, config, db, project as proj
from .retrieval import Passage, search_pooled, search_scoped
from .router import Route, route

SYSTEM_GROUNDED = """You are a geotechnical engineering assistant helping a consulting firm prepare geologic-hazard and geotechnical reports for a project whose sites fall under different local jurisdictions.

{project}

You are given source passages as documents. Each document title starts with the jurisdiction it belongs to.

Rules:
- Requirements are jurisdiction-specific. State each jurisdiction's requirement separately and name the jurisdiction every time. Never apply one jurisdiction's requirement to another jurisdiction's site.
- Use only the provided documents for requirements, numbers, methods and definitions. Quote numbers and named methods exactly as written.
- If the documents for a jurisdiction do not address the point, say plainly that the loaded documents for that jurisdiction do not specify it. Do not fill the gap from another jurisdiction, from statewide guidance, or from general knowledge.
- Documents from the Utah Geological Survey are statewide guidance, not local law. Use them for context and label them as guidance.
- When jurisdictions differ, show the difference directly; a compact table is welcome when comparing three or more items.
- Wording: refer to each authority by its name (for example "Draper City") or as "the city" or "the local code". Use the word "jurisdiction" sparingly, at most once or twice in an answer.
- Be concise and practical. Write for a practicing geotechnical engineer. Do not add a list of sources at the end; citations are attached automatically.
- This is decision support. Where a requirement drives design, remind the reader once, briefly, to confirm against the currently adopted code."""

# The naive baseline is what a typical RAG setup looks like: one pooled search on the raw
# question, a generic "answer from the documents" prompt, and no jurisdiction handling.
SYSTEM_NAIVE = """You are a geotechnical engineering assistant helping a consulting firm prepare geologic-hazard and geotechnical reports.

{project}

Answer the question using the provided documents. Be concise and practical. Write for a practicing geotechnical engineer. Do not add a list of sources at the end; citations are attached automatically."""

SYSTEM_NONE = """You are a geotechnical engineering assistant helping a consulting firm prepare geologic-hazard and geotechnical reports for a project whose sites fall under different local jurisdictions.

{project}

Answer from your own knowledge. Be concise and practical. Write for a practicing geotechnical engineer."""

LIMIT_MESSAGE = ("The daily usage limit for this demo has been reached. It resets at midnight UTC. "
                 "Please try again tomorrow, or contact Caleb to raise the limit.")


def _document(p: Passage, plain: bool = False) -> dict:
    if plain:       # baseline: the document's own title and section, as a typical pipeline would pass
        return {"type": "document", "title": f"{p.doc_title} | {p.section_path}"[:480],
                "citations": {"enabled": True},
                "source": {"type": "text", "media_type": "text/plain", "data": p.text}}
    return {"type": "document", "title": p.title[:480], "citations": {"enabled": True},
            "context": f"Source: {p.doc_title}. Pages {p.page_start}-{p.page_end}. "
                       f"{'Statewide guidance, not local law.' if p.layer == 'state' else 'Local requirement.'}",
            "source": {"type": "text", "media_type": "text/plain", "data": p.text}}


def _file_for(url: str) -> str:
    return next((d["file"] for d in proj.manifest()["documents"] if d["url"] == url), "")


def _source(n: int, p: Passage) -> dict:
    return {"n": n, "id": p.id, "file": _file_for(p.url), "jurisdiction": p.jurisdiction, "jurisdiction_name": p.jurisdiction_name,
            "layer": p.layer, "title": p.title, "label": p.label, "doc_title": p.doc_title,
            "section": p.section_path,
            "pages": [p.page_start, p.page_end], "url": p.link, "text": p.text}


def _text(content) -> str:
    return content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def retrieve(mode: str, messages: list[dict], project: dict) -> tuple[Route | None, list[Passage]]:
    """Run routing + retrieval for a mode. Returns passages in prompt order."""
    if mode == "none":
        return None, []
    if mode == "naive":                         # no routing, no query rewriting: just the question
        question = _text(messages[-1]["content"])[:config.MAX_MESSAGE_CHARS]
        return None, search_pooled(question, [question[:1000]])
    r = route(messages, project)
    if not r.needs_retrieval:
        return r, []
    state = project.get("state_guidance", []) if r.include_state else []
    by_j = search_scoped(r.question, r.queries, r.jurisdictions, state)
    ordered = [p for j in r.jurisdictions + state for p in by_j.get(j, [])]
    return r, ordered


def build_request(mode: str, model_key: str, messages: list[dict], project: dict,
                  passages: list[Passage]) -> dict:
    template = {"none": SYSTEM_NONE, "naive": SYSTEM_NAIVE}.get(mode, SYSTEM_GROUNDED)
    system = template.format(project=proj.describe(project))
    history = [{"role": m["role"], "content": _text(m["content"])[:config.MAX_MESSAGE_CHARS]}
               for m in messages[:-1][-config.MAX_HISTORY_MESSAGES:]
               if m["role"] in ("user", "assistant") and _text(m["content"]).strip()]
    while history and history[0]["role"] != "user":      # a trimmed history must start with the user
        history.pop(0)
    question = _text(messages[-1]["content"])[:config.MAX_MESSAGE_CHARS]
    content: list[dict] = [_document(p, plain=(mode == "naive")) for p in passages]
    content.append({"type": "text", "text": question})
    return {
        "model": config.ANSWER_MODELS[model_key],
        "max_tokens": config.MAX_OUTPUT_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": history + [{"role": "user", "content": content}],
        "output_config": {"effort": config.ANSWER_EFFORT},
    }


async def answer(mode: str, model_key: str, messages: list[dict],
                 project_id: str = "wasatch-front-portfolio") -> AsyncIterator[dict]:
    started = time.monotonic()
    project = proj.load(project_id)
    question = _text(messages[-1]["content"])

    # Refuse before spending anything if this request could not fit in what is left today.
    recent = messages[-(config.MAX_HISTORY_MESSAGES + 1):]
    estimate = (sum(min(len(_text(m["content"])), config.MAX_MESSAGE_CHARS) for m in recent) // 3
                + config.MAX_OUTPUT_TOKENS
                + (0 if mode == "none" else config.RETRIEVAL_TOKEN_ALLOWANCE))
    if not db.reserve_tokens(estimate, config.DAILY_TOKEN_LIMIT):
        yield {"type": "delta", "text": LIMIT_MESSAGE}
        yield {"type": "done", "usage": {}, "limited": True}
        return

    import asyncio
    if mode != "none":
        yield {"type": "status", "text": "Finding the relevant codes", "done": False}
    r, passages = await asyncio.to_thread(retrieve, mode, messages, project)
    if mode != "none":
        if mode == "scoped":
            names = ", ".join(proj.jurisdiction_name(j) for j in r.jurisdictions)
            label = f"Searched separately: {names}" + (" + statewide guidance" if r.include_state else "")
        else:
            label = "Searched all documents as one pool"
        yield {"type": "status", "text": f"{label} ({len(passages)} passages)", "done": True}
        yield {"type": "sources", "sources": [_source(i + 1, p) for i, p in enumerate(passages)]}

    request = build_request(mode, model_key, messages, project, passages)
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        async with bedrock.claude_async().messages.stream(**request) as stream:
            pending: list[int] = []            # source numbers cited by the current text block
            async for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield {"type": "delta", "text": event.delta.text}
                    elif event.delta.type == "citations_delta":
                        n = event.delta.citation.document_index + 1
                        if n not in pending:
                            pending.append(n)
                elif event.type == "content_block_stop" and pending:
                    yield {"type": "cite", "sources": pending}     # passages backing the text just sent
                    pending = []
            final = await stream.get_final_message()
        usage = {"input_tokens": final.usage.input_tokens
                 + (final.usage.cache_read_input_tokens or 0)
                 + (final.usage.cache_creation_input_tokens or 0),
                 "output_tokens": final.usage.output_tokens}
        if final.stop_reason == "refusal":
            yield {"type": "delta", "text": "\n\nThe model declined to answer this request."}
        elif final.stop_reason == "max_tokens":
            yield {"type": "delta", "text": "\n\n_(Answer cut off at the length limit.)_"}
    except Exception as exc:                    # surface a readable error in the chat
        yield {"type": "error", "text": f"The model call failed: {type(exc).__name__}: {str(exc)[:300]}"}
        # Real usage is unknown after a failure, so the reservation stays counted in full.
        usage = {"input_tokens": estimate, "output_tokens": 0}

    latency = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(db.settle_usage, estimate, usage["input_tokens"], usage["output_tokens"])
    await asyncio.to_thread(
        db.log_request, mode=mode, model=request["model"], question=question[:2000],
        route=r.dict() if r else None, chunk_ids=[p.id for p in passages],
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], latency_ms=latency)
    yield {"type": "done", "usage": usage, "latency_ms": latency, "route": r.dict() if r else None}
