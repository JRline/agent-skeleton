"""Connectors and their datasets, loaded from extensions/connectors/ (docs/extensions.md, docs/design.md §6).

A connector is one connection to a data source (a server, a database, the local synthetic lake): a folder with
`connector.md` (front matter: name, kind, description, needs_login, kind settings; body: notes every dataset shares)
and `datasets/<name>.md`, one file per dataset (a table or view: its meaning, filters and columns). The connection
code is a built-in kind in this package (`sqlite`, `mock`, `odbc`), or `connector.py` in the folder (`kind: custom`).

A kind module provides:
  connect(connector, login) -> DB-API connection (read-only)
  list_datasets(connector, login) -> names the server has
  partitions(connector, dataset, column, login) -> values        (optional: from catalogue metadata)
  limit_sql(sql, n) -> sql                                       (optional: the dialect's row limit)
  source(connector) -> str                                       (optional: identifies the server, for caching)

Settings: CONNECTORS (comma-separated names; default every folder not starting with "_") picks the enabled ones, and
CONNECTOR_<NAME>_<KEY> overrides a front matter value, e.g. CONNECTOR_SAMPLE_LAKE_KIND=mock.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

ERRORS: list[str] = []                 # connectors that could not be loaded (shown in the UI, never fatal)
OVERRIDES: dict[str, dict] = {}        # name -> settings set in code (tests)


def parse_front_matter(text: str) -> tuple[dict, str]:
    """`---` / `key: value` lines / `---`, then the body. Shared by skills, connectors and datasets; a line starting
    with # and a trailing " # comment" are ignored."""
    m = re.match(r"---\s*\n(.*?)\n---\s*\n?(.*)", text, flags=re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.split(" #", 1)[0].strip()
    return meta, m.group(2)


def _required(spec: str) -> list[list[str]]:
    """`a` or `b`, `c` (or GROUP BY) -> [["a", "b"], ["c", "GROUP BY"]]: every group must be filtered on one of its
    columns; a group marked (or GROUP BY) may instead be split by in GROUP BY."""
    groups = []
    for part in [p for p in spec.split(",") if p.strip()]:
        cols = re.findall(r"`([^`]+)`", part) or [part.strip(" .")]
        groups.append(cols + (["GROUP BY"] if re.search(r"group by", part, re.I) else []))
    return [g for g in groups if g and g[0]]


@dataclass
class Dataset:
    name: str
    connector: str
    description: str = ""
    partition_column: str = ""
    required: list[list[str]] = field(default_factory=list)
    defaults: str = ""
    body: str = ""                     # meaning, grain, filters, columns: what describe_dataset returns

    def doc(self, about: str = "") -> str:
        """The dataset's whole documentation, as the agent reads it."""
        head = [f"## {self.name} (connector {self.connector})", self.description]
        if self.required:
            head.append("Required filter: " + ", ".join(
                " or ".join(f"`{c}`" for c in g if c != "GROUP BY") + (" (or GROUP BY)" if "GROUP BY" in g else "")
                for g in self.required) + ".")
        if self.defaults:
            head.append(f"Default filters: {self.defaults}.")
        return "\n".join([*head, self.body.strip()] + ([f"Notes for every dataset of this connector: {about}"]
                                                        if about else []))


@dataclass
class Connector:
    name: str
    kind: str
    folder: Path
    description: str = ""
    needs_login: bool = False
    settings: dict = field(default_factory=dict)
    about: str = ""
    datasets: dict[str, Dataset] = field(default_factory=dict)

    def setting(self, key: str, default: str = "") -> str:
        env = os.getenv(f"CONNECTOR_{self.name.upper()}_{key.upper()}")
        return env if env is not None else str(self.settings.get(key, default))

    def path(self, key: str, default: str = "") -> Path:
        value = Path(self.setting(key, default)).expanduser()
        return value if value.is_absolute() else self.folder / value

    def module(self):
        """The connection code: a built-in kind, or connector.py in the connector's folder."""
        if self.kind == "custom":
            spec = importlib.util.spec_from_file_location(f"connector_{self.name}", self.folder / "connector.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        return importlib.import_module(f"agent.connectors.{self.kind}")


def _load(folder: Path) -> Connector:
    meta, about = parse_front_matter((folder / "connector.md").read_text())
    meta.update(OVERRIDES.get(meta.get("name", folder.name), {}))
    name = meta.get("name") or folder.name
    conn = Connector(name=name, kind="", folder=folder, description=meta.get("description", ""),
                     settings=meta, about=" ".join(about.split()))
    conn.kind = conn.setting("kind", "sqlite")
    conn.needs_login = conn.setting("needs_login", "no").lower() in ("yes", "true", "1")
    for path in sorted((folder / "datasets").glob("*.md")):
        if path.name.startswith("_"):
            continue
        dmeta, body = parse_front_matter(path.read_text())
        ds = Dataset(name=dmeta.get("name") or path.stem, connector=name, description=dmeta.get("description", ""),
                     partition_column=dmeta.get("partition_column", ""),
                     required=_required(dmeta.get("required_filters", "")),
                     defaults=dmeta.get("default_filters", "").rstrip("."), body=body)
        conn.datasets[ds.name] = ds
    return conn


def all_connectors() -> dict[str, Connector]:
    """Every connector folder (template folders, starting with "_", left out)."""
    ERRORS.clear()
    out = {}
    root = config.EXTENSIONS_DIR / "connectors"
    for folder in sorted(p for p in root.glob("*") if p.is_dir() and not p.name.startswith("_")) if root.exists() else []:
        try:
            conn = _load(folder)
        except Exception as e:                         # a broken connector is reported, not fatal
            ERRORS.append(f"connector {folder.name}: {type(e).__name__}: {e}")
            continue
        out[conn.name] = conn
    return out


def enabled() -> dict[str, Connector]:
    """The connectors this deployment uses (CONNECTORS, default all). A dataset name must be unique among them, so
    a query's datasets say which connector runs it."""
    conns = all_connectors()
    names = [n for n in os.getenv("CONNECTORS", "").split(",") if n.strip()] or list(conns)
    picked = {n: conns[n] for n in names if n in conns}
    seen: dict[str, str] = {}
    for c in picked.values():
        for ds in list(c.datasets):
            if ds in seen:
                ERRORS.append(f"dataset {ds} is in connectors {seen[ds]} and {c.name}; the second is ignored")
                del c.datasets[ds]
            else:
                seen[ds] = c.name
    return picked


def datasets() -> dict[str, Dataset]:
    """Every dataset of the enabled connectors, by name."""
    return {name: ds for c in enabled().values() for name, ds in c.datasets.items()}


def build_samples(force: bool = False) -> list[str]:
    """Run each connector's optional `sample.py` (its `write()` builds synthetic data for local runs) when a
    dataset's data is missing. Data is never committed: a fresh clone builds the same files from the seed."""
    built = []
    for conn in all_connectors().values():
        script = conn.folder / "sample.py"
        data = conn.path("data", "data")
        if not script.exists() or (not force and all((data / f"{d}.csv").exists() for d in conn.datasets)):
            continue
        spec = importlib.util.spec_from_file_location(f"sample_{conn.name}", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.write()
        built.append(conn.name)
    return built


def dataset_doc(name: str) -> str:
    ds = datasets().get(name)
    if ds is None:
        return ""
    return ds.doc(enabled()[ds.connector].about)
