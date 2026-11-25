"""The project profile: which site falls under which jurisdiction."""
from __future__ import annotations

from functools import lru_cache

import yaml

from . import config


@lru_cache
def manifest() -> dict:
    return yaml.safe_load((config.CORPUS_ROOT / "corpus" / "sources.yaml").read_text())


@lru_cache
def load(project_id: str = "wasatch-front-portfolio") -> dict:
    path = config.CORPUS_ROOT / "corpus" / "projects" / f"{project_id}.yaml"
    return yaml.safe_load(path.read_text())


def jurisdiction_name(jid: str) -> str:
    return manifest()["jurisdictions"][jid]["name"]


def local_jurisdictions(project: dict) -> list[str]:
    seen: list[str] = []
    for s in project["sites"]:
        if s["jurisdiction"] not in seen:
            seen.append(s["jurisdiction"])
    return seen


def describe(project: dict) -> str:
    lines = [f"Project: {project['name']}", project["summary"].strip(), "", "Sites:"]
    for s in project["sites"]:
        lines.append(f"- {s['name']} - governed by {jurisdiction_name(s['jurisdiction'])} "
                     f"(id: {s['jurisdiction']}). {s['description'].strip()}")
    return "\n".join(lines)
