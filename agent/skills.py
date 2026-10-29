"""Analysis skills: reusable step lists written in Markdown (extensions/skills/*.md), no code needed.

A skill file has front matter (name, description, when_to_use, toolkits, datasets, references) and three sections.
`datasets` lists the datasets the skill is written for, from any of the enabled connectors (e.g. a limits
view): such a skill knows their fields and rules and is only for questions about them; once the skill is
accepted their documentation is in the agent's prompt. A skill without datasets is general (e.g. any attached file).
`references` are other sandbox documents the skill relies on (methodology): while the skill is active they are the
run's CONTEXT (small ones inline, larger ones via search_docs). Sections:
  ## Steps   numbered steps; they become the run's plan
  ## Checks  bullet points; added to the reviewer's checklist
  ## Output  what the final answer should look like
The planner may pick a skill, or the user can force one. The executor sees the whole skill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config
from .connectors import parse_front_matter    # one front-matter format for skills, connectors and datasets


@dataclass
class Skill:
    name: str
    description: str = ""
    when_to_use: str = ""
    toolkits: list[str] = field(default_factory=list)   # enabled when the skill is used
    datasets: list[str] = field(default_factory=list)   # datasets it is written for (any connectors); [] = general
    references: list[str] = field(default_factory=list) # sandbox docs (definitions, methods) put in context
    steps: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    output: str = ""


def parse_skill(text: str) -> Skill:
    meta, body = parse_front_matter(text)
    sections = {h.strip().lower(): c for h, c in re.findall(r"^##\s+(.+?)\s*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)}
    items = lambda s: [re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", "", line).strip()
                       for line in s.splitlines() if re.match(r"^\s*(?:\d+[.)]|[-*])\s+", line)]
    return Skill(name=meta.get("name", ""), description=meta.get("description", ""),
                 when_to_use=meta.get("when_to_use", ""),
                 toolkits=[t.strip() for t in meta.get("toolkits", "").split(",") if t.strip()],
                 datasets=[d.strip() for d in meta.get("datasets", "").split(",") if d.strip()],
                 references=[r.strip() for r in meta.get("references", "").split(",") if r.strip()],
                 steps=items(sections.get("steps", "")),
                 checks=items(sections.get("checks", "")), output=sections.get("output", "").strip())


def list_skills() -> list[Skill]:
    skills = [parse_skill(p.read_text()) for p in sorted(config.SKILLS_DIR.glob("*.md")) if not p.name.startswith("_")]
    return [s for s in skills if s.name and s.steps]


def get_skill(name: str | None) -> Skill | None:
    return next((s for s in list_skills() if s.name == name), None) if name else None


def render_skill_list() -> str:
    return "\n".join(f"- {s.name}" + (f" [datasets: {', '.join(s.datasets)}]" if s.datasets else " [general]")
                     + f": {s.description} Use when: {s.when_to_use}"
                     + (f" (needs toolkits: {', '.join(s.toolkits)})" if s.toolkits else "")
                     for s in list_skills()) or "(none)"


def render_skill(skill: Skill) -> str:
    """The active skill, appended to the executor's system prompt."""
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(skill.steps, 1))
    checks = "\n".join(f"- {c}" for c in skill.checks)
    return (f"## SKILL: {skill.name}\nFollow these steps (they are also the plan):\n{steps}\n"
            f"Checks the reviewer will apply:\n{checks}\nOutput: {skill.output}")
