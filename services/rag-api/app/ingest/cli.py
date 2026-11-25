"""Corpus pipeline: download -> parse/chunk -> embed -> load -> verify.

    python -m app.ingest.cli download   # fetch source PDFs listed in corpus/sources.yaml
    python -m app.ingest.cli load       # parse, embed and (re)load every chunk
    python -m app.ingest.cli verify     # counts + known-fact retrieval checks
    python -m app.ingest.cli matrix     # extract the requirements matrix from the full text
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date

import requests
from pgvector import Vector

from .. import bedrock, config, db
from .parse import build_chunks, load_manifest

UA = "Mozilla/5.0 (compatible; corpus-fetch/1.0)"
RAW = config.CORPUS_ROOT / "corpus" / "raw"


def download() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    provenance = {}
    for doc in load_manifest(config.CORPUS_ROOT)["documents"]:
        path = RAW / doc["file"]
        if not path.exists():
            r = requests.get(doc["url"], headers={"User-Agent": UA}, timeout=120)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise SystemExit(f"{doc['id']}: {doc['url']} did not return a PDF")
            path.write_bytes(r.content)
        provenance[doc["id"]] = {"url": doc["url"], "file": doc["file"],
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "bytes": path.stat().st_size}
        print(f"ok  {doc['id']}  {provenance[doc['id']]['sha256'][:12]}")
    out = config.CORPUS_ROOT / "corpus" / "provenance.json"
    existing = json.loads(out.read_text()) if out.exists() else {}
    if existing.get("documents") != provenance:
        out.write_text(json.dumps({"checked": date.today().isoformat(), "documents": provenance}, indent=2) + "\n")


def load() -> None:
    db.init_schema()
    chunks = build_chunks(config.CORPUS_ROOT)
    print(f"embedding {len(chunks)} chunks with {config.EMBED_MODEL} ...")
    vectors = bedrock.embed_many([c.embed_text for c in chunks])
    with db.conn() as c, c.transaction():
        c.execute("TRUNCATE chunks")
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (id, doc_id, seq, jurisdiction, jurisdiction_name, layer, doc_title, "
                "doc_date, url, section_path, page_start, page_end, text, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                [(k.id, k.doc_id, n, k.jurisdiction, k.jurisdiction_name, k.layer, k.doc_title,
                  k.doc_date, k.url, k.section_path, k.page_start, k.page_end, k.text, Vector(v))
                 for n, (k, v) in enumerate(zip(chunks, vectors))])
    print("loaded")


# (question, jurisdiction, text that must appear in a retrieved passage)
KNOWN_FACTS = [
    ("minimum static factor of safety for slope stability", "draper", "1.5"),
    ("how long is a geotechnical report valid", "draper", "two years"),
    ("definition of an active fault", "draper", "4,000 radiocarbon years"),
    ("which fault setback calculation method was adopted", "draper", "Lund and others (2020)"),
    ("which fault setback calculation method was adopted", "fruit_heights", "Batatian and Nelson (1999)"),
    ("minimum building setback from an active fault trace", "ogden_valley", "50 feet"),
    ("minimum fault setback for single-family dwellings by risk category", "draper", "U4: 1.5; Minimum Setback (feet)4: 15"),
    ("minimum fault setback table by occupancy class", "fruit_heights", "U: 3.0; Minimum setback: 50 feet"),
]


def verify() -> bool:
    from ..retrieval import search_scoped
    with db.conn() as c:
        for row in c.execute("SELECT jurisdiction, count(*) AS n FROM chunks GROUP BY 1 ORDER BY 1"):
            print(f"{row['jurisdiction']:>15}: {row['n']} chunks")
    ok = True
    for question, jurisdiction, needle in KNOWN_FACTS:
        hits = search_scoped(question, [question], [jurisdiction], [])[jurisdiction]
        found = any(needle in p.text for p in hits)
        leaked = any(p.jurisdiction != jurisdiction for p in hits)
        ok &= found and not leaked
        print(f"{'PASS' if found and not leaked else 'FAIL'}  [{jurisdiction}] {question!r} -> {needle!r}")
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "download":
        download()
    elif cmd == "load":
        load()
    elif cmd == "matrix":
        import yaml
        from .. import matrix
        matrix.PATH.write_text(yaml.safe_dump(matrix.extract(), sort_keys=False, width=100, allow_unicode=True))
        print(f"wrote {matrix.PATH}")
    elif cmd == "verify":
        sys.exit(0 if verify() else 1)
    else:
        sys.exit(__doc__)
