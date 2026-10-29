"""Parse each table file once per version, then reuse it (IO cost: a 50k-row Excel file takes ~1.3 s to parse,
its Parquet copy a few ms).

A version is (resolved path, modification time, size) plus the reader's arguments, so an edited or re-uploaded
file is parsed again automatically. Two levels: a small in-memory cache in this process, and Parquet files in
sandbox/work/.cache/ that the analysis subprocess loads instead of parsing. Parquet is plain data (loading it runs
no code, unlike a pickle). A table Parquet cannot hold (a column mixing numbers and text, e.g. a "Total" row in a
number column) is kept in memory only, and the analysis subprocess parses its file itself.
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
_UNSTORABLE: set[str] = set()       # file versions Parquet cannot hold (mixed columns)


def cache_dir() -> Path:
    return config.SANDBOX_DIR / "work" / ".cache"


def clear_memory() -> None:
    _MEMORY.clear()
    _UNSTORABLE.clear()


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
    cached = cache_dir() / f"{key}.parquet"
    if cached.is_file():
        df = pd.read_parquet(cached)
    else:
        df = read(path, **kwargs)
        _write(cached, df)
    _remember(key, df)
    return df


def cached_file(path: Path, kwargs: dict | None = None) -> Path | None:
    """Parquet copy of this file version for the analysis subprocess, created if needed. None if the file cannot
    be read or Parquet cannot hold the table: the subprocess then parses the file itself."""
    try:
        key = _key(path, kwargs)
        cached = cache_dir() / f"{key}.parquet"
        if cached.is_file():
            return cached
        if key in _UNSTORABLE:
            return None
        df = _MEMORY.get(key) if key in _MEMORY else read(path, **(kwargs or {}))
        _remember(key, df)
        if _write(cached, df):
            return cached
        _UNSTORABLE.add(key)          # tried once per file version: not parsed again just to fail again
    except Exception:                 # unreadable or bad arguments: the code's own read will report it
        pass
    return None


def store(path: Path, df: pd.DataFrame) -> None:
    """A table the agent has just written (work tables): cache it so the next step need not parse it."""
    key = _key(path, None)
    _write(cache_dir() / f"{key}.parquet", df)
    _remember(key, df)


def _write(target: Path, df: pd.DataFrame) -> bool:
    """Whether the table could be stored as Parquet."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    try:
        df.to_parquet(tmp, index=False)
    except Exception:                 # e.g. a column mixing numbers and text: kept in memory only
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(target)               # atomic: a half-written file is never read
    return True


def _remember(key: str, df: pd.DataFrame) -> None:
    _MEMORY[key] = df
    _MEMORY.move_to_end(key)
    while len(_MEMORY) > _MEMORY_ITEMS:
        _MEMORY.popitem(last=False)
