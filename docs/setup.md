# Setup and configuration

How to install and run the agent in its three stages, and every setting. For what the agent does once running, see `docs/usage.md`.

## Stage 1: FakeModel on your Mac

Use the same Python minor version as production (check with `python3 --version` there).

```bash
cd agent-skeleton
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/setup_local.py                     # builds the synthetic lake and sample inputs (no data is in git)

python -m unittest discover -s tests -t .        # unit tests, including the Streamlit UI tests
python cli.py "Compute the latest capital ratio from inputs/sample_capital.csv and save a summary"
streamlit run app.py                              # opens http://localhost:8501
```

Run `streamlit` from the project folder so it picks up `.streamlit/config.toml`, which keeps the UI
on this machine only (`server.address = "localhost"`) and turns off Streamlit's usage statistics.

The demo (`LLM_BACKEND=fake`, the default) reads `sandbox/inputs/sample_capital.csv`, calls the
`capital_ratio` tool, asks you to approve writing `summary.md`, then finishes after review.
In the UI, try Reject as well as Approve, the Stop button, and resuming a run from the sidebar.

Each run leaves an audit trail in `runs/<run_id>/`:
- `state.json`: full state, used for resume
- `log.jsonl`: every model call (purpose, effort, latency, output) and tool call
- `outputs/<ref>.txt`: full tool outputs referenced by the model

## Stage 2: local model with Ollama

```bash
brew install ollama            # or install the Ollama Mac app
ollama serve                   # skip if the app is running
ollama pull qwen3:4b           # any small instruct model works; try 2–3 sizes

export LLM_BACKEND=ollama
export OLLAMA_MODEL=qwen3:4b
# optional: a bigger model for effort "medium"
# export OLLAMA_MODEL_MEDIUM=qwen3:8b
# thinking is off for effort "none" and on for "medium" by default; OLLAMA_THINK=0 leaves the model's default
# export OLLAMA_THINK=never   # thinking off for every effort (qwen3.5 otherwise overthinks careful calls)

python smoke_model.py --backend ollama     # does the model follow the JSON protocol?
python cli.py "Compute the latest capital ratio from inputs/sample_capital.csv"
streamlit run app.py                        # pick "ollama" in the sidebar
```

In the UI sidebar, with Backend `ollama`:
- **Model** lists only installed models (`ollama list`). The chosen model answers every call and
  overrides `OLLAMA_MODEL` / `OLLAMA_MODEL_MEDIUM` for that app session. The effort policy still turns
  thinking off ("none") or on ("medium") for models that support it.
- **Stop** cancels the model call in progress (Ollama stops generating) and nothing runs after it.
  **New conversation** clears the screen; the conversation stays saved and can be reopened.

What to look at in this stage:
- How often replies need repair (`attempts` in the trace, `log.jsonl`).
- Whether the planner produces usable steps, and whether the reviewer catches unsupported numbers.
- Latency per effort level (the "Model calls" table in the UI).
- Tune prompts in `agent/prompts/` and limits in `agent/config.py`, then rerun the tests.

Use synthetic or public data only on the Mac.

### Stage 2b: Claude Haiku via the Claude Code CLI

A real, fast model with no API key, package or server: the `claude` backend runs the Claude Code CLI
that ships with the Claude desktop app (`claude -p`, all its own tools switched off, our system prompt,
an empty temporary folder). Prompts go to Anthropic as in any Claude Code session: **synthetic data only.**

```bash
python smoke_model.py --backend claude
python cli.py --backend claude "Show DemoBank's CET1 ratio trend"
streamlit run app.py                        # Backend "claude", Model Haiku 5.5 or Sonnet 5.5
```
`CLAUDE_MODEL` picks the model (default `claude-haiku-5-5`); `CLAUDE_CLI` overrides the binary path.

## Stage 3: production (closed environment)

1. Copy the project folder in through the approved route.
2. Install the pinned requirements from the internal package mirror.
3. Make the production API reachable as a module with `call(text, effort) -> str`.
   If the existing module has a different signature, copy `agent/backends/prod_template.py`
   to a new module (for example `gpt_wrapper.py`) and wrap it there.
4. Configure and check:
   ```bash
   export LLM_BACKEND=prod
   export PROD_API_MODULE=gpt_wrapper      # or the existing module's name
   python smoke_model.py --backend prod
   python -m unittest discover -s tests -t . # FakeModel tests still pass
   ```
5. Re-tune prompts and the effort policy against the production model. The local model will behave differently.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BACKEND` | `fake` | `fake`, `ollama`, `claude` or `prod` |
| `PROD_API_MODULE` | `existing_api_module` | module imported for `prod` |
| `CLAUDE_MODEL`, `CLAUDE_CLI` | `claude-haiku-5-5`, auto | see `agent/backends/claude_cli.py` |
| `DATALAKE` | `sqlite` | `sqlite` (in-process synthetic), `agent.backends.datalake_mock` (mock lake server) or a production lake module |
| `MOCK_LAKE_URL`, `MOCK_LAKE_TOKEN` | `http://127.0.0.1:8765`, none | client settings for the mock lake server |
| `DATALAKE_VIEWS` | (all) | optional comma-separated allowlist of views |
| `AGENT_PLUGINS` | `plugins/` | plugin folder (tools/*.py, skills/*.md) |
| `BASIC_TOOLKITS` | (marked basic) | comma-separated toolkits on when a session starts |
| `AGENT_MAX_STEPS` | `15` | executor steps per run |
| `SQL_TIMEOUT_S` | `900` | longest a lake query may run (15 minutes) |
| `TOOL_TIMEOUT_S` | `30` | time limit for every other tool call |
| `AGENT_HOME` | project folder | where `runs/` and `sandbox/` live |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_MODEL_MEDIUM`, `OLLAMA_THINK`, `OLLAMA_NUM_CTX` | | see `agent/backends/ollama_api.py` |

Other limits and the effort policy are constants in `agent/config.py`.
