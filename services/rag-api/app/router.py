"""Decide which jurisdictions a question concerns, and what to search for.

A small, fast model reads the project profile and the conversation and returns
a routing decision. If anything goes wrong, the safe fallback is to search every
jurisdiction in the project (still separately), never to guess one.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from . import bedrock, config, project as proj

PROMPT = """You route questions for a geotechnical engineering assistant.

{project}

Statewide guidance source (not local law): utah_ugs (Utah Geological Survey Circular 128).

Given the conversation, decide:
1. "jurisdictions": which local jurisdiction ids the latest question concerns. Use the site-to-jurisdiction mapping above. If the question is about the whole project, asks for a comparison, or does not name a site or place, include every local jurisdiction. Valid ids: {valid}.
2. "include_state": true if statewide guidance would help (general methodology, or when local rules may be silent). Usually true.
3. "question": the latest question restated as one standalone sentence, with pronouns and follow-ups resolved from the conversation, and without site names or jurisdiction names.
4. "queries": 1 to 3 short standalone search queries that capture what must be looked up, written without site names or jurisdiction names (those are applied as filters). Resolve pronouns and follow-ups using the conversation.
5. "needs_retrieval": false only for greetings or questions unrelated to engineering requirements.

Reply with one JSON object and nothing else."""


@dataclass
class Route:
    jurisdictions: list[str]
    include_state: bool
    queries: list[str]
    needs_retrieval: bool = True
    question: str = ""
    fallback: bool = False

    def dict(self) -> dict:
        return asdict(self)


def _conversation(messages: list[dict], limit: int = 6) -> str:
    out = []
    for m in messages[-limit:]:
        text = m["content"] if isinstance(m["content"], str) else " ".join(
            b.get("text", "") for b in m["content"] if isinstance(b, dict))
        out.append(f"{m['role'].upper()}: {text[:1500]}")
    return "\n\n".join(out)


def route(messages: list[dict], project: dict) -> Route:
    local = proj.local_jurisdictions(project)
    question = _conversation(messages[-1:], 1).split(": ", 1)[-1]
    fallback = Route(local, True, [question[:400]], True, question[:600], fallback=True)
    try:
        resp = bedrock.claude().messages.create(
            model=config.ROUTER_MODEL, max_tokens=1500,
            system=PROMPT.format(project=proj.describe(project), valid=", ".join(local)),
            messages=[{"role": "user", "content": _conversation(messages)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        chosen = [j for j in data.get("jurisdictions", []) if j in local] or local
        queries = [q.strip() for q in data.get("queries", []) if isinstance(q, str) and q.strip()][:3]
        return Route(chosen, bool(data.get("include_state", True)), queries or fallback.queries,
                     bool(data.get("needs_retrieval", True)),
                     str(data.get("question") or question)[:600])
    except Exception:
        return fallback
