# LangGraph prototype (`lgagent/`)

The same agent rebuilt on LangGraph and LangChain, to judge whether the framework can replace the custom step machine
(`agent/loop.py`). It sits next to the existing agent and changes nothing in it: same toolkits, plugins, skills,
prompts, sandbox, data lake guard and number check. Branch `langgraph-prototype` only.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-langgraph.txt
python scripts/setup_local.py                                           # synthetic lake and sample files
python -m lgagent.run --backend claude "What is DemoBank's latest total RWA usage?"
python -m lgagent.run --backend claude --resume RUN_ID                  # continue a paused run
python -m lgagent.eval --backend claude                                 # golden tasks, same checks as eval.py
python -m unittest tests.test_lgagent                                   # scripted model, no LLM
```

Set `LANGSMITH_TRACING=false` (the default) in production: LangSmith is a hosted tracing service and is installed as a
LangChain dependency.

## What is stock LangGraph / LangChain

| Need | Step machine (`agent/`) | Prototype |
|---|---|---|
| Model ↔ tool loop | `loop._step` | `langchain.agents.create_agent` (ToolNode, routing) |
| Plan → execute → review | statuses in `loop.py` | `StateGraph`: `plan` → `agent` → `check_answer` → back or end |
| Save and resume | `state.json`, `persist`, `load_state` | `SqliteSaver` checkpointer (`runs/langgraph.sqlite`), thread id = run id |
| Approve or reject with a reason | `awaiting_approval`, `resolve_pending` | `HumanInTheLoopMiddleware` (per call; `when` = the tool's `risky` rule) |
| Ask the user | `awaiting_user` | `interrupt()` inside the `ask_user` tool |
| Accept a suggested skill or toolkit | `awaiting_choice` | `interrupt()` in the plan node |
| Step limit | `MAX_STEPS` check | `ToolCallLimitMiddleware` |
| Transport retries | `ModuleChatModel` loop | `ModelRetryMiddleware` |
| Long conversations | `summarise_old_turns` | `SummarizationMiddleware` |
| Structured replies (plan, verdicts) | `request_json` + Pydantic | `with_structured_output(Model)` |
| Tools | registry + `dispatch` | `StructuredTool` per registry tool (same args model), still calling `dispatch` |
| Log | `memory.call_model` | a LangChain callback writing the same `log.jsonl` |

## What is still custom, and why

| Part | Lines | Why no stock part does it |
|---|---|---|
| `chat_model.py` `TextChatModel` | ~185 | The production API is text only (no native tool calls or structured output). It renders the tool list and a JSON reply format into the prompt, turns the reply into tool calls, and repairs bad replies. Everything above it is stock. |
| `graph.SessionPrompt` | ~25 | Per-session rules, skill, plan, files and enabled toolkits read from the graph state. |
| `graph.RequestCheck` | ~70 | Data reads and analysis code are checked (static check, then the request reviewer) before the user sees them. |
| `graph.check_answer` | ~40 | Every number must trace to a tool result (`loop.uncited_numbers`), then the reviewer. |
| `tools.py` | ~105 | Registry tools → LangChain tools; refs and the log. |
| `run.py`, `eval.py` | ~190 | Command line and golden-task scoring. |

About 800 lines in total, against about 750 for `loop.py` alone. The rest of `agent/` (tools, sandbox, lake guard,
`pysandbox`, skills, plugins, prompts) is reused unchanged.

## First results (Haiku via the Claude CLI, 2026-10-28)

Four golden tasks, one run: score 0.96. The step machine's last two full runs scored 1.00 / 1.00 / 1.00 / 0.17 on the same
four (runs vary; compare averages over full runs before deciding).

| task | score | tool calls | model calls | model s | missed |
|---|---|---|---|---|---|
| file_average | 1.00 | 2 | 7 | 43.9 | - |
| general_definition | 1.00 | 0 | 3 | 20.3 | - |
| rwa_latest_total | 1.00 | 4 | 9 | 50.6 | - |
| rwa_limit_status_now | 0.83 | 5 | 11 | 77.1 | the excess amount (1,088) |

Next: the full golden set (`python -m lgagent.eval --backend claude`, about 35 minutes) against the step machine.

## Not ported yet

- The Streamlit app (`app.py` still drives the step machine). The app would call `graph.invoke` / `get_state` and show
  `interrupts`; the approval, question and suggestion cards map one to one.
- Follow-up instructions in the same conversation (`continue_with`), titles, export and replay scripts.
- Session memory mode (it was built after this branch's base commit).
- Smaller step-machine behaviours: repeat detection and the finisher, effort escalation after an error, the
  "changed from the previous request" note, a suggestion when the model reaches for a disabled toolkit, context
  documents attached by the user, the STATE block (the plan is in the system prompt instead).
- Approval cards show the request reviewer's explanation (through the `description` callback), not the previous request.

## Dependencies

`requirements-langgraph.txt`: langgraph, langgraph-checkpoint(-sqlite), langchain, langchain-core and about 20 packages
they pull in (langsmith, httpx, orjson, ormsgpack, xxhash, tenacity, PyYAML, sqlite-vec, ...). All need approval in the
closed environment, at pinned versions; upgrades need the tests and the golden eval re-run.
