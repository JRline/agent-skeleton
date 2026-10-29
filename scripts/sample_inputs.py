"""Synthetic sample input files for the demo, the tests and the evals. Kept as text here so that no data file is
committed; `scripts/setup_local.py` and the test setup write them into an inputs folder. Inputs that belong to a
connector's sample data (e.g. a mapping of its codes) come from that connector's `sample.py` (`inputs()`)."""
from __future__ import annotations

from pathlib import Path

SAMPLE_INPUTS = {
    "sample_capital.csv": ("entity,period,cet1_capital,rwa\n"
                           "DemoBank,2026Q1,12100,96500\n"
                           "DemoBank,2026Q2,12400,98000\n"
                           "DemoBank,2026Q3,12850,101500\n"),
}


def write_samples(inputs_dir: Path) -> list[Path]:
    """Write every sample input into inputs_dir; existing files are left alone."""
    inputs_dir.mkdir(parents=True, exist_ok=True)
    written = []
    from agent import connectors
    for name, text in {**SAMPLE_INPUTS, **connectors.sample_inputs()}.items():
        path = inputs_dir / name
        if not path.exists():
            path.write_text(text)
            written.append(path)
    return written
