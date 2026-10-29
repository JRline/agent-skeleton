"""Budgets, limits, effort policy and paths (docs/design.md §9, §11).

Everything is read at call time as `config.X`, so tests can point the paths
at a temporary folder by assigning to these attributes.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- paths
ROOT = Path(os.getenv("AGENT_HOME", Path(__file__).resolve().parent.parent))
RUNS_DIR = ROOT / "runs"          # one folder per run: state.json, log.jsonl, outputs/
SANDBOX_DIR = ROOT / "sandbox"    # the only folder tools may read from or write to
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SETTINGS_DIR = ROOT / "settings"     # user prompt settings (settings/prompts.json)

# ---------------------------------------------------------------- backend
# fake   : scripted demo model, no LLM needed
# ollama : local model on your Mac (agent/backends/ollama_api.py)
# prod   : the production API module, imported by name
# claude : Claude (Haiku by default) via the Claude Code CLI on this Mac (agent/backends/claude_cli.py)
# chatgpt: ChatGPT, or any model behind an OpenAI-compatible API (agent/backends/openai_api.py: API_URL, API_MODEL, ...)
# langchain : any LangChain chat model, used natively (LC_MODEL="provider:model"; needs its provider package)
BACKEND = os.getenv("LLM_BACKEND", "fake")
PROD_API_MODULE = os.getenv("PROD_API_MODULE", "existing_api_module")
LC_MODEL = os.getenv("LC_MODEL", "")
BACKENDS = ["fake", "ollama", "claude", "chatgpt", "langchain", "prod"]
MODEL_RETRIES = 3

# ---------------------------------------------------------------- data (connectors and datasets: extensions/connectors/)
# Data sources are remote and read-only: nothing is ever loaded or written to them, and their data is never inside
# sandbox/ (that is the user's local files). Which connectors are on: CONNECTORS (default all; agent/connectors).
MAX_SQL_ROWS = 5000             # rows a query may return; more is an error that asks for aggregation in SQL
LAKE_META_TTL_S = 600           # datasets, columns and partitions are cached this long (one round trip per session)
READ_FILE_STORED_ROWS = 200     # read_file stores this many rows under its ref; the file itself stays the source
UI_MAX_GRID_ROWS = 1000         # rows shown in a result grid (the download has them all)
PY_TIMEOUT_S = 20               # model-written analysis code: wall-clock and CPU limit per run
PY_MAX_ROWS = 100_000           # rows a code result may return
REVIEW_CITED_CHARS = 8000      # full tool outputs cited in a final answer, shown to the reviewer
CONTEXT_INLINE_TOKENS = 1500   # context docs up to this size go into the prompt; larger ones via search_docs

# ---------------------------------------------------------------- extensions (see docs/extensions.md)
# Everything added per environment: connectors/ (with their datasets), skills/, toolkits/, examples/
EXTENSIONS_DIR = Path(os.getenv("AGENT_EXTENSIONS", Path(__file__).resolve().parent.parent / "extensions"))
SKILLS_DIR = Path(os.getenv("AGENT_SKILLS", EXTENSIONS_DIR / "skills"))
TOOLKITS_DIR = Path(os.getenv("AGENT_TOOLKITS", EXTENSIONS_DIR / "toolkits"))
# Worked examples for prompts (examples/<prompt>.md); swapped per environment like skills
EXAMPLES_DIR = Path(os.getenv("AGENT_EXAMPLES", EXTENSIONS_DIR / "examples"))
# Toolkits enabled when a session starts; empty = those whose TOOLKIT says "basic": True
BASIC_TOOLKITS = [t for t in os.getenv("BASIC_TOOLKITS", "").split(",") if t]

# ---------------------------------------------------------------- limits
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "15"))   # executor steps per run
MAX_REPAIRS = 2                 # JSON repair attempts at effort "none" before escalating
MAX_REVIEW_ROUNDS = 2
MAX_ACTION_REJECTIONS = 2       # requests sent back before the user is asked anyway (with the reviewer's notes)
REVIEWED_TOOLS = ("sql_query", "run_python", "read_file")   # checked by the reviewer before approval           # reviewer rejections before finishing with warnings
MAX_CONSECUTIVE_ERRORS = 2      # unparseable replies in a row before aborting
MAX_REPEATS = 2                 # identical tool calls in a row before aborting
PROMPT_BUDGET_TOKENS = 6000     # rough budget per model call (chars / 4)
RECENT_TURNS = 6                # exchanges kept verbatim before summarising
MAX_FACTS = 30
OBS_PREVIEW_CHARS = 1500        # observation text sent back to the model
KEEP_FULL_RESULTS = 3           # tool results the model sees in full; older ones shrink to one line with their ref
SHORT_RESULT_CHARS = 300        # results this short are never shortened
TOOL_TIMEOUT_S = int(os.getenv("TOOL_TIMEOUT_S", "30"))     # default limit for one tool call
# A lake query on a large view can legitimately run for many minutes; only sql_query gets this longer limit,
# so a stuck file read or search still fails fast.
SQL_TIMEOUT_S = int(os.getenv("SQL_TIMEOUT_S", "900"))

# ---------------------------------------------------------------- session memory (docs/design.md §4a)
# "off": every call sends the whole prompt (stateless backends). "session": the backend keeps one memory per
# channel (a fixed instance with a fixed effort), so the agent sends the fixed parts once and then only what is new.
MODEL_MEMORY = os.getenv("MODEL_MEMORY", "off")
CHANNEL_OF = {"executor": "executor", "action_review": "check", "planner": "review", "reviewer": "review",
              "finisher": "review", "summariser": "review"}
CHANNEL_EFFORT = {"executor": "none", "check": "medium", "review": "medium"}
# Re-send the fixed parts after this much text since the last full send: the backend compresses old memory,
# which can blur exact rules (reply format, tool names, required filters).
MEMORY_REFRESH_TOKENS = int(os.getenv("MEMORY_REFRESH_TOKENS", "20000"))

# ---------------------------------------------------------------- effort policy
EFFORT = {
    "planner": "medium",
    "executor": "none",
    "executor_recover": "medium",   # after an error or a repeated action
    "repair": "none",
    "repair_escalate": "medium",
    "summariser": "none",
    "reviewer": "medium",
    "action_review": "medium",      # a data read or analysis request, checked before the user is asked
    "finisher": "medium",           # one direct request for the answer when the executor is stuck
}


def inputs_dir() -> Path:
    return SANDBOX_DIR / "inputs"


def outputs_dir(run_id: str) -> Path:
    return SANDBOX_DIR / "outputs" / run_id


def work_dir(run_id: str) -> Path:
    """Per-run scratch tables (query results, derived columns). Not user-facing, so no approval needed."""
    return SANDBOX_DIR / "work" / run_id


def context_dir() -> Path:
    return SANDBOX_DIR / "context"


def connectors_dir() -> Path:
    """Connectors, their dataset files and any local synthetic data. Outside SANDBOX_DIR: file tools never see it."""
    return EXTENSIONS_DIR / "connectors"
