"""Budgets, limits, effort policy and paths (docs/design.md §9, §11).

Everything is read at call time as `config.X`, so tests can point the paths
at a temporary folder by assigning to these attributes. The values that may come from the environment are read
once, typed and checked, by `Settings` below.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOME = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Every setting that may come from an environment variable (the name in each alias). A bad value, e.g.
    SQL_TIMEOUT_S=abc or MODEL_MEMORY=yes, stops the agent at start with the variable named, instead of failing
    deep inside a run."""
    model_config = SettingsConfigDict(extra="ignore")

    agent_home: Path = Field(_HOME, alias="AGENT_HOME")
    backend: Literal["fake", "ollama", "claude", "chatgpt", "langchain", "prod"] = Field("fake", alias="LLM_BACKEND")
    prod_api_module: str = Field("existing_api_module", alias="PROD_API_MODULE")
    lc_model: str = Field("", alias="LC_MODEL")
    export_max_rows: int = Field(5_000_000, gt=0, alias="EXPORT_MAX_ROWS")
    extensions_dir: Path = Field(_HOME / "extensions", alias="AGENT_EXTENSIONS")
    skills_dir: Path | None = Field(None, alias="AGENT_SKILLS")
    toolkits_dir: Path | None = Field(None, alias="AGENT_TOOLKITS")
    examples_dir: Path | None = Field(None, alias="AGENT_EXAMPLES")
    basic_toolkits: str = Field("", alias="BASIC_TOOLKITS")          # comma-separated
    max_steps: int = Field(15, gt=0, alias="AGENT_MAX_STEPS")
    steps_per_plan_step: int = Field(3, ge=0, alias="AGENT_STEPS_PER_PLAN_STEP")
    tool_timeout_s: int = Field(30, gt=0, alias="TOOL_TIMEOUT_S")
    sql_timeout_s: int = Field(900, gt=0, alias="SQL_TIMEOUT_S")
    model_memory: Literal["off", "session"] = Field("off", alias="MODEL_MEMORY")
    memory_refresh_tokens: int = Field(20_000, gt=0, alias="MEMORY_REFRESH_TOKENS")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        bad = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
        raise RuntimeError(f"Invalid setting in the environment: {bad}") from None


_ENV = load_settings()

# ---------------------------------------------------------------- paths
ROOT = _ENV.agent_home
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
BACKEND = _ENV.backend
PROD_API_MODULE = _ENV.prod_api_module
LC_MODEL = _ENV.lc_model
BACKENDS = list(Settings.model_fields["backend"].annotation.__args__)
MODEL_RETRIES = 3

# ---------------------------------------------------------------- data (connectors and datasets: extensions/connectors/)
# Data sources are remote and read-only: nothing is ever loaded or written to them, and their data is never inside
# sandbox/ (that is the user's local files). Which connectors are on: CONNECTORS (default all; agent/connectors).
MAX_SQL_ROWS = 5000             # rows a query may return; more is an error that asks for aggregation in SQL
# export_query streams rows straight to a CSV in the run's outputs (never into memory or the prompt), so its cap only
# protects the shared server and the disk.
EXPORT_MAX_ROWS = _ENV.export_max_rows
UI_MAX_DOWNLOAD_MB = 200        # larger result files are not offered as an in-page download (the path is shown)
LAKE_META_TTL_S = 600           # datasets, columns and partitions are cached this long (one round trip per session)
READ_FILE_STORED_ROWS = 200     # read_file stores this many rows under its ref; the file itself stays the source
UI_MAX_GRID_ROWS = 1000         # rows shown in a result grid (the download has them all)
PY_TIMEOUT_S = 20               # model-written analysis code: wall-clock and CPU limit per run
PY_MAX_ROWS = 100_000           # rows a code result may return
REVIEW_CITED_CHARS = 8000      # full tool outputs cited in a final answer, shown to the reviewer
CONTEXT_INLINE_TOKENS = 1500   # context docs up to this size go into the prompt; larger ones via search_docs

