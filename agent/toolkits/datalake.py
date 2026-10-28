"""Toolkit: Read-only SQL on the remote data lake (a remote cluster): list, describe, query. Never local files."""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .. import config
from ..tools import ToolContext, ToolError, tool, _save_work, _table_preview

_GUIDANCE = ("The data lake is a remote, read-only lake server: nothing can be loaded, uploaded or written "
             "to it. Attached files (INPUT FILES) are local and never part of it: read and analyse them with "
             "read_file and run_python, not with these tools. "
             "Use list_datasets, then describe_dataset once per view, before writing SQL, unless the view's "
             "documentation is already in your Skill references: then write the SQL from it. "
             "SQL is for extraction and aggregation only: apply the required filters, select only the dates, "
             "scenario and resource you need, and GROUP BY to the level the analysis needs (e.g. date x "
             "division, never leaf rows). Views hold billions of rows on a shared server, so keep each extract "
             "small; a result over the row cap is refused, not truncated. Analysis (changes, averages over "
             "dates, ratios, utilisation, ranking) is done afterwards with run_python on the saved extracts. "
             "For a point-in-time question, first find the latest date of that resource and scenario "
             "(MAX(as_of_date)), together with the extract if possible. "
             "An approved query's result is in its observation and saved as a work table: cite its ref "
             "directly and pass its path to run_python; never re-read it only to cite it. The same query is "
             "not run twice in a conversation.")


def _view_rules() -> str:
    from ..datalake import view_rules
    try:
        return view_rules()
    except Exception:                          # no dictionary: the general rules still apply
        return ""


TOOLKIT = {
    "name": "datalake",
    "description": ("Read-only SQL on the remote data lake (a remote server): the views named in the data "
                    "dictionary. Needed for any question about lake data; not for attached files."),
    "basic": True,      # enabled when a session starts (read-only)
    "guidance": lambda: _GUIDANCE + " " + _view_rules(),
}


class NoArgs(BaseModel):
    pass


def _summary(note: str) -> str:
    """A view's meaning without its column bullets: the first line plus rules such as 'Total RWA = ...' and its
    required filters. Detail sections (### ...) are left to describe_dataset."""
    note = note.split("\n###", 1)[0]
    lines = [ln.strip() for ln in note.splitlines() if ln.strip() and not ln.strip().startswith("-")]
    return " ".join(lines)


WIDE = 12           # views with more columns list a count here; describe_dataset gives them with their meaning


@tool("list_datasets", "List the data lake views you may query: what each one means (from the data dictionary), "
      "required filters and columns. Start here.", NoArgs, preview_chars=2500)
def list_datasets(args: NoArgs, ctx: ToolContext):
    from ..datalake import get_lake, view_notes
    lake, notes = get_lake(ctx.run_id), view_notes()
    views = lake.list_views()
    def cols(v):
        names = lake.describe(v)
        return f"{len(names)} columns: use describe_dataset" if len(names) > WIDE else f"columns: {', '.join(names)}"
    parts = [f"- {v}: {_summary(notes.get(v, ''))}\n  {cols(v)}" for v in views]
    general = [f"{h}: {_summary(body)} " + " ".join(ln.strip() for ln in body.splitlines() if ln.strip().startswith("-"))
               for h, body in notes.items() if h not in views]
    return "\n".join(parts + general) or "No views."


class DescribeArgs(BaseModel):
    view: str = Field(description="A view name from list_datasets")


@tool("describe_dataset", "What one data lake view means (grain, required filters, time range, hierarchies, "
      "measures) and its columns, from the data dictionary and the catalogue (no data rows). Use before writing SQL; "
      "once per view in a conversation.", DescribeArgs, preview_chars=9000)
def describe_dataset(args: DescribeArgs, ctx: ToolContext):
    from ..datalake import get_lake, view_notes
    lake = get_lake(ctx.run_id)
    cols = lake.describe(args.view)
    meaning = view_notes().get(args.view, "")
    if meaning and str(config.datalake_dictionary().relative_to(config.SANDBOX_DIR)) in ctx.context:
        meaning = "(The view's documentation is already in your Skill references; not repeated here.)"
    try:
        parts = lake.partitions(args.view)
    except Exception:                       # a catalogue that cannot list partitions is not an error
        parts = []
    found = f"\npartitions ({config.LAKE_PARTITION_COLUMN}): {', '.join(parts)}" if parts else ""
    return (f"{meaning}\n\n" if meaning else "") + f"columns: {', '.join(cols)}" + found


class SqlArgs(BaseModel):
    sql: str = Field(description="One read-only SELECT on data lake views. Select only needed columns; "
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


@tool("sql_query", "Run one read-only SQL SELECT on the data lake; filter and aggregate in SQL. The full result "
      f"(at most {config.MAX_SQL_ROWS} rows) is saved as a work table (path in the reply) for run_python, "
      "read_file or save_table. A repeated identical query reuses its saved table. Each query needs the user's "
      "approval (it reads the lake).", SqlArgs, risky=_queries_lake, check=_sql_problem,
      timeout_s=lambda: config.SQL_TIMEOUT_S)
def sql_query(args: SqlArgs, ctx: ToolContext):
    from ..datalake import get_lake
    from ..tools import _load_table
    key = " ".join(args.sql.split()).rstrip(";").strip()          # same query, whatever the whitespace
    cache_file = ctx.run_dir / "sql_cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
    hit = cache.get(key)
    if hit and not args.refresh and (config.SANDBOX_DIR / hit["path"]).is_file():
        df = _load_table(config.SANDBOX_DIR / hit["path"])
        return _table_preview(df, hit["path"], f" (reused: the same query already ran as {hit['ref']}; "
                                               "not sent to the lake again)")
    df, truncated = get_lake(ctx.run_id).query(args.sql)
    if truncated:
        raise ToolError(f"The query returns more than {config.MAX_SQL_ROWS} rows, so nothing was saved. Aggregate "
                        "in SQL to the level the goal needs (GROUP BY the dimensions asked for, SUM/COUNT/AVG the "
                        "measures) and filter with WHERE (dates, entity, slice), then run it again.")
    path = _save_work(df, ctx, args.save_as)
    cache[key] = {"path": path, "ref": ctx.ref or "an earlier step", "rows": len(df)}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=1))
    return _table_preview(df, path)
