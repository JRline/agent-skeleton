"""Build the local synthetic data after a fresh clone: each connector's synthetic data and the sample input files.

No data is committed to the repository; everything here is generated from code, the same every time.

  python scripts/setup_local.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config, connectors  # noqa: E402
from scripts.sample_inputs import write_samples  # noqa: E402


def main() -> int:
    built = connectors.build_samples()
    print(f"Synthetic data built for: {', '.join(built)}" if built else "Synthetic connector data already there.")
    for path in write_samples(config.inputs_dir()):
        print(f"Sample input written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
