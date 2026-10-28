"""Citations in answers: `[ref: run_python-3]` and `[doc: file]` become numbered links to a Sources list.

The answer text itself keeps the plain markers (they are the audit trail the citation check and the reviewer
use, and what Save/Download write). Only the screen shows numbers; each source expands to the tool, its input
and its full stored output (runs/<id>/outputs/<ref>.txt), so the user can check every number.
"""
from __future__ import annotations

import json
import re

import streamlit as st

from agent import config
from agent.memory import run_dir
from agent.outputs import parse_output, python_table  # noqa: F401  (re-exported for callers)
from agent.sqlfmt import format_sql
from agent.ui.results_page import number_columns

CITATION = re.compile(r"\[(ref|doc):\s*([^\]]+)\]")
MAX_SHOWN = 6000


def number_sources(text: str, anchor: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace each citation with numbered links [1][2] to `#<anchor>-<n>`. Returns (text, [(kind, name)])."""
    sources: list[tuple[str, str]] = []

    def link(m: re.Match) -> str:
        out = []
        for name in (n.strip() for n in re.split(r"[,;]", m.group(2))):
            if not name:
                continue
            if (m.group(1), name) not in sources:
                sources.append((m.group(1), name))
            n = sources.index((m.group(1), name)) + 1
            out.append(f"[[{n}]](#{anchor}-{n})")
        return "".join(out)

    return CITATION.sub(link, text), sources


@st.cache_data(show_spinner=False, max_entries=64)
def _read_text(path: str, version: tuple) -> str:
    """Stored outputs, read once per version rather than on every rerun of the page."""
    with open(path, errors="replace") as f:
        return f.read()


def _text(path) -> str:
    st_ = path.stat()
    return _read_text(str(path), (st_.st_mtime_ns, st_.st_size))


def source_details(state: dict, kind: str, name: str) -> dict:
    """Title, input and output of one cited source, from the run's events and stored outputs."""
    if kind == "doc":
        path = config.SANDBOX_DIR / name
        text = _text(path) if path.is_file() else "(document not found in the sandbox)"
        return {"title": f"document {name}", "input": None, "output": text}
    for ev in state.get("events", []):
        obs = ev["payload"].get("obs") or {}
        if obs.get("ref") == name:
            stored = run_dir(state) / "outputs" / f"{name}.txt"
            output = _text(stored) if stored.is_file() else str(obs.get("result") or obs.get("error"))
            if ev["payload"].get("answer"):                          # ask_user: the user's own words
                output = f"Question: {ev['payload'].get('question', '')}\nAnswer: {ev['payload']['answer']}"
            return {"title": ev["payload"].get("tool") or name.rsplit("-", 1)[0],
                    "input": ev["payload"].get("input"), "output": output}
    return {"title": "not found in this conversation", "input": None, "output": ""}


def show_output(output: str, key: str = "") -> None:
    """Render a tool output: a grid with a caption for tables, text otherwise."""
    caption, table, rest = parse_output(output)
    if table is not None:
        st.caption(caption)
        st.dataframe(table.head(config.UI_MAX_GRID_ROWS), hide_index=True, use_container_width=True,
                     column_config=number_columns(table))
        if rest:
            st.caption(rest)
    else:
        st.text(output[:MAX_SHOWN] + ("\n… (truncated)" if len(output) > MAX_SHOWN else ""))


def render_answer(state: dict, text: str, key: str, md=lambda t: t) -> None:
    """The answer with numbered citations, then a Sources list whose items open to show the evidence."""
    anchor = f"src-{re.sub(r'[^A-Za-z0-9_-]', '', key)}"
    body, sources = number_sources(text, anchor)
    st.markdown(md(body))
    if not sources:
        return
    st.markdown("**Sources**")
    for n, (kind, name) in enumerate(sources, 1):
        d = source_details(state, kind, name)
        # scroll-margin keeps the source below Streamlit's sticky header when a [n] link jumps to it
        st.markdown(f'<a id="{anchor}-{n}" style="display:block;scroll-margin-top:5.5rem"></a>', unsafe_allow_html=True)
        with st.expander(f"[{n}] {name} · {d['title']}"):
            if d["input"]:
                code = d["input"].get("code") if isinstance(d["input"], dict) else None
                if code:
                    st.code(code, language="python")
                    rest = {k: v for k, v in d["input"].items() if k != "code" and v}
                    if rest:
                        st.code(json.dumps(rest, indent=2), language="json")
                elif isinstance(d["input"], dict) and "sql" in d["input"]:
                    st.code(format_sql(d["input"]["sql"]), language="sql")
                else:
                    st.code(json.dumps(d["input"], indent=2), language="json")
            show_output(d["output"])
