"""Toolkit: read-only SQL on the datasets of the enabled connectors (extensions/connectors/): list, describe, query.
Never local files. Each query runs on the connector that holds its datasets (agent/datalake.py)."""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .. import config
from ..tools import ToolContext, ToolError, tool, _save_work, _table_preview

_GUIDANCE = ("Datasets live on remote, read-only data sources (connectors): nothing can be loaded, uploaded or "
             "written to them. Attached files (INPUT FILES) are local and never part of them: read and analyse "
             "them with read_file and run_python, not with these tools. "
             "Use list_datasets, then describe_dataset once per dataset, before writing SQL, unless the dataset's "
             "documentation is already in your Skill references: then write the SQL from it. One query reads "
             "datasets of one connector; to combine datasets of different connectors, query each and join the "
             "saved extracts with run_python. "
             "SQL is for extraction and aggregation only: apply the required filters, select only the dates, "
             "scenario and resource you need, and GROUP BY to the level the analysis needs (e.g. date x "
             "division, never leaf rows). Datasets hold billions of rows on shared servers, so keep each extract "
             "small; a result over the row cap is refused, not truncated. Analysis (changes, averages over "
             "dates, ratios, utilisation, ranking) is done afterwards with run_python on the saved extracts. "
             "For a point-in-time question, first find the latest date of that resource and scenario "
             "(MAX(as_of_date)), together with the extract if possible. "
             "An approved query's result is in its observation and saved as a work table: cite its ref "
             "directly and pass its path to run_python; never re-read it only to cite it. The same query is "
             "not run twice in a conversation.")


def _dataset_rules() -> str:
    from ..datalake import dataset_rules
    try:
        return dataset_rules()
    except Exception:                          # no connectors: the general rules still apply
        return ""


TOOLKIT = {
    "name": "datalake",
    "description": ("Read-only SQL on the datasets of the connected data sources (data lake, databases). Needed for "
                    "any question about those datasets; not for attached files."),
    "basic": True,      # enabled when a session starts (read-only)
    "guidance": lambda: _GUIDANCE + " " + _dataset_rules(),
}


class NoArgs(BaseModel):
    pass


WIDE = 12           # datasets with more columns list a count here; describe_dataset gives them with their meaning


def _skill_datasets(ctx: ToolContext) -> set[str]:
    from ..memory import load_state, skill_datasets
    try:
        return set(skill_datasets(load_state(ctx.run_id)))
    except FileNotFoundError:
        return set()


@tool("list_datasets", "List the datasets you may query, by connector: what each one means and its columns. "
      "Start here.", NoArgs, preview_chars=2500)
def list_datasets(args: NoArgs, ctx: ToolContext):
    from .. import connectors
    from ..datalake import get_lake
    lines = []
    for name, conn in connectors.enabled().items():
        lake = get_lake(ctx.run_id, name)
        lines.append(f"Connector {name} ({conn.description})" + (f": {conn.about}" if conn.about else ""))
        for ds in lake.list_datasets():
            cols = lake.describe(ds)
            listed = f"{len(cols)} columns: use describe_dataset" if len(cols) > WIDE else f"columns: {', '.join(cols)}"
            req = conn.datasets[ds].required
            rule = ("\n  Required filter: " + ", ".join(" or ".join(c for c in g if c != "GROUP BY")
                                                      + (" (or GROUP BY)" if "GROUP BY" in g else "") for g in req)
                    if req else "")
            lines.append(f"- {ds}: {conn.datasets[ds].description}{rule}\n  {listed}")
    return "\n".join(lines) or "No datasets."


class DescribeArgs(BaseModel):
    dataset: str = Field(description="A dataset name from list_datasets")


@tool("describe_dataset", "What one dataset means (grain, required filters, time range, hierarchies, measures) and "
      "its columns, from its documentation and the catalogue (no data rows). Use before writing SQL; once per "
      "dataset in a conversation.", DescribeArgs, preview_chars=9000)
def describe_dataset(args: DescribeArgs, ctx: ToolContext):
    from .. import connectors
    from ..datalake import LakeError, get_lake
    ds = connectors.datasets().get(args.dataset)
    if ds is None:
        raise LakeError(f"Unknown dataset: {args.dataset}. Use one of: {', '.join(connectors.datasets())}.")
    lake = get_lake(ctx.run_id, ds.connector)
    cols = lake.describe(args.dataset)
    meaning = connectors.dataset_doc(args.dataset)
    if args.dataset in _skill_datasets(ctx):
        meaning = "(This dataset's documentation is already in your Skill references; not repeated here.)"
    try:
        parts = lake.partitions(args.dataset)
    except Exception:                       # a catalogue that cannot list partitions is not an error
        parts = []
    found = f"\npartitions ({ds.partition_column}): {', '.join(parts)}" if parts else ""
    return f"{meaning}\n\ncolumns: {', '.join(cols)}" + found


class SqlArgs(BaseModel):
    sql: str = Field(description="One read-only SELECT on the datasets of one connector. Select only needed columns; "
                                 "aggregate (SUM, GROUP BY) in SQL")
    save_as: str = Field(description="Short name for the result table, e.g. rwa_by_period")
    refresh: bool = Field(False, description="Run again even if this exact query already ran in this "
                                             "conversation (only when the data may have changed)")


def _queries_lake(args: dict, ctx: ToolContext) -> bool:
    """A query sent to the lake is IO the user confirms; a repeat answered from this conversation's saved
    result is not."""
    if args.get("refresh"):
        return True
    cache_file = ctx.run_dir / "sql_cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
    hit = cache.get(" ".join(str(args.get("sql", "")).split()).rstrip(";").strip())
    return not (hit and (config.SANDBOX_DIR / hit["path"]).is_file())


def _sql_problem(args) -> str | None:
    from ..datalake import static_problem
    return static_problem(args.sql)


@tool("sql_query", "Run one read-only SQL SELECT on datasets of one connector; filter and aggregate in SQL. The full result "
      f"(at most {config.MAX_SQL_ROWS} rows) is saved as a work table (path in the reply) for run_python, "
      "read_file or save_table. A repeated identical query reuses its saved table. Each query needs the user's "
      "approval (it reads a data source).", SqlArgs, risky=_queries_lake, check=_sql_problem,
      timeout_s=lambda: config.SQL_TIMEOUT_S)
def sql_query(args: SqlArgs, ctx: ToolContext):
    from ..datalake import lake_for
    from ..tools import _load_table
    key = " ".join(args.sql.split()).rstrip(";").strip()          # same query, whatever the whitespace
    cache_file = ctx.run_dir / "sql_cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
    hit = cache.get(key)
    if hit and not args.refresh and (config.SANDBOX_DIR / hit["path"]).is_file():
        df = _load_table(config.SANDBOX_DIR / hit["path"])
        return _table_preview(df, hit["path"], f" (reused: the same query already ran as {hit['ref']}; "
                                               "not sent to the lake again)")
    df, truncated = lake_for(args.sql, ctx.run_id).query(args.sql)
    if truncated:
        raise ToolError(f"The query returns more than {config.MAX_SQL_ROWS} rows, so nothing was saved. Aggregate "
                        "in SQL to the level the goal needs (GROUP BY the dimensions asked for, SUM/COUNT/AVG the "
                        "measures) and filter with WHERE (dates, entity, slice), then run it again.")
    path = _save_work(df, ctx, args.save_as)
    cache[key] = {"path": path, "ref": ctx.ref or "an earlier step", "rows": len(df)}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=1))
    return _table_preview(df, path)
