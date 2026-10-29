"""A second, separate synthetic source: market risk by trading desk, on its own server and connection string."""

NAME = "market_lake"
KIND = "sqlite"                   # locally; in production "odbc" with the connection string below
DESCRIPTION = ("Synthetic DemoBank market risk system (VaR, stressed VaR, expected shortfall by trading desk), "
               "a separate server from sample_lake. Illustrative only.")
NEEDS_LOGIN = False
DATA = "data"
URL = "http://127.0.0.1:8766"     # where scripts/mock_lake_server.py serves DATA (KIND = "mock", replay scripts)
CONNECTION_STRING = "DRIVER={Market ODBC Driver};SERVER=market.example.internal;PORT=5433;DATABASE=market_risk"

ABOUT = """
All data is synthetic. A separate data source from sample_lake: its own server and connection string. Its datasets
cannot be joined to sample_lake in one query: query each, then combine the saved extracts with run_python. Desks are
keyed by `desk_code`, not by product: the user's mapping table (source_system = market) links each desk_code to a
product_id and location.
"""
