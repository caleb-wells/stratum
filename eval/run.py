"""Run the evaluation: every question, in three modes, graded against gold answers written from the source text.

    docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec rag-api \
        python /srv/eval/run.py            # all questions
        python /srv/eval/run.py --limit 3  # quick smoke run

Two measurements per answer:
  * retrieval hit  - did the passages for each expected jurisdiction contain the gold text?
                     (not applicable to the no-retrieval mode)
  * graded answer  - a separate model call compares the answer with the gold answer and
                     reports `correct` and `contaminated` (a requirement attributed to the
                     wrong jurisdiction, or invented for a jurisdiction that is silent).
Results are written to eval/results/ as JSON plus a Markdown table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, "/srv/services/rag-api")
from app import bedrock, config, db, project as proj  # noqa: E402
from app.generate import build_request, retrieve       # noqa: E402

HERE = Path(__file__).resolve().parent
MODES = ["none", "naive", "scoped"]
LABELS = {"none": "No retrieval", "naive": "Naive RAG (one pooled search)",
          "scoped": "Stratum (one search per regulation)"}

JUDGE = """You are grading an assistant's answer about geotechnical requirements for a project whose three sites fall under different jurisdictions: Site A = Draper City, Site B = Fruit Heights City, Site C = Ogden Valley.

Question:
{question}

Verified gold answer (from the source documents):
{gold}

Assistant's answer:
{answer}

Grade strictly against the gold answer.
- "correct": true only if the answer states the key requirement(s) in the gold answer with the right values, named methods and jurisdictions, and does not contradict it. Extra correct detail is fine. If the gold answer says the documents do not specify something, the answer is correct only if it says so (it may add clearly labelled guidance from elsewhere).
- "contaminated": true if the answer attributes a requirement, number or method to a jurisdiction it does not belong to, presents a general or another jurisdiction's value as this jurisdiction's requirement, or states a specific requirement for a jurisdiction where the gold answer says none is specified.
- "reason": one sentence.

Reply with one JSON object: {{"correct": bool, "contaminated": bool, "reason": str}}"""


async def ask(mode: str, question: str, project: dict) -> dict:
    messages = [{"role": "user", "content": question}]
    started = time.monotonic()
    route, passages = await asyncio.to_thread(retrieve, mode, messages, project)
    request = build_request(mode, "opus", messages, project, passages)
    resp = await bedrock.claude_async().messages.create(**request)
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {"answer": text, "passages": passages,
            "tokens_in": resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0)
                         + (resp.usage.cache_creation_input_tokens or 0),
            "tokens_out": resp.usage.output_tokens,
            "seconds": round(time.monotonic() - started, 1)}


async def grade(question: str, gold: str, answer: str) -> dict:
    resp = await bedrock.claude_async().messages.create(
        model=config.ANSWER_MODELS["opus"], max_tokens=2000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": JUDGE.format(question=question, gold=gold, answer=answer)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception:
        return {"correct": False, "contaminated": False, "reason": "grader output could not be parsed"}


def retrieval_hit(sources: dict, passages) -> bool | None:
    if not sources:
        return None
    passages = [p for p in passages if not p.matrix]      # search only: matrix cells are looked up, not found
    return all(any(p.jurisdiction == j and needle in p.text for p in passages)
               for j, needle in sources.items())


async def run_one(q: dict, mode: str, project: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        out = await ask(mode, q["question"], project)
        verdict = await grade(q["question"], q["gold"], out["answer"])
    hit = None if mode == "none" else retrieval_hit(q.get("sources") or {}, out["passages"])
    return {"id": q["id"], "kind": q["kind"], "mode": mode, "question": q["question"], "gold": q["gold"],
            "answer": out["answer"], "correct": bool(verdict.get("correct")),
            "contaminated": bool(verdict.get("contaminated")), "reason": verdict.get("reason", ""),
            "retrieval_hit": hit,
            "passages": [f"{p.jurisdiction}: {'matrix: ' + p.matrix if p.matrix else p.section_path}" for p in out["passages"]],
            "tokens_in": out["tokens_in"], "tokens_out": out["tokens_out"], "seconds": out["seconds"]}


def pct(rows: list[dict], key: str) -> str:
    rows = [r for r in rows if r[key] is not None]
    return f"{100 * sum(bool(r[key]) for r in rows) / len(rows):.0f}% ({sum(bool(r[key]) for r in rows)}/{len(rows)})" if rows else "n/a"


def report(rows: list[dict]) -> str:
    kinds = ["single", "compare", "silent", "draft"]
    lines = ["| Setup | Correct | Wrong-regulation errors | Retrieved the right section | Avg input tokens |",
             "|---|---|---|---|---|"]
    for m in MODES:
        mine = [r for r in rows if r["mode"] == m]
        if mine:
            lines.append(f"| {LABELS[m]} | {pct(mine, 'correct')} | {pct(mine, 'contaminated')} | "
                         f"{pct(mine, 'retrieval_hit')} | {sum(r['tokens_in'] for r in mine) // len(mine):,} |")
    lines += ["", "Correct answers by question type:", "",
              "| Setup | " + " | ".join(k.capitalize() for k in kinds) + " |", "|---|" + "---|" * len(kinds)]
    for m in MODES:
        mine = [r for r in rows if r["mode"] == m]
        if mine:
            lines.append(f"| {LABELS[m]} | " + " | ".join(
                pct([r for r in mine if r["kind"] == k], "correct") for k in kinds) + " |")
    misses = [r for r in rows if r["mode"] == "scoped" and (not r["correct"] or r["contaminated"])]
    if misses:
        lines += ["", "Misses by Stratum:", ""]
        lines += [f"- `{r['id']}`: {r['reason']}" for r in misses]
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--modes", default=",".join(MODES), help="comma-separated subset of: " + ", ".join(MODES))
    ap.add_argument("--only", default="", help="run a single question id")
    args = ap.parse_args()
    questions = yaml.safe_load((HERE / "questions.yaml").read_text())["questions"]
    if args.limit:
        questions = questions[:args.limit]
    if args.only:
        questions = [q for q in questions if q["id"] == args.only]
    modes = [m for m in MODES if m in args.modes.split(",")]
    project = proj.load()
    sem = asyncio.Semaphore(args.concurrency)
    rows = await asyncio.gather(*(run_one(q, m, project, sem) for q in questions for m in modes))
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, indent=2))
    table = report(rows)
    (out / "results.md").write_text(table)
    print(table)
    print(f"total tokens: in {sum(r['tokens_in'] for r in rows):,}  out {sum(r['tokens_out'] for r in rows):,}")
    db.pool().close()


if __name__ == "__main__":
    asyncio.run(main())
