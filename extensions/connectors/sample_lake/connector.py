"""The synthetic sample lake: a folder of CSVs (built by sample.py) served as a read-only SQLite database."""

NAME = "sample_lake"
KIND = "sqlite"
DESCRIPTION = "Synthetic DemoBank data lake for local runs and tests (illustrative only)."
NEEDS_LOGIN = False
DATA = "data"                     # sqlite: the folder of CSVs, one per dataset
URL = "http://127.0.0.1:8765"     # where scripts/mock_lake_server.py serves DATA (KIND = "mock", replay scripts)

ABOUT = """
All data is synthetic. Fiscal years run from 1 April to 31 March: FY2025 = 2025-04-01 to 2026-03-31, FY2025Q1 =
April to June 2025, Q2 = July to September, Q3 = October to December, Q4 = January to March 2026.
"""
