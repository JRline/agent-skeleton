# Agent skeleton

A small, auditable Python agent for a text-only model (production: the GPT CLI, effort `none`/`medium`).

The model only produces text. Python runs the loop, the tools, memory, validation and logging.
The backend is swappable with one setting, so the same code runs in three places:

| Stage | Backend | Where | Purpose |
|---|---|---|---|
| 1 | `fake` | anywhere | Test the loop, tools, approvals and UI with no model |
| 2 | `ollama` | your Mac | A small local model behaves roughly like a small production-class model: messy JSON, weak plans |
| 3 | `prod` | closed environment | The real API module |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/setup_local.py                     # builds the synthetic lake and sample inputs (no data is in git)
python -m unittest discover -s tests -t .
streamlit run app.py                              # http://localhost:8501, fake backend
```

## Documentation

| I want to... | Read |
|---|---|
| Install, run the three stages, change settings | [docs/setup.md](docs/setup.md) |
| Use the agent: sessions, tools, skills, data lake, evals | [docs/usage.md](docs/usage.md) |
| Add or update skills, tools, data sources and evals with the GPT CLI | [docs/maintenance.md](docs/maintenance.md) |
| Write a data-source entry | [docs/datasource_entry.md](docs/datasource_entry.md) |
| Write a plugin (skill, toolkit, planner examples) | [docs/plugins.md](docs/plugins.md) |
| Understand the design in full | [docs/design.md](docs/design.md) |
| Work on the code as a coding agent | [CLAUDE.md](CLAUDE.md) |

All documentation is listed in [docs/README.md](docs/README.md).

## Project layout

Details in `docs/design.md` §3. The map:

```
app.py, cli.py, eval.py, smoke_model.py   entry points: UI, terminal, golden-task evals, backend check
results_page.py, sources.py               UI helpers: result tables, numbered sources under an answer
agent/        the framework (fixed): loop.py step machine · memory.py state, logs, prompt building ·
              protocol.py JSON actions and repair · tools.py tool registry and sandbox · plugins.py loader ·
              datalake.py read-only SQL guard · pysandbox.py isolated model-written code · config.py
agent/prompts/      planner, executor (system), request check, reviewer, finisher, summariser
agent/toolkits/     built-in tools: files, analysis (run_python), datalake (SQL), docs
agent/backends/     models (fake, ollama, claude, prod template) and data lakes (mock, ODBC template)
plugins/      per environment: skills/, tools/, examples/ (planner worked examples)
sandbox/      the only files tools may touch: inputs/, references/ (data dictionary), outputs/, work/
tests/        unit tests (FakeModel, no network) · tests/golden/ eval tasks
docs/         all documentation (index: docs/README.md) · gpt_prompts/ (used by scripts/gpt_pack.py)
mock_lake/    synthetic data lake for local runs
runs/         one folder per conversation: state.json, log.jsonl, outputs (git-ignored)
```
