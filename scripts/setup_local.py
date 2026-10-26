"""Build the local synthetic data after a fresh clone: the mock data lake and the sample input files.

No data is committed to the repository; everything here is generated from code, the same every time.

  python scripts/setup_local.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from mock_lake import generate_view  # noqa: E402
from mock_lake.samples import write_samples  # noqa: E402


def main() -> int:
    if generate_view.OUT.exists():
        print(f"Mock lake already there: {generate_view.OUT}")
    else:
        df = generate_view.write()
        print(f"Mock lake written: {generate_view.OUT} ({len(df):,} rows)")
    for path in write_samples(config.inputs_dir()):
        print(f"Sample input written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
