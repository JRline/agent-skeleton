#!/bin/bash
# Double-click in Finder to start the agent UI on this Mac only: http://localhost:8501 (fake backend).
# Bound to 127.0.0.1, so it is NOT reachable from other devices or the internet.
# Close this Terminal window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")" || exit 1
PY=python3
for v in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$v" >/dev/null 2>&1; then PY="$v"; break; fi
done
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10+ is required (found $("$PY" --version 2>&1)). Install it with: brew install python@3.12"
  read -r -p "Press Return to close." _; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $("$PY" --version)…"
  "$PY" -m venv .venv || { read -r -p "venv failed. Press Return to close." _; exit 1; }
fi
source .venv/bin/activate
python -c "import streamlit, pydantic, pandas, openpyxl" 2>/dev/null || pip install -r requirements.txt || { read -r -p "pip install failed. Press Return to close." _; exit 1; }
echo; echo "Running tests…"; python -m unittest discover -s tests -t . 2>&1 | tail -4; echo
export LLM_BACKEND="${LLM_BACKEND:-fake}"
echo "Starting Streamlit on http://localhost:8501 (backend=$LLM_BACKEND, local only)."
exec streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
