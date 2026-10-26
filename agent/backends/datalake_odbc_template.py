"""Shape of the production data lake module: the production lake over ODBC (copy into the closed environment and fill in).

Set DATALAKE=<this module's name>. agent/datalake.py adds the SQL guard, row cap and allowlist
(DATALAKE_VIEWS) on top, so this module only has to connect and list views.

Use a read-only lake account and never put credentials in code or the repository: use a DSN with
integrated authentication, or whatever secret store the closed environment provides.
"""

NAME = "production lake (odbc)"


def connect():
    """Return a DB-API connection to the data lake. Must be read-only. Set the driver's query timeout to at
    least config.SQL_TIMEOUT_S (15 minutes by default), or long queries are cut off by the driver first."""
    # Example only. Check that pyodbc and the lake's ODBC driver exist in production first:
    # import os, pyodbc
    # return pyodbc.connect(f"DSN={os.environ['LAKE_DSN']}", readonly=True, autocommit=True)
    raise NotImplementedError("Wire this to the lake inside the closed environment")


def limit_sql(sql: str, n: int) -> str:
    """Optional: how the lake's SQL dialect asks for at most n rows. Check which form your lake version accepts
    (e.g. `... LIMIT n`); without this function the agent appends LIMIT n."""
    return f"{sql} LIMIT {n}"


def partitions(view: str) -> list[str]:
    """Optional: the partitions (values of config.LAKE_PARTITION_COLUMN) of a partitioned view, from catalogue metadata, never a table scan.
    Without this function describe_dataset lists no partitions."""
    raise NotImplementedError("List partitions from the lake catalogue")


def list_views() -> list[str]:
    """Views the agent may query. Prefer an explicit list agreed with the data owner."""
    # Example only:
    # import os
    # return [v.strip() for v in os.environ["LAKE_VIEWS"].split(",") if v.strip()]
    raise NotImplementedError("List the allowed lake views")