# ---------------------------------------------------------------- extensions (see docs/extensions.md)
# Everything added per environment: connectors/ (with their datasets), skills/, toolkits/, examples/
EXTENSIONS_DIR = _ENV.extensions_dir
SKILLS_DIR = _ENV.skills_dir or EXTENSIONS_DIR / "skills"
TOOLKITS_DIR = _ENV.toolkits_dir or EXTENSIONS_DIR / "toolkits"
# Worked examples for prompts (examples/<prompt>.md); swapped per environment like skills
EXAMPLES_DIR = _ENV.examples_dir or EXTENSIONS_DIR / "examples"
# Toolkits enabled when a session starts; empty = those whose TOOLKIT says "basic": True
BASIC_TOOLKITS = [t.strip() for t in _ENV.basic_toolkits.split(",") if t.strip()]

# ---------------------------------------------------------------- limits
MAX_STEPS = _ENV.max_steps   # executor steps per instruction, at least (step_budget)
# A longer plan gets more steps: a join over three datasets needs a description, a query and a check per dataset,
# and failed under a flat 15. The planner keeps plans to 7 steps, so the budget is at most 21 by default.
STEPS_PER_PLAN_STEP = _ENV.steps_per_plan_step
MAX_REPAIRS = 2                 # JSON repair attempts at effort "none" before escalating
MAX_REVIEW_ROUNDS = 2
MAX_ACTION_REJECTIONS = 2       # requests sent back before the user is asked anyway (with the reviewer's notes)
REVIEWED_TOOLS = ("sql_query", "export_query", "run_python", "read_file")   # checked by the reviewer before approval           # reviewer rejections before finishing with warnings
MAX_CONSECUTIVE_ERRORS = 2      # unparseable replies in a row before aborting
MAX_REPEATS = 2                 # identical tool calls in a row before aborting
PROMPT_BUDGET_TOKENS = 6000     # rough budget per model call (chars / 4)
RECENT_TURNS = 6                # exchanges kept verbatim before summarising
MAX_FACTS = 30
OBS_PREVIEW_CHARS = 1500        # observation text sent back to the model
KEEP_FULL_RESULTS = 3           # tool results the model sees in full; older ones shrink to one line with their ref
SHORT_RESULT_CHARS = 300        # results this short are never shortened
TOOL_TIMEOUT_S = _ENV.tool_timeout_s     # default limit for one tool call
# A lake query on a large view can legitimately run for many minutes; only sql_query gets this longer limit,
# so a stuck file read or search still fails fast.
SQL_TIMEOUT_S = _ENV.sql_timeout_s

# ---------------------------------------------------------------- session memory (docs/design.md §4a)
# "off": every call sends the whole prompt (stateless backends). "session": the backend keeps one memory per
# channel (a fixed instance with a fixed effort), so the agent sends the fixed parts once and then only what is new.
MODEL_MEMORY = _ENV.model_memory
CHANNEL_OF = {"executor": "executor", "action_review": "check", "planner": "review", "reviewer": "review",
              "finisher": "review", "summariser": "review"}
CHANNEL_EFFORT = {"executor": "none", "check": "medium", "review": "medium"}
# Re-send the fixed parts after this much text since the last full send: the backend compresses old memory,
# which can blur exact rules (reply format, tool names, required filters).
MEMORY_REFRESH_TOKENS = _ENV.memory_refresh_tokens

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


MAX_STEPS_LIMIT = 100           # the highest step limit a user may set for a conversation


def step_budget(plan: list | None, max_steps: int | None = None) -> int:
    """Tool steps an instruction may take: the user's own limit for the conversation when set, else MAX_STEPS, or
    more for a longer plan."""
    if max_steps:
        return max(1, min(int(max_steps), MAX_STEPS_LIMIT))
    return max(MAX_STEPS, STEPS_PER_PLAN_STEP * len(plan or []))


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
