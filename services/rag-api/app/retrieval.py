"""Hybrid retrieval.

`search_scoped` is the point of the project: one search per jurisdiction, each
behind a hard metadata filter, so evidence from one jurisdiction can never be
returned for another. `search_pooled` is the naive baseline it is compared with.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pgvector import Vector

from . import bedrock, config, db

_STOP = set("a an and are as at be by do does for from how i in is it of on or our "
            "should that the their this to was we what when where which who will with "
            "under about need needs required require requirement requirements each all "
            "any can must site sites project".split())


@dataclass
class Passage:
    id: str
    jurisdiction: str
    jurisdiction_name: str
    layer: str
    doc_title: str
    doc_date: str | None
    url: str
    section_path: str
    page_start: int
    page_end: int
    text: str
    score: float = 0.0

    @property
    def title(self) -> str:
        return f"{self.jurisdiction_name} | {self.section_path}"

    @property
    def label(self) -> str:
        """Compact name for a source chip, e.g. "Draper 8.1.3 (p. 36)". The chip is narrow."""
        short = {"utah_ugs": "UGS guidance"}.get(
            self.jurisdiction, self.jurisdiction_name.replace(" City", ""))
        section = self.section_path.rsplit(" > ", 1)[-1]
        m = re.match(r"(Sec [\d-]+|[\d.]*\d[A-Z]?(?:\.\d+)*)\s", section)
        ref = m.group(1) if m else " ".join(section.split()[:3])
        return f"{short} {ref} (p. {self.page_start})"

    @property
    def link(self) -> str:
        return f"{self.url}#page={self.page_start}"


_COLS = ("id, jurisdiction, jurisdiction_name, layer, doc_title, doc_date, url, "
         "section_path, page_start, page_end, text")


def keyword_query(text: str) -> str:
    """OR together the meaningful terms, so one missing word does not zero the match."""
    terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", text.lower())
             if t not in _STOP and len(t) > 1]
    cleaned = {re.sub(r"[^a-z0-9]", " ", t).strip() for t in terms}
    parts = [" <-> ".join(c.split()) for c in cleaned if c]
    return " | ".join(sorted(parts))


def _hybrid(query: str, embedding: list[float], jurisdiction: str | None, limit: int) -> list[Passage]:
    """Vector + keyword candidates, merged with reciprocal rank fusion."""
    where = "WHERE jurisdiction = %(j)s" if jurisdiction else ""
    and_ = "AND jurisdiction = %(j)s" if jurisdiction else ""
    params = {"j": jurisdiction, "e": Vector(embedding), "q": keyword_query(query),
              "n": config.CANDIDATES_PER_SEARCH}
    with db.conn() as c:
        vec = c.execute(f"SELECT {_COLS} FROM chunks {where} "
                        "ORDER BY embedding <=> %(e)s LIMIT %(n)s", params).fetchall()
        kw = []
        if params["q"]:
            kw = c.execute(
                f"SELECT {_COLS} FROM chunks, to_tsquery('english', %(q)s) tq "
                f"WHERE tsv @@ tq {and_} ORDER BY ts_rank_cd(tsv, tq) DESC LIMIT %(n)s",
                params).fetchall()
    fused: dict[str, Passage] = {}
    for ranked in (vec, kw):
        for rank, row in enumerate(ranked):
            p = fused.setdefault(row["id"], Passage(**row))
            p.score += 1.0 / (config.RRF_K + rank + 1)
    return sorted(fused.values(), key=lambda p: p.score, reverse=True)[:limit]


def _rerank(query: str, candidates: list[Passage], top_n: int) -> list[Passage]:
    if not candidates:
        return []
    try:
        order = bedrock.rerank(query, [f"{p.section_path}\n{p.text}" for p in candidates], top_n)
    except Exception:                      # re-ranking is an improvement, not a dependency
        return candidates[:top_n]
    out = []
    for idx, score in order:
        candidates[idx].score = score
        out.append(candidates[idx])
    return out


def _search_one(question: str, queries: list[str], embeddings: list[list[float]],
                jurisdiction: str | None, top_n: int) -> list[Passage]:
    """Gather candidates from every search query, then re-rank against the real question."""
    merged: dict[str, Passage] = {}
    for q, e in zip(queries, embeddings):
        for p in _hybrid(q, e, jurisdiction, config.CANDIDATES_PER_SEARCH):
            if p.id in merged:
                merged[p.id].score += p.score          # found by several queries: stronger
            else:
                merged[p.id] = p
    candidates = sorted(merged.values(), key=lambda p: p.score, reverse=True)[:config.RERANK_POOL]
    return _expand_sections(_rerank(question, candidates, top_n))


def _expand_sections(hits: list[Passage]) -> list[Passage]:
    """If two or more hits are parts of one section, bring in that section's other parts.

    A requirement is often split across siblings (a formula in 8.1.1, its table of
    minimums in 8.1.3). When retrieval has clearly landed in a section, the
    neighbours are cheap to add and are what a human reader would look at next.
    """
    parents: dict[tuple[str, str, str], int] = {}
    for p in hits:
        if " > " in p.section_path:
            key = (p.jurisdiction, p.doc_title, p.section_path.rsplit(" > ", 1)[0])
            parents[key] = parents.get(key, 0) + 1
    have = {p.id for p in hits}
    extra: list[Passage] = []
    for (jurisdiction, doc_title, parent), n in sorted(parents.items(), key=lambda kv: -kv[1]):
        room = config.SECTION_EXPANSION - len(extra)
        if n < 2 or room <= 0 or parent.count(" > ") < 1:
            continue
        with db.conn() as c:
            rows = c.execute(
                f"SELECT {_COLS} FROM chunks WHERE doc_title = %s AND jurisdiction = %s "
                "AND starts_with(section_path, %s) AND NOT (id = ANY(%s)) ORDER BY seq LIMIT %s",
                (doc_title, jurisdiction, parent, list(have), room)).fetchall()
        for row in rows:
            extra.append(Passage(**row))
            have.add(row["id"])
    return hits + extra


def search_scoped(question: str, queries: list[str], local: list[str],
                  state: list[str]) -> dict[str, list[Passage]]:
    """One isolated search per jurisdiction. Returns {jurisdiction: passages}."""
    embeddings = bedrock.embed_many(queries)
    scopes = [(j, config.TOP_K_LOCAL) for j in local] + [(j, config.TOP_K_STATE) for j in state]
    with ThreadPoolExecutor(max_workers=max(1, len(scopes))) as pool:
        results = pool.map(lambda s: _search_one(question, queries, embeddings, s[0], s[1]), scopes)
    return {j: r for (j, _), r in zip(scopes, results)}


def search_pooled(question: str, queries: list[str]) -> list[Passage]:
    """Naive baseline: a single search over every jurisdiction at once."""
    return _search_one(question, queries, bedrock.embed_many(queries), None, config.TOP_K_NAIVE)
