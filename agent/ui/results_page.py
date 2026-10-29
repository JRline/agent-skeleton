"""Results view for one conversation, opened in a new browser tab (app.py with ?results=<run_id>).

Created only when the user asks for it. It re-reads the saved conversation on every load, so it shows
the tables of later follow-ups too: use Refresh, or reload the tab.
"""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from agent import config
from agent.memory import load_state


# Numeric columns that are labels, not amounts: a year shown as "2,025" or an ID with separators misleads.
_NOT_AMOUNT = re.compile(r"date|year|^fy|period|quarter|month|(^|_)id$|code|partition_key|rank", re.I)


def number_columns(df: pd.DataFrame) -> dict:
    """Column formats for a result grid: thousands separators on amounts, as in the reports users know."""
    return {col: st.column_config.NumberColumn(format="localized") for col in df.columns
            if pd.api.types.is_numeric_dtype(df[col]) and not pd.api.types.is_bool_dtype(df[col])
            and not _NOT_AMOUNT.search(str(col))}


def _version(path) -> tuple:
    st_ = path.stat()
    return st_.st_mtime_ns, st_.st_size


@st.cache_data(show_spinner=False, max_entries=32)
def _read(path: str, version: tuple) -> tuple[pd.DataFrame, bytes]:
    """Read once per file version: Streamlit reruns the page on every click."""
    data = open(path, "rb").read()
    import io
    return pd.read_csv(io.BytesIO(data)), data


@st.cache_data(show_spinner=False, max_entries=8)
def _head(path: str, version: tuple, rows: int) -> pd.DataFrame:
    return pd.read_csv(path, nrows=rows)


def show_table(path, title: str, key: str) -> None:
    """A result grid (first rows only for big tables) with the full CSV as download. A very large file (an
    export) is never loaded whole: the grid shows its first rows and the page gives its location on disk."""
    limit = config.UI_MAX_GRID_ROWS
    st.caption(title)
    if path.stat().st_size > config.UI_MAX_DOWNLOAD_MB * 1e6:
        df = _head(str(path), _version(path), limit)
        st.dataframe(df, hide_index=True, use_container_width=True, column_config=number_columns(df))
        st.caption(f"Showing the first {limit:,} rows. The file ({path.stat().st_size / 1e6:,.0f} MB) is too large "
                   f"to download here; it is saved at `{path.resolve()}`.")
        return
    df, data = _read(str(path), _version(path))
    st.dataframe(df.head(limit), hide_index=True, use_container_width=True, column_config=number_columns(df))
    if len(df) > limit:
        st.caption(f"Showing the first {limit:,} of {len(df):,} rows; the download has them all.")
    st.download_button("Download CSV", data, file_name=path.name, mime="text/csv", key=key)


def render(run_id: str) -> None:
    try:
        s = load_state(run_id)
    except FileNotFoundError:
        st.error(f"No saved conversation {run_id}.")
        return
    st.title(f"Results · {s.get('title') or run_id}")
    st.button("Refresh", key="results_refresh")
    if not s.get("results"):
        st.info("No result tables yet. Tables appear here when an answer includes them.")
        return
    answers = [t.get("final") for t in s.get("tasks", [])] + [s.get("final")]
    for task in sorted({r["task"] for r in s["results"]}):
        rows = [r for r in s["results"] if r["task"] == task]
        st.subheader(rows[0]["goal"])
        if task < len(answers) and answers[task]:
            with st.expander("Answer", expanded=False):
                st.markdown(answers[task])
        for i, r in enumerate(rows):
            path = config.SANDBOX_DIR / r["path"]
            if not path.is_file():
                st.caption(f"{r['title']}: file no longer available")
                continue
            show_table(path, r["title"], key=f"results_dl_{task}_{i}")
