"""Parse each table file once per version, then reuse it (IO cost: a 50k-row Excel file takes ~1.3 s to parse,
its pickle ~0 s).

A version is (resolved path, modification time, size) plus the reader's arguments, so an edited or re-uploaded
file is parsed again automatically. Two levels: a small in-memory cache in this process, and pickles in
sandbox/work/.cache/ that the analysis subprocess loads instead of parsing. Only the agent writes these pickles;
model-written code cannot write files and may not call read_pickle, so it never chooses what is unpickled.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

import pandas as pd

from . import config

_MEMORY: OrderedDict[str, pd.DataFrame] = OrderedDict()
_MEMORY_ITEMS = 8


def cache_dir() -> Path:
    return config.SANDBOX_DIR / "work" / ".cache"


def clear_memory() -> None:
    _MEMORY.clear()


def kwargs_key(kwargs: dict | None) -> str:
    """The reader arguments as text; the analysis runner builds the same key for the same call."""
    return json.dumps(kwargs or {}, sort_keys=True, default=str)


def _key(path: Path, kwargs: dict | None) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{kwargs_key(kwargs)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def read(path: Path, **kwargs) -> pd.DataFrame:
    """Parse a CSV/Excel/JSON file with pandas (no cache)."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, **kwargs)
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, **kwargs)
    if suffix == ".json":
        return pd.read_json(path, **kwargs)
    raise ValueError(f"Not a table file: {path.name}")


def load(path: Path, **kwargs) -> pd.DataFrame:
    """The table at `path`, parsed at most once per file version and reader arguments."""
    key = _key(path, kwargs)
    if key in _MEMORY:
        _MEMORY.move_to_end(key)
        return _MEMORY[key]
    pkl = cache_dir() / f"{key}.pkl"
    if pkl.is_file():
        df = pd.read_pickle(pkl)
    else:
        df = read(path, **kwargs)
        _write(pkl, df)
    _remember(key, df)
    return df


def pickle_for(path: Path, kwargs: dict | None = None) -> Path | None:
    """Pickle of this file version for the analysis subprocess, created if needed. None if it cannot be read."""
    try:
        key = _key(path, kwargs)
        pkl = cache_dir() / f"{key}.pkl"
        if not pkl.is_file():
            _write(pkl, _MEMORY.get(key) if key in _MEMORY else read(path, **(kwargs or {})))
        return pkl
    except Exception:                 # unreadable or bad arguments: the code's own read will report it
        return None


def store(path: Path, df: pd.DataFrame) -> None:
    """A table the agent has just written (work tables): cache it so the next step need not parse it."""
    key = _key(path, None)
    _write(cache_dir() / f"{key}.pkl", df)
    _remember(key, df)


def _write(pkl: Path, df: pd.DataFrame) -> None:
    pkl.parent.mkdir(parents=True, exist_ok=True)
    tmp = pkl.with_suffix(".tmp")
    df.to_pickle(tmp)
    tmp.replace(pkl)                  # atomic: a half-written pickle is never read


def _remember(key: str, df: pd.DataFrame) -> None:
    _MEMORY[key] = df
    _MEMORY.move_to_end(key)
    while len(_MEMORY) > _MEMORY_ITEMS:
        _MEMORY.popitem(last=False)
