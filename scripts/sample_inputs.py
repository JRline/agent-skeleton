"""Synthetic sample input files for the demo, the tests and the evals. Kept as text here so that no data file is
committed; `scripts/setup_local.py` and the test setup write them into an inputs folder."""
from __future__ import annotations

from pathlib import Path

SAMPLE_INPUTS = {
    "sample_capital.csv": ("entity,period,cet1_capital,rwa\n"
                           "DemoBank,2026Q1,12100,96500\n"
                           "DemoBank,2026Q2,12400,98000\n"
                           "DemoBank,2026Q3,12850,101500\n"),
}


def _mapping_csv() -> str:
    """The user's mapping of credit portfolio and trading desk codes to product_id and location (joins credit_exposures
    and desk_risk to usage_limits); the codes come from the sample lake's generator."""
    import importlib.util
    script = Path(__file__).resolve().parent.parent / "extensions" / "connectors" / "sample_lake" / "sample.py"
    spec = importlib.util.spec_from_file_location("sample_lake_sample", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.mapping_csv()


def write_samples(inputs_dir: Path) -> list[Path]:
    """Write every sample input into inputs_dir; existing files are left alone."""
    inputs_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in {**SAMPLE_INPUTS, "book_mapping.csv": _mapping_csv()}.items():
        path = inputs_dir / name
        if not path.exists():
            path.write_text(text)
            written.append(path)
    return written
