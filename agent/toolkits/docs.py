"""Toolkit: Search the user's context documents and this run's earlier outputs."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .. import config
from ..tools import ToolContext, ToolError, tool, read_doc, sandbox_path

TOOLKIT = {
    "name": "docs",
    "description": "Search the user's context documents and this run's earlier outputs.",
    "basic": True,     # enabled when a session starts
    "guidance": ("CONTEXT documents define terms and methods: follow them. Use search_docs for larger documents and cite numbers quoted from them as [doc: <file name>]."),
}


class RecallArgs(BaseModel):
    query: str = Field(description="Keywords to search for")
    k: int = Field(5, ge=1, le=20)


@tool("recall", "Keyword search over this run's earlier tool outputs and the text/CSV files in "
      "sandbox/inputs. Returns the best matching lines with their source.", RecallArgs)
def recall(args: RecallArgs, ctx: ToolContext):
    terms = [t.lower() for t in re.findall(r"\w+", args.query)]
    if not terms:
        raise ToolError("Empty query.")
    files = list((ctx.run_dir / "outputs").glob("*.txt"))
    inputs = config.inputs_dir()
    if inputs.exists():
        files += [p for p in inputs.rglob("*") if p.suffix.lower() in (".txt", ".md", ".csv", ".json")]
    hits = []
    for f in files:
        for i, line in enumerate(f.read_text(errors="replace").splitlines(), start=1):
            score = sum(line.lower().count(t) for t in terms)
            if score:
                hits.append((score, f"{f.name}:{i}: {line.strip()[:200]}"))
    hits.sort(key=lambda h: -h[0])
    return "\n".join(h[1] for h in hits[: args.k]) or "No matches."



class SearchDocsArgs(BaseModel):
    query: str = Field(description="Keywords, e.g. tier 1 ratio definition")
    k: int = Field(4, ge=1, le=10)


@tool("search_docs", "Keyword search in the user's context documents (definitions, methodology). Returns "
      "the best matching paragraphs with their source.", SearchDocsArgs)
def search_docs(args: SearchDocsArgs, ctx: ToolContext):
    terms = [t.lower() for t in re.findall(r"\w+", args.query) if len(t) > 1]
    if not ctx.context:
        return "No context documents were given for this run."
    hits = []
    for doc in ctx.context:
        path = sandbox_path(doc)
        if not path.is_file():
            continue
        text = read_doc(path)
        for n, para in enumerate(re.split(r"\n\s*\n", text), start=1):
            score = sum(para.lower().count(t) for t in terms)
            if score:
                hits.append((score, f"[{path.name} §{n}]\n{para.strip()[:800]}"))
    hits.sort(key=lambda h: -h[0])
    return "\n\n".join(h[1] for h in hits[: args.k]) or "No matches."
