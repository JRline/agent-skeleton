# Agent skeleton

A small, auditable Python agent for a text-only model (production: the GPT CLI, effort `none`/`medium`).

The model only produces text. The agent runs on **LangGraph / LangChain** (one `create_agent` graph: the tool loop,
approvals, pauses, checkpoints); Python adds the domain checks, the tools and a chat model that gives a text-only API
tool calling. The backend is swappable with one setting, so the same code runs in three places (and on any real
OpenAI-compatible API with `LLM_BACKEND=chatgpt`, or any LangChain chat model with `LLM_BACKEND=langchain`):

| Stage | Backend | Where | Purpose |
|---|---|---|---|
| 1 | `fake` | anywhere | Test the loop, tools, approvals and UI with no model |
| 2 | `ollama` | your Mac | A small local model behaves roughly like a small production-class model: messy JSON, weak plans |
| 3 | `prod` | closed environment | The real API module |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/setup_local.py                     # builds the synthetic sample data and inputs (no data is in git)
python -m unittest discover -s tests -t .
streamlit run app.py                              # http://localhost:8501, fake backend
```

## Documentation

| I want to... | Read |
|---|---|
| Install, run the three stages, change settings | [docs/setup.md](docs/setup.md) |
| Use the agent: sessions, tools, skills, data lake, evals | [docs/usage.md](docs/usage.md) |
| Add or update skills, tools, data sources and evals with the GPT CLI | [docs/maintenance.md](docs/maintenance.md) |
| Write a dataset file | [docs/dataset_entry.md](docs/dataset_entry.md) |
| Add a connector, dataset, skill, toolkit or planner example | [docs/extensions.md](docs/extensions.md) |
| Understand the design in full | [docs/design.md](docs/design.md) |
| Work on the code as a coding agent | [CLAUDE.md](CLAUDE.md) |

All documentation is listed in [docs/README.md](docs/README.md).

## Project layout

Details in `docs/design.md` §3. The map:

```
app.py, cli.py   entry points: UI and terminal
agent/           the framework (fixed): the LangGraph agent (graph.py), its step-by-step driver (loop.py),
                 the chat model for a text-only API, tools and sandbox, connectors (agent/connectors/: sqlite,
                 mock, odbc), the read-only SQL guard, sign-in (memory only), prompts, UI helpers (agent/ui/)
extensions/      added per environment, one folder per type (contract: docs/extensions.md):
  connectors/    one folder per data source: connector.py + datasets/<dataset>.md (one file per table)
  skills/        Markdown recipes that may use datasets of several connectors
  toolkits/      Python tools the agent can call
  examples/      worked plans for the planner
scripts/         eval.py, smoke_model.py, setup_local.py, gpt_pack.py, mock_lake_server.py
tests/           unit tests (FakeModel, no network) · tests/golden/ eval tasks
docs/            all documentation (index: docs/README.md) · gpt_prompts/ (used by scripts/gpt_pack.py)
sandbox/, runs/  runtime only, not in git: the user's files and work tables; one folder per conversation
```
