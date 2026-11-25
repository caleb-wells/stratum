"""The project profile: which site falls under which jurisdiction."""
from __future__ import annotations

import re
from functools import lru_cache

import yaml

from . import config


@lru_cache
def manifest() -> dict:
    return yaml.safe_load((config.CORPUS_ROOT / "corpus" / "sources.yaml").read_text())


_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@lru_cache
def load(project_id: str = "wasatch-front-portfolio") -> dict:
    """Load a project profile. The id is restricted so it can never name a path."""
    if not _PROJECT_ID.match(project_id):
        raise KeyError(project_id)
    root = (config.CORPUS_ROOT / "corpus" / "projects").resolve()
    path = (root / f"{project_id}.yaml").resolve()
    if path.parent != root or not path.is_file():
        raise KeyError(project_id)
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
