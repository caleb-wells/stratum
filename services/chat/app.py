"""Stratum chat frontend (Chainlit).

Thin by design: it handles sign-in, saved conversations and presentation, and forwards
every question to the private retrieval API, which does routing, search and generation.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from datetime import datetime, timezone

import chainlit as cl
import httpx

API_URL = os.getenv("RAG_API_URL", "http://rag-api:8000")
API_KEY = os.getenv("RAG_API_KEY", "")
ENABLE_FABLE = os.getenv("ENABLE_FABLE", "false").lower() == "true"

# name shown in the picker -> (retrieval mode, answer model, description)
PROFILES: dict[str, tuple[str, str, str]] = {
    "Stratum · Opus 5.5": ("scoped", "opus",
        "Answers from each site's own local code, with sources. Claude Opus 5.5."),
    "Stratum · Fable 5.2": ("scoped", "fable",
        "The same approach, answered by Claude Fable 5.2."),
    "Baseline · Naive RAG": ("naive", "opus",
        "For comparison: searches all the documents at once."),
    "Baseline · No retrieval": ("none", "opus",
        "For comparison: no documents, model knowledge only."),
}
if not ENABLE_FABLE:
    PROFILES.pop("Stratum · Fable 5.2")
DEFAULT_PROFILE = next(iter(PROFILES))

STARTERS = [
    ("Slope stability factors of safety",
     "What factors of safety do we need for slope stability at each site, static and seismic?"),
    ("Fault setbacks",
     "How do we work out the fault setback at each site, and is there a minimum distance?"),
    ("Report validity at Site A",
     "How long is our geotechnical report good for at Site A, and what do we need to submit after that?"),
    ("Draft the Site B regulatory section",
     "Draft the 'Regulatory Requirements' section of the geologic hazards report for Site B. Cover fault investigation, slope stability and liquefaction, with the governing criteria."),
]

# ---------------------------------------------------------------- sign-in

def _accounts() -> dict[str, tuple[str, str, str]]:
    out = {}
    for prefix, role in (("ADMIN", "admin"), ("DEMO", "user")):
        email, password = os.getenv(f"{prefix}_EMAIL", ""), os.getenv(f"{prefix}_PASSWORD", "")
        if email and password:
            out[email.strip().lower()] = (password, role, os.getenv(f"{prefix}_NAME", email))
    return out


# Failed sign-ins are counted per real account, and all unknown emails share one bucket.
# The table therefore has at most (accounts + 1) entries: it cannot be flooded, and nothing
# an attacker sends can reset the count for a real account.
_failures: dict[str, list[float]] = {}
MAX_FAILURES, WINDOW_SECONDS = 8, 15 * 60
UNKNOWN = "(unknown account)"


@cl.password_auth_callback
def sign_in(username: str, password: str):
    """Accounts come from the environment. There is no self-registration."""
    key = username.strip().lower()[:200]
    account = _accounts().get(key)
    bucket = key if account else UNKNOWN
    now = time.time()
    recent = [t for t in _failures.get(bucket, []) if now - t < WINDOW_SECONDS]
    _failures[bucket] = recent
    if account and len(recent) >= MAX_FAILURES:
        return None                         # locked until the oldest failure ages out
    # compare against a dummy when the account is unknown so timing does not reveal which emails exist
    expected = account[0] if account else "x" * 24
    if hmac.compare_digest(password.encode(), expected.encode()) and account:
        _failures.pop(bucket, None)
        return cl.User(identifier=key, metadata={"role": account[1], "name": account[2]})
    _failures[bucket] = (recent + [now])[-MAX_FAILURES:]
    return None

# ---------------------------------------------------------------- picker and starters

@cl.set_chat_profiles
async def chat_profiles(user: cl.User | None = None):
    return [cl.ChatProfile(name=name, markdown_description=desc, default=(name == DEFAULT_PROFILE),
                           starters=[cl.Starter(label=label, message=msg) for label, msg in STARTERS])
            for name, (_, _, desc) in PROFILES.items()]

# ---------------------------------------------------------------- conversation state

@cl.on_chat_start
async def start():
    cl.user_session.set("history", [])


@cl.on_chat_resume
async def resume(thread: dict):
    """Rebuild the model's view of an earlier conversation from the saved messages."""
    history = []
    for step in thread.get("steps", []):
        text = (step.get("output") or "").strip()
        if step.get("type") == "user_message" and text:
            history.append({"role": "user", "content": text})
        elif step.get("type") == "assistant_message" and text:
            history.append({"role": "assistant", "content": text})
    cl.user_session.set("history", history)

