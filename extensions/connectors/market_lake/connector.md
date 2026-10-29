---
name: market_lake
kind: sqlite
description: Synthetic DemoBank market risk system (VaR, stressed VaR, expected shortfall by trading desk), a separate server from sample_lake. Illustrative only.
needs_login: no
data: data
connection_env: MARKET_ODBC
---
All data is synthetic. A separate data source from sample_lake: its own server and connection string (in production
an `odbc` connector whose connection string is in MARKET_ODBC). Its datasets cannot be joined to sample_lake in one
query: query each, then combine the saved extracts with run_python. Desks are keyed by `desk_code`, not by product:
the user's mapping table (source_system = market) links each desk_code to a product_id and location.
