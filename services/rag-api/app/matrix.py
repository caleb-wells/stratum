"""The requirements matrix: a short list of values a report cannot get wrong.

Search is good at the long tail and can still miss a section. For the handful of
values that drive design, that is not acceptable, so they are extracted once per
local code from the full text (`python -m app.ingest.cli matrix`), each with the
exact sentence it came from, and stored in corpus/requirements-matrix.yaml. A cell
is kept only if its quote is found word for word in the source.

At answer time the cells for the cities in question are looked up exactly and sent
to the model alongside the searched passages. They are an index into the source,
not a replacement for it: every cell points at its document, section and page.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import yaml

from . import bedrock, config, project as proj
from .retrieval import Passage

PATH = config.CORPUS_ROOT / "corpus" / "requirements-matrix.yaml"

# id -> (name, what to extract)
PARAMETERS: dict[str, tuple[str, str]] = {
    "fault_setback_method": (
        "Fault setback method",
        "How the required building setback from an active fault is determined: the named formula or "
        "method and what it is compared with."),
    "fault_setback_minimum": (
        "Minimum fault setback",
        "The minimum fault setback distances, including any table of minimums by building class and "
        "any absolute floor."),
    "active_fault_definition": (
        "Active fault definition",
        "The age or time criterion that makes a fault 'active'."),
    "slope_fs_static": (
        "Static slope stability factor of safety",
        "The minimum factor of safety for static slope stability (gross and surficial)."),
    "slope_fs_seismic": (
        "Seismic slope stability factor of safety",
        "The minimum factor of safety for seismic (pseudostatic) slope stability, and what is "
        "required if it is not met."),
    "slope_fs_temporary": (
        "Temporary slope factor of safety",
        "The minimum factor of safety for temporary slopes and excavations during construction."),
    "seismic_coefficient": (
        "Seismic coefficient and ground motion",
        "How the horizontal seismic coefficient for pseudostatic analysis is chosen, and the design "
        "ground motion level it is based on."),
    "report_validity": (
        "Report validity period",
        "How long a geotechnical or geologic-hazards report stays valid, and what must be submitted "
        "after that."),
}

EXTRACT = """You are building a requirements matrix from the full text of one local code: {name}.

The text below is every passage of the loaded documents, in reading order. Each passage starts with a line like "### 17 | section path | p. 36".

For each parameter, find what THIS code requires.

Parameters:
{parameters}

Reply with one JSON object keyed by parameter id. Each value is an object:
- "specified": true if the text states a requirement for this parameter, false if it does not.
- "value": if specified, the requirement in one or two plain sentences, with numbers and named methods exactly as written. If not specified, one sentence saying what the text does and does not say on the point.
- "quote": if specified, ONE sentence or table row copied character for character from a single passage, which states the core of the requirement. No ellipses, no joining of separate sentences. Empty string if not specified.
- "passage": if specified, the number of the passage the quote was copied from. null if not specified.

Do not use outside knowledge. A requirement that is only in a different document does not count. Reply with the JSON object and nothing else.

{text}"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract() -> dict:
    """Build the matrix from the parsed corpus. Returns the YAML-ready structure."""
    from .ingest.parse import build_chunks

    chunks = build_chunks(config.CORPUS_ROOT)
    manifest = proj.manifest()
    files = {d["url"]: d["file"] for d in manifest["documents"]}
    listing = "\n".join(f'- "{pid}" ({name}): {ask}' for pid, (name, ask) in PARAMETERS.items())
    cells: dict[str, dict] = {}
    for jid, info in manifest["jurisdictions"].items():
        if info["layer"] != "local":
            continue
        mine = [c for c in chunks if c.jurisdiction == jid]
        text = "\n\n".join(f"### {n} | {c.section_path} | p. {c.page_start}\n{c.text}"
                           for n, c in enumerate(mine))
        resp = bedrock.claude().messages.create(
            model=config.ANSWER_MODELS["opus"], max_tokens=6000,
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": EXTRACT.format(
                name=info["name"], parameters=listing, text=text)}])
        raw = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        cells[jid] = {}
        for pid in PARAMETERS:
            cell = data.get(pid) or {}
            if not cell.get("specified"):
                cells[jid][pid] = {"specified": False, "value": _norm(str(cell.get("value", "")))}
                print(f"--    {jid}/{pid}: not specified")
                continue
            quote = _norm(str(cell.get("quote", "")))
            n = cell.get("passage")
            home = mine[n] if isinstance(n, int) and 0 <= n < len(mine) else None
            if not (home and quote and quote in _norm(home.text)):      # look in the rest of this code
                home = next((c for c in mine if quote and quote in _norm(c.text)), None)
            if home is None:        # fail closed: an unverifiable quote never reaches an answer
                print(f"DROP  {jid}/{pid}: quote not found in the source: {quote[:90]!r}")
                continue
            cells[jid][pid] = {
                "specified": True, "value": _norm(str(cell["value"])), "quote": quote,
                "document": home.doc_title, "file": files.get(home.url, ""), "url": home.url,
                "section": home.section_path, "pages": [home.page_start, home.page_end]}
            print(f"ok    {jid}/{pid}: p. {home.page_start}")
    return {"note": "Generated by `python -m app.ingest.cli matrix` from the full text of each local "
                    "code. Every quote was checked word for word against the source. Not reviewed "
                    "by a licensed engineer.",
            "cells": cells}


@lru_cache
def load() -> dict:
    return yaml.safe_load(PATH.read_text())["cells"] if PATH.is_file() else {}


def rows(jurisdictions: list[str], parameters: list[str] | None = None) -> dict[str, list[Passage]]:
    """Matrix cells for these codes, as passages. `parameters=None` means every cell."""
    wanted = list(PARAMETERS) if parameters is None else [p for p in parameters if p in PARAMETERS]
    manifest = proj.manifest()
    out: dict[str, list[Passage]] = {}
    for jid in jurisdictions:
        name = manifest["jurisdictions"][jid]["name"]
        first = next((d for d in manifest["documents"] if d["jurisdiction"] == jid), None)
        for pid in wanted:
            cell = load().get(jid, {}).get(pid)
            if not cell or (not cell["specified"] and not first):
                continue
            label = PARAMETERS[pid][0]
            if cell["specified"]:
                text = (f"{label}: {cell['value']}\n\nSource sentence: \"{cell['quote']}\"\n"
                        f"({cell['document']}, {cell['section']}, p. {cell['pages'][0]})")
                p = Passage(id=f"matrix:{jid}:{pid}", jurisdiction=jid, jurisdiction_name=name,
                            layer="local", doc_title=cell["document"], doc_date=None, url=cell["url"],
                            section_path=cell["section"], page_start=cell["pages"][0],
                            page_end=cell["pages"][1], text=text)
            else:
                text = (f"{label}: not specified in the loaded {name} documents. {cell['value']} "
                        f"Do not supply a value from another city or from general practice.")
                p = Passage(id=f"matrix:{jid}:{pid}", jurisdiction=jid, jurisdiction_name=name,
                            layer="local", doc_title=first["title"], doc_date=None, url=first["url"],
                            section_path="Full text reviewed", page_start=1, page_end=1, text=text)
            p.matrix = label
            out.setdefault(jid, []).append(p)
    return out