# ---------------------------------------------------------------- answering

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _searched_summary(sources: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for s in sources:
        groups.setdefault(s["jurisdiction_name"], []).append(s)
    lines = []
    for name, items in groups.items():
        lines.append(f"**{name}** ({len(items)} passages)")
        seen = []
        for s in items:
            entry = f"{s['section'].rsplit(' > ', 1)[-1]} (p. {s['pages'][0]})"
            if entry not in seen:
                seen.append(entry)
        lines += [f"- {e}" for e in seen]
        lines.append("")
    return "\n".join(lines).strip()


def _search_label(mode: str, sources: list[dict]) -> str:
    n = len(sources)
    if mode != "scoped":
        return f"all documents ({n} passages)"
    names = []
    for s in sources:
        if s["jurisdiction_name"] not in names:
            names.append(s["jurisdiction_name"])
    return f"{', '.join(names)} ({n} passages)" if names else "no documents"


@cl.on_message
async def answer(message: cl.Message):
    history: list[dict] = cl.user_session.get("history") or []
    history.append({"role": "user", "content": message.content})
    mode, model, _ = PROFILES.get(cl.user_session.get("chat_profile") or "", PROFILES[DEFAULT_PROFILE])

    reply: cl.Message | None = None            # created at the first token, so it sits below the search step
    step: cl.Step | None = None
    sources: dict[int, dict] = {}
    elements: dict[str, cl.Pdf] = {}
    text = ""

    try:
        timeout = httpx.Timeout(connect=10, read=300, write=30, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{API_URL}/v1/chat", headers={"X-API-Key": API_KEY},
                                     json={"mode": mode, "model": model, "messages": history}) as resp:
                if resp.status_code != 200:
                    await cl.Message(content=f"The retrieval service returned an error ({resp.status_code}).").send()
                    history.pop()
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    kind = event["type"]
                    if kind == "status":
                        if step is None:          # the UI prefixes step names with "Using" / "Used"
                            step = cl.Step(name="the project documents", type="retrieval")
                            step.start = _now()          # without a start time the step sorts below the answer
                            await step.send()
                    elif kind == "sources":
                        sources = {s["n"]: s for s in event["sources"]}
                        if step is not None:
                            step.name = _search_label(mode, event["sources"])
                            step.output = _searched_summary(event["sources"]) or "No passages were needed."
                            step.end = _now()
                            await step.update()
                    elif kind == "delta":
                        if reply is None:
                            reply = cl.Message(content="")
                        text += event["text"]
                        await reply.stream_token(event["text"])
                    elif kind == "cite":
                        # one chip per cited passage; clicking it opens the source PDF at that page
                        labels = []
                        for n in event["sources"]:
                            s = sources.get(n)
                            if not s:
                                continue
                            label = s["label"]
                            if label not in elements:
                                elements[label] = cl.Pdf(name=label, url=f"/public/sources/{s['file']}",
                                                         page=s["pages"][0], display="side")
                            if label not in labels:
                                labels.append(label)
                        if labels and reply is not None:
                            chips = " " + " ".join(labels)
                            text += chips
                            await reply.stream_token(chips)
                    elif kind == "error":
                        reply = reply or cl.Message(content="")
                        await reply.stream_token(f"\n\n**{event['text']}**")
    except httpx.HTTPError as exc:
        reply = reply or cl.Message(content="")
        await reply.stream_token(f"\n\nCould not reach the retrieval service ({type(exc).__name__}).")

    reply = reply or cl.Message(content="No answer was produced. Please try again.")
    reply.elements = list(elements.values())
    await reply.send()
    history.append({"role": "assistant", "content": text})
    cl.user_session.set("history", history)
