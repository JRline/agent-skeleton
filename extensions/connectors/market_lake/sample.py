"""Generate the synthetic dataset `desk_risk` (data/desk_risk.csv in this connector's folder). The desks and their
sizes come from the sample_lake generator, so the local mapping file links them to usage_limits."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _sample_lake():
    spec = importlib.util.spec_from_file_location("sample_lake_sample", HERE.parent / "sample_lake" / "sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path = HERE / "data" / "desk_risk.csv"):
    df = _sample_lake().generate_market()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


if __name__ == "__main__":
    print(f"desk_risk: {len(write()):,} rows")
