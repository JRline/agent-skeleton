"""Export a whole conversation as one self-contained HTML file, readable anywhere with formatting kept.

Each instruction: the request (with attached files), its steps folded (what each request did, the SQL formatted,
the code, result tables), the answer with its tables, and the sources its [ref] links point to. Inline styles,
no scripts and no internet needed, so it opens offline and can be mailed or archived. Standard library only.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config
from .memory import read_log, run_dir
from .outputs import parse_output
from .sqlfmt import format_sql

MAX_ROWS = 200                 # rows of a result table in the export (the CSV in work/ has them all)

_CSS = """
:root { --bg:#fbfbfa; --fg:#1d2321; --muted:#5f6b68; --rule:#d9dfdd; --req:#fdecec; --req-edge:#d64545;
  --ans:#eaf6ec; --ans-edge:#2e8b47; --code:#f1f3f2; }
@media (prefers-color-scheme: dark) { :root { --bg:#141817; --fg:#e4ebe9; --muted:#9aaaa6; --rule:#2c3634;
  --req:#3a2020; --req-edge:#e06565; --ans:#1b2f22; --ans-edge:#56b971; --code:#1d2523; } }
body { background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
  margin:0; padding:24px max(16px, 4vw); }
main { max-width:900px; margin:0 auto; }
h1 { font-size:26px; margin:0 0 4px; } .meta { color:var(--muted); font-size:13px; margin-bottom:24px; }
.req, .ans { border-radius:8px; padding:12px 16px; margin:18px 0 8px; }
.req { background:var(--req); border-left:4px solid var(--req-edge); }
.ans { background:var(--ans); border:1px solid var(--ans-edge); }
.label { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
details { border:1px solid var(--rule); border-radius:8px; padding:8px 12px; margin:8px 0; }
summary { cursor:pointer; color:var(--muted); }
.step { border-top:1px solid var(--rule); padding:8px 0; } .step:first-of-type { border-top:none; }
pre { background:var(--code); padding:10px 12px; border-radius:6px; overflow-x:auto; font-size:13px; }
code { font-family:ui-monospace,Menlo,Consolas,monospace; }
.table-wrap { overflow-x:auto; margin:8px 0; }
table { border-collapse:collapse; font-size:13px; } th, td { border:1px solid var(--rule); padding:4px 8px; text-align:left; }
td.num { text-align:right; font-variant-numeric:tabular-nums; } .caption { color:var(--muted); font-size:12px; }
.files { color:var(--muted); font-size:13px; } sup a { text-decoration:none; }
"""


def export_html(state: dict) -> Path:
    """Write sandbox/outputs/<run_id>/conversation_<run_id>.html and return its path."""
    out = config.outputs_dir(state["run_id"]) / f"conversation_{state['run_id']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(state), encoding="utf-8")
    return out


def build_html(state: dict) -> str:
    models = sorted({e.get("model") for e in read_log(state) if e.get("kind") == "model" and e.get("model")})
    title = state.get("title") or state["run_id"]
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
             f"initial-scale=1'><title>{_e(title)}</title><style>{_CSS}</style></head><body><main>",
             f"<h1>{_e(title)}</h1><div class='meta'>Conversation {_e(state['run_id'])} · exported "
             f"{datetime.now():%Y-%m-%d %H:%M} · model(s) {_e(', '.join(models) or 'n/a')}</div>"]
    for n, (request, steps, finals) in enumerate(_segments(state), 1):
        parts.append(_request(request))
        if steps:
            body = "".join(_step(state, ev) for ev in steps)
            parts.append(f"<details><summary>How I got here · {len(steps)} steps</summary>{body}</details>")
        for ev in finals:
            parts.append(_answer(state, ev, n))
    parts.append("</main></body></html>")
    return "".join(parts)


def _segments(state: dict):
    first = state["tasks"][0]["goal"] if state.get("tasks") else state["goal"]
    segs = [[{"kind": "task", "payload": {"goal": first, "files": state.get("first_files", [])}}]]
    for ev in state["events"]:
        if ev["kind"] == "task":
            segs.append([])
        segs[-1].append(ev)
    for seg in segs:
        yield seg[0], [e for e in seg[1:] if e["kind"] != "final"], [e for e in seg[1:] if e["kind"] == "final"]


def _request(ev: dict) -> str:
    p = ev["payload"]
    files = "".join(f"📎 {_e(Path(f).name)} " for f in p.get("files") or [])
    return (f"<div class='req'><div class='label'>Your request</div><div>{_md(p['goal'])}</div>"
            + (f"<div class='files'>{files}</div>" if files else "") + "</div>")


def _step(state: dict, ev: dict) -> str:
    kind, p = ev["kind"], ev["payload"]
    if kind == "plan":
        return "<div class='step'><b>Plan</b><ol>" + "".join(f"<li>{_e(s)}</li>" for s in p.get("plan", [])) + "</ol></div>"
    if kind == "approval":
        inp = p.get("input") or {}
        body = f"<div class='step'><b>Asked to approve</b> <code>{_e(p.get('tool', ''))}</code>"
        if p.get("explain"):
            body += f"<div>{_e(p['explain'])}</div>"
        if inp.get("sql"):
            body += f"<pre><code>{_e(format_sql(inp['sql']))}</code></pre>"
        elif inp.get("code"):
            body += f"<pre><code>{_e(inp['code'])}</code></pre>"
        return body + "</div>"
    if kind in ("tool", "user") and p.get("tool"):
        obs = p.get("obs") or {}
        head = ("Approved" if p.get("approved") else "Rejected") if kind == "user" else "Ran"
        body = f"<div class='step'><b>{head}</b> <code>{_e(p['tool'])}</code>"
        if kind == "tool" and (p.get("input") or {}).get("sql"):
            body += f"<pre><code>{_e(format_sql(p['input']['sql']))}</code></pre>"
        if obs:
            body += _output(str(obs.get("result") or obs.get("error") or ""))
        return body + "</div>"
    if kind == "user" and "answer" in p:
        return f"<div class='step'><b>Question</b> {_e(p.get('question', ''))}<br><b>Answer</b> {_e(p['answer'])}</div>"
    if kind in ("review", "precheck"):
        issues = "".join(f"<li>{_e(i)}</li>" for i in p.get("issues", []))
        label = "Reviewer asked for changes" if kind == "review" else "Request sent back before approval"
        return f"<div class='step'><b>{label}</b><ul>{issues}</ul></div>"
    if kind == "ask_user":
        return f"<div class='step'><b>Question to you</b> {_e(p.get('question', ''))}</div>"
    return ""


def _output(text: str) -> str:
    caption, table, rest = parse_output(text)
    if table is not None:
        return (f"<div class='caption'>{_e(caption.replace('`', ''))}</div>{_table(table)}"
                + (f"<div class='caption'>{_e(rest)}</div>" if rest else ""))
    return f"<pre>{_e(text[:3000])}</pre>"


def _answer(state: dict, ev: dict, n: int) -> str:
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
    tables = ""
    for path in p.get("tables", []):
        full = config.SANDBOX_DIR / path
        if full.is_file():
            title = (p.get("table_titles") or {}).get(path) or Path(path).stem.replace("_", " ")
            tables += f"<div class='caption'><b>{_e(title)}</b></div>{_table(pd.read_csv(full))}"
    sources = ""
    if refs:
        items = "".join(f"<li id='s{n}-{k}'>{_source(state, name)}</li>" for k, name in enumerate(refs, 1))
        sources = f"<details><summary>Sources</summary><ol>{items}</ol></details>"
    from .loop import answer_timing, describe_timing
    timing = answer_timing(state).get(ev.get("ts"))
    when = f"<div class='caption'>{_e(describe_timing(timing))}</div>" if timing else ""
    return f"<div class='ans'><div class='label'>Answer</div>{when}{body}{tables}</div>{sources}"


def _source(state: dict, name: str) -> str:
    for ev in state["events"]:
        obs = ev["payload"].get("obs") or {}
        if obs.get("ref") == name:
            stored = run_dir(state) / "outputs" / f"{name}.txt"
            text = stored.read_text(errors="replace") if stored.is_file() else str(obs.get("result", ""))
            inp = ev["payload"].get("input") or {}
            code = (f"<pre><code>{_e(format_sql(inp['sql']))}</code></pre>" if inp.get("sql") else
                    f"<pre><code>{_e(inp['code'])}</code></pre>" if inp.get("code") else "")
            return f"<code>{_e(name)}</code>{code}{_output(text)}"
    return f"<code>{_e(name)}</code> (document or earlier source)"


def _table(df: pd.DataFrame) -> str:
    head = "".join(f"<th>{_e(str(c))}</th>" for c in df.columns)
    rows = []
    for row in df.head(MAX_ROWS).itertuples(index=False, name=None):
        cells = []
        for v in row:
            num = isinstance(v, (int, float)) and not isinstance(v, bool)
            text = "" if v is None or (isinstance(v, float) and pd.isna(v)) else (
                f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}" if num else str(v))
            cells.append(f"<td class='num'>{_e(text)}</td>" if num else f"<td>{_e(text)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    more = f"<div class='caption'>first {MAX_ROWS:,} of {len(df):,} rows</div>" if len(df) > MAX_ROWS else ""
    return f"<div class='table-wrap'><table><tr>{head}</tr>{''.join(rows)}</table></div>{more}"


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
