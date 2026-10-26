"""Analysis skills: reusable step lists written in Markdown (plugins/skills/*.md), no code needed.

A skill file has front matter (name, description, when_to_use, toolkits, references, source) and three sections.
`source` ties a skill to one data source (e.g. a data lake view): such a skill knows that source's fields and
rules and is only for questions about it; a skill without a source is general (e.g. any attached file).
`references` are sandbox documents the skill relies on (data dictionary, methodology): while the skill is
active they are the run's CONTEXT (small ones inline, larger ones via search_docs). Sections:
  ## Steps   numbered steps; they become the run's plan
  ## Checks  bullet points; added to the reviewer's checklist
  ## Output  what the final answer should look like
The planner may pick a skill, or the user can force one. The executor sees the whole skill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config


@dataclass
class Skill:
    name: str
    description: str = ""
    when_to_use: str = ""
    toolkits: list[str] = field(default_factory=list)   # enabled when the skill is used
    references: list[str] = field(default_factory=list) # sandbox docs (definitions, methods) put in context
    source: str = ""                                     # the data source it is written for; "" = general
    steps: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    output: str = ""


def parse_skill(text: str) -> Skill:
    meta, body = {}, text
    m = re.match(r"---\s*\n(.*?)\n---\s*\n(.*)", text, flags=re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        body = m.group(2)
    sections = {h.strip().lower(): c for h, c in re.findall(r"^##\s+(.+?)\s*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)}
    items = lambda s: [re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", "", line).strip()
                       for line in s.splitlines() if re.match(r"^\s*(?:\d+[.)]|[-*])\s+", line)]
    return Skill(name=meta.get("name", ""), description=meta.get("description", ""),
                 when_to_use=meta.get("when_to_use", ""),
                 toolkits=[t.strip() for t in meta.get("toolkits", "").split(",") if t.strip()],
                 references=[r.strip() for r in meta.get("references", "").split(",") if r.strip()],
                 source=meta.get("source", ""),
                 steps=items(sections.get("steps", "")),
                 checks=items(sections.get("checks", "")), output=sections.get("output", "").strip())


def list_skills() -> list[Skill]:
    skills = [parse_skill(p.read_text()) for p in sorted(config.SKILLS_DIR.glob("*.md"))]
    return [s for s in skills if s.name and s.steps]


def get_skill(name: str | None) -> Skill | None:
    return next((s for s in list_skills() if s.name == name), None) if name else None


def render_skill_list() -> str:
    return "\n".join(f"- {s.name}" + (f" [data source: {s.source}]" if s.source else " [general]")
                     + f": {s.description} Use when: {s.when_to_use}"
                     + (f" (needs toolkits: {', '.join(s.toolkits)})" if s.toolkits else "")
                     for s in list_skills()) or "(none)"


def render_skill(skill: Skill) -> str:
    """The active skill, appended to the executor's system prompt."""
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(skill.steps, 1))
    checks = "\n".join(f"- {c}" for c in skill.checks)
    return (f"## SKILL: {skill.name}\nFollow these steps (they are also the plan):\n{steps}\n"
            f"Checks the reviewer will apply:\n{checks}\nOutput: {skill.output}")
