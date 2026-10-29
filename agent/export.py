"""Export a whole conversation as one self-contained HTML file, readable anywhere with formatting kept.

Each instruction: the request (with attached files), its steps folded (what each request did, the SQL formatted,
the code, result tables), the answer with its tables, and the sources its [ref] links point to. Inline styles,
no scripts and no internet needed, so it opens offline and can be mailed or archived. The page is the Jinja2
template templates/conversation.html (values escaped automatically); this module prepares its data.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from . import config
from .memory import read_log, run_dir
from .outputs import parse_output
from .sqlfmt import format_sql

MAX_ROWS = 200                 # rows of a result table in the export (the CSV in work/ has them all)
_PAGE = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True,
                    trim_blocks=True).get_template("conversation.html")


def export_html(state: dict) -> Path:
    """Write sandbox/outputs/<run_id>/conversation_<run_id>.html and return its path."""
    out = config.outputs_dir(state["run_id"]) / f"conversation_{state['run_id']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(state), encoding="utf-8")
    return out


def build_html(state: dict) -> str:
    models = sorted({e.get("model") for e in read_log(state) if e.get("kind") == "model" and e.get("model")})
    segments = [{"goal": Markup(_md(request["payload"]["goal"])),
                 "files": [Path(f).name for f in request["payload"].get("files") or []],
                 "steps": [s for s in (_step(ev) for ev in steps) if s],
                 "answers": [_answer(state, ev, n) for ev in finals]}
                for n, (request, steps, finals) in enumerate(_segments(state), 1)]
    return _PAGE.render(title=state.get("title") or state["run_id"], run_id=state["run_id"],
                        exported=f"{datetime.now():%Y-%m-%d %H:%M}", models=", ".join(models) or "n/a",
                        segments=segments)


def _segments(state: dict):
    first = state["tasks"][0]["goal"] if state.get("tasks") else state["goal"]
    segs = [[{"kind": "task", "payload": {"goal": first, "files": state.get("first_files", [])}}]]
    for ev in state["events"]:
        if ev["kind"] == "task":
            segs.append([])
        segs[-1].append(ev)
    for seg in segs:
        yield seg[0], [e for e in seg[1:] if e["kind"] != "final"], [e for e in seg[1:] if e["kind"] == "final"]


def _code(inp: dict) -> str:
    return format_sql(inp["sql"]) if inp.get("sql") else inp.get("code") or ""


def _step(ev: dict) -> dict | None:
    """One step of the folded "How I got here" list, as data for the template."""
    kind, p = ev["kind"], ev["payload"]
    if kind == "plan":
        return {"kind": "plan", "plan": p.get("plan", [])}
    if kind == "approval":
        return {"kind": "approval", "tool": p.get("tool", ""), "explain": p.get("explain"),
                "code": _code(p.get("input") or {})}
    if kind in ("tool", "user") and p.get("tool"):
        obs = p.get("obs") or {}
        head = ("Approved" if p.get("approved") else "Rejected") if kind == "user" else "Ran"
        sql = (p.get("input") or {}).get("sql") if kind == "tool" else None
        return {"kind": "ran", "head": head, "tool": p["tool"], "code": format_sql(sql) if sql else "",
                "output": _output(str(obs.get("result") or obs.get("error") or "")) if obs else None}
    if kind == "user" and "answer" in p:
        return {"kind": "answered", "question": p.get("question", ""), "answer": p["answer"]}
    if kind in ("review", "precheck"):
        return {"kind": "issues", "issues": p.get("issues", []),
                "label": "Reviewer asked for changes" if kind == "review" else "Request sent back before approval"}
    if kind == "ask_user":
        return {"kind": "asked", "question": p.get("question", "")}
    return None


def _output(text: str) -> dict:
    caption, table, rest = parse_output(text)
    if table is not None:
        return {"caption": caption.replace("`", ""), "table": _table(table), "rest": rest}
    return {"text": text[:3000]}


def _answer(state: dict, ev: dict, n: int) -> dict:
    p = ev["payload"]
    refs: list[str] = []

    def cite(m):
        out = []
        for name in (x.strip() for x in re.split(r"[,;]", m.group(2)) if x.strip()):
            if name not in refs:
                refs.append(name)
            k = refs.index(name) + 1
            out.append(f"<sup><a href='#s{n}-{k}'>[{k}]</a></sup>")
        return "".join(out)

    links: list[str] = []                   # citations become placeholders, so markdown escaping leaves them alone

    def hold(m):
        links.append(cite(m))
        return f"CITE{len(links) - 1}END"

    body = _md(re.sub(r"\[(ref|doc):\s*([^\]]+)\]", hold, p["text"]))
    body = re.sub(r"CITE(\d+)END", lambda m: links[int(m.group(1))], body)
    tables = []
    for path in p.get("tables", []):
        full = config.SANDBOX_DIR / path
        if full.is_file():
            title = (p.get("table_titles") or {}).get(path) or Path(path).stem.replace("_", " ")
            tables.append({"title": title, "table": _table(pd.read_csv(full))})
    from .loop import answer_timing, describe_timing
    timing = answer_timing(state).get(ev.get("ts"))
    return {"when": describe_timing(timing) if timing else "", "body": Markup(body), "tables": tables,
            "sources": [{"id": f"s{n}-{k}", "name": name, **_source(state, name)} for k, name in enumerate(refs, 1)]}


def _source(state: dict, name: str) -> dict:
    for ev in state["events"]:
        obs = ev["payload"].get("obs") or {}
        if obs.get("ref") == name:
            stored = run_dir(state) / "outputs" / f"{name}.txt"
            text = stored.read_text(errors="replace") if stored.is_file() else str(obs.get("result", ""))
            return {"found": True, "code": _code(ev["payload"].get("input") or {}), "output": _output(text)}
    return {"found": False}


def _table(df: pd.DataFrame) -> dict:
    def cell(v):
        num = isinstance(v, (int, float)) and not isinstance(v, bool)
        text = "" if v is None or (isinstance(v, float) and pd.isna(v)) else (
            f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}" if num else str(v))
        return {"text": text, "num": num}
    return {"columns": [str(c) for c in df.columns],
            "rows": [[cell(v) for v in row] for row in df.head(MAX_ROWS).itertuples(index=False, name=None)],
            "more": f"first {MAX_ROWS:,} of {len(df):,} rows" if len(df) > MAX_ROWS else ""}


def _e(text: str) -> str:
    return html.escape(str(text), quote=True)


def _inline(text: str) -> str:
    """Escape, then bold, italics, inline code and links."""
    text = _e(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|#[\w-]+)\)", r"<a href='\2'>\1</a>", text)


def _md(text: str) -> str:
    """Markdown as written by the agent: headings, lists, tables, code blocks, paragraphs."""
    out, para, lines, i = [], [], text.splitlines(), 0

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append(f"<pre><code>{_e(chr(10).join(lines[i + 1:j]))}</code></pre>")
            i = j + 1
            continue
        if re.match(r"^\s*\|.*\|\s*$", line) and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            flush()
            cells = lambda s: [c.strip() for c in s.strip().strip("|").split("|")]  # noqa: E731
            head = "".join(f"<th>{_inline(c)}</th>" for c in cells(line))
            j, rows = i + 2, []
            while j < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[j]):
                rows.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(lines[j])) + "</tr>")
                j += 1
            out.append(f"<div class='table-wrap'><table><tr>{head}</tr>{''.join(rows)}</table></div>")
            i = j
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            level = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+[.)]\s+", line):
            flush()
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            tag, items = ("ol" if ordered else "ul"), []
            while i < len(lines) and (re.match(r"^\s*[-*]\s+", lines[i]) or re.match(r"^\s*\d+[.)]\s+", lines[i])):
                items.append(f"<li>{_inline(re.sub(r'^\s*(?:[-*]|\d+[.)])\s+', '', lines[i]))}</li>")
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return "".join(out)
