"""Streamlit UI (docs/design.md §12). Run:  streamlit run app.py

The UI holds no agent logic: each rerun advances the step machine by one step, renders the conversation,
and collects approvals, answers and choices. A conversation is one saved run: after an answer, a new
message continues it (`continue_with`), keeping earlier results.
"""
from __future__ import annotations

import difflib
import inspect
import json
import secrets
import os
import re
import threading
import time
from pathlib import Path

import streamlit as st

from agent import config, connectors, credentials, extensions, settings
from agent.adapter import get_model
from agent.backends import ollama_api
from agent.datalake import get_lake, needs_login
from agent.loop import (DONE, abort, answer_timing, continue_with, describe_timing, load_state, new_state,
                        rename, resolve_pending, run_one_step)
from agent.memory import list_conversations, read_log
from agent.extensions import basic_toolkits, parse_commands
from agent.skills import get_skill, list_skills
from agent.pysandbox import normalize_code
from agent.tools import step_summary
from agent.ui import results_page
from agent.ui.results_page import STRETCH
from agent.ui.sources import render_answer, scroll_code, show_output
from agent.sqlfmt import format_sql
from agent.export import build_html
from agent.replay import build_script, has_steps

st.set_page_config(page_title="Agent", layout="wide")
# The user's request and the answer stand out from the steps in between (marker spans select their chat bubble).
st.markdown("""<style>
[data-testid="stChatMessage"]:has(.req-mark) { border-left: 4px solid #ff4b4b; background: rgba(255, 75, 75, 0.07);
  padding: 0.75rem 1rem; border-radius: 0.5rem; }
[data-testid="stChatMessage"]:has(.req-mark) p { font-size: 1.05rem; font-weight: 500; }
[data-testid="stChatMessage"]:has(.ans-mark) { border: 1px solid rgba(46, 160, 67, 0.55); background: rgba(46, 160, 67, 0.06);
  padding: 0.75rem 1rem; border-radius: 0.5rem; }
.req-mark, .ans-mark { display: block; font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.75; }
</style>""", unsafe_allow_html=True)

# Partial reruns (docs/design.md §12): a click redraws only its own part of the page where it can. Each feature is
# detected, so an older Streamlit falls back to full reruns with the same behaviour.
fragment = getattr(st, "fragment", None) or (lambda func: func)          # st.fragment: Streamlit 1.37+
_LAZY = "on_change" in inspect.signature(st.expander).parameters        # an expander that knows it is open
NO_RERUN = ({"on_change": "ignore"} if '"ignore"' in (st.number_input.__doc__ or "")
            else {})                                                     # sent with the next action, not on typing


def lazy_expander(label: str, key: str):
    """An expander whose body is built only while it is open (closed ones cost nothing on a rerun). Returns the
    expander and whether to build its body."""
    if not _LAZY:
        return st.expander(label), True
    box = st.expander(label, key=key, on_change="rerun")
    return box, bool(box.open)


if st.query_params.get("results"):                  # the Results view, opened in a new tab on request
    results_page.render(st.query_params["results"])
    st.stop()


@st.cache_resource
def model_for(backend: str):
    """Created once per backend per server process, not on every rerun."""
    return get_model(backend)


@st.cache_data(ttl=30, show_spinner=False)
def installed_models() -> list[str]:
    return ollama_api.list_models()


def pick_ollama_model() -> None:
    """One model, chosen among installed models only, answers every call; effort still decides
    thinking on/off. ollama_api reads these env vars on every call, so setting them here
    switches models without restarting (this is a single-user local app)."""
    try:
        names = installed_models()
    except OSError:
        st.warning("Ollama is not running. Start it with `ollama serve`.")
        return
    if not names:
        st.warning("No models installed. Run `ollama pull <model>`.")
        return
    default = os.getenv("OLLAMA_MODEL", "")
    name = st.selectbox("Model", names, index=names.index(default) if default in names else 0, key="model")
    os.environ["OLLAMA_MODEL"] = os.environ["OLLAMA_MODEL_MEDIUM"] = name
    pick_ollama_thinking()


OLLAMA_THINKING = {"1": "By effort", "never": "Off", "0": "Model default"}   # OLLAMA_THINK value -> label


def pick_ollama_thinking() -> None:
    """A choice, not a fixed setting: qwen3 needs thinking on careful calls, while qwen3.5's thinking
    on those calls can run on without an answer. Set like the model picker (read on every call)."""
    default = os.getenv("OLLAMA_THINK", "1")
    think = st.segmented_control(
        "Thinking", list(OLLAMA_THINKING), format_func=OLLAMA_THINKING.get, required=True,
        default=default if default in OLLAMA_THINKING else "1", key="ollama_think",
        help="By effort: off for quick calls, on for careful ones (planner, reviewer, recovery). "
             "Off: never think, for models whose thinking does not finish. "
             "Model default: send no setting.")
    os.environ["OLLAMA_THINK"] = think


CLAUDE_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5"]


def pick_claude_model() -> None:
    """claude_cli reads CLAUDE_MODEL on every call, like the Ollama picker above."""
    default = os.getenv("CLAUDE_MODEL", CLAUDE_MODELS[0])
    os.environ["CLAUDE_MODEL"] = st.selectbox(
        "Model", CLAUDE_MODELS, index=CLAUDE_MODELS.index(default) if default in CLAUDE_MODELS else 0,
        key="claude_model", help="Runs through the Claude Code CLI on this Mac. Synthetic data only.")


def save_uploads(files) -> list[str]:
    """Attachments are input files of the conversation (sandbox/inputs/)."""
    paths = []
    for f in files or []:
        config.inputs_dir().mkdir(parents=True, exist_ok=True)
        (config.inputs_dir() / f.name).write_bytes(f.getvalue())
        paths.append(f"inputs/{f.name}")
    return paths


def exports(state: dict) -> dict:
    """The conversation as downloads, built once per state of the conversation (not on every rerun)."""
    key = (state["run_id"], len(state["events"]))
    if ss.get("exports_key") != key:
        try:
            script = build_script(state) if has_steps(state) else None
        except Exception as e:                       # a broken stored output must not break the page
            script = e
        ss.exports = {"html": build_html(state), "script": script}
        ss.exports_key = key
    return ss.exports


def open_conversation() -> None:
    """Button callback: load the conversation picked in the sidebar."""
    if ss.resume_id:
        ss.state = load_state(ss.resume_id)
        ss.max_steps = ss.state.get("max_steps") or 0          # the sidebar shows this conversation's step limit


def new_conversation() -> None:
    ss.state = None


def end_session() -> None:
    """Button callback: tools back to the basic set, skill back to Auto, screen cleared. Runs stay saved."""
    ss.session_toolkits = basic_toolkits()
    ss.skill = "Auto"
    ss.state = None
    model_for(ss.get("backend", config.BACKEND)).new_session()   # a backend with memory starts fresh instances
    sign_out()


def sign_out(connector: str | None = None) -> None:
    """Forget a connector's sign-in (all, if none named): this browser session's copy and every conversation it was
    lent to."""
    logins = ss.get("lake_logins", {})
    names = [connector] if connector else list(logins)
    for name in names:
        logins.pop(name, None)
        for key in ss.get("lake_runs", []):
            credentials.use(key, name, None)
    ss.lake_logins = logins


def lend_sign_ins(key: str) -> None:
    """This conversation's queries use this session's sign-ins (memory only)."""
    for name in needs_login():
        credentials.use(key, name, ss.get("lake_logins", {}).get(name))
    ss.lake_runs = list(dict.fromkeys(ss.get("lake_runs", []) + [key]))


@fragment
def connector_sign_in(name: str) -> None:
    """User ID and password for a connector that needs the user's own sign-in. Kept in this session's memory only
    (never saved, logged or shown to the model); the password field is cleared as soon as the form is sent."""
    login = ss.get("lake_logins", {}).get(name)
    with st.expander(f"Data sign-in: {name}", expanded=login is None):
        if login is not None:
            st.success(f"Signed in as {login.user}")
            if st.button("Sign out", key=f"sign_out_{name}"):
                sign_out(name)
                st.rerun()
            return
        with st.form(f"sign_in_{name}", clear_on_submit=True):
            user = st.text_input("User ID", key=f"user_{name}")
            password = st.text_input("Password", type="password", key=f"password_{name}")
            sent = st.form_submit_button("Sign in", type="primary")
        if sent:
            if not (user.strip() and password):
                st.error("Enter your user ID and password.")
                return
            candidate, key = credentials.Login(user.strip(), password), "check-" + secrets.token_hex(8)
            credentials.use(key, name, candidate)
            try:                                  # one uncached call: does the server accept this sign-in?
                lake = get_lake(key, name)
                lake.module.list_datasets(lake.connector, candidate)
            except Exception as e:
                st.error(f"Sign-in failed: {credentials.scrub(str(e))}")
                return
            finally:
                credentials.forget(key)
            ss.setdefault("lake_logins", {})[name] = candidate
            st.rerun()


@fragment
def conversation_picker() -> None:
    """Choosing a conversation redraws only this; opening it redraws the page."""
    convs = list_conversations()
    titles = {c["run_id"]: c["title"] for c in convs}
    st.selectbox("Conversations", [""] + [c["run_id"] for c in convs], key="resume_id",
                 format_func=lambda r: titles.get(r, r) if r else "Choose a conversation")
    if st.button("Open", key="resume", disabled=not ss.get("resume_id")):
        open_conversation()
        st.rerun()


@fragment
def datasets_list() -> None:
    """Asks the data sources only while open."""
    box, is_open = lazy_expander("Datasets", key="datasets_open")
    if not is_open:
        return
    with box:
        key = "view-" + ss.setdefault("lake_key", secrets.token_hex(8))
        lend_sign_ins(key)
        for name, conn in connectors.enabled().items():
            st.caption(f"{name} ({conn.kind})")
            try:
                st.markdown("\n".join(f"- `{d}`" for d in get_lake(key, name).list_datasets()) or "No datasets.")
            except Exception as e:
                st.caption(f"Not available: {credentials.scrub(str(e))}")


@fragment
def prompt_settings() -> None:
    """Role, answer style and templates: read only while open; typing sends nothing until Save."""
    box, is_open = lazy_expander("Prompt settings", key="prompt_settings_open")
    if not is_open:
        return
    with box:
        prefs = settings.load()
        role = st.text_area("Role", value=prefs["role"], key="pref_role", height=80,
                            placeholder="e.g. You are a regulatory capital analyst.", **NO_RERUN)
        style = st.text_area("Answer style", value=prefs["style"], key="pref_style", height=80,
                             placeholder="e.g. English, concise, amounts in JPY bn, tables for numbers.", **NO_RERUN)
        if st.button("Save role and style", key="pref_save"):
            settings.save_role_style(role, style)
            st.success("Saved. Used from the next model call.")
        if st.checkbox("Edit prompt templates (advanced)", key="pref_advanced"):
            name = st.selectbox("Template", settings.EDITABLE, key="pref_template")
            text = st.text_area("Template text", value=settings.template(name), key=f"pref_text_{name}",
                                height=320, help="Keep every {{placeholder}}; the agent fills them in.", **NO_RERUN)
            c1, c2 = st.columns(2)
            if c1.button("Save template", key="pref_template_save"):
                problem = settings.save_template(name, text)
                st.error(problem) if problem else st.success("Template saved.")
            if c2.button("Reset to default", key="pref_template_reset"):
                settings.reset_template(name)
                ss.pop(f"pref_text_{name}", None)
                st.rerun()


def stop_run() -> None:
    """Button callback: runs at once, even while a step is still waiting on a slow model.
    Abort first, so the step that is waiting sees the stop when its call is cancelled."""
    abort(ss.state, "Stopped by user.")
    model_for(ss.backend).cancel()


def add_to_session(toolkits) -> None:
    """Toolkits enabled by /commands or accepted suggestions stay on for the rest of the session.
    Applied at the top of the next rerun, before the Tools widget is drawn."""
    ss.pending_toolkits = list(dict.fromkeys(list(ss.get("session_toolkits", [])) + list(toolkits)))


def md(text: str) -> str:
    """Streamlit renders only $…$ and $$…$$; reasoning models often write \\(…\\) and \\[…\\]."""
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: f"$${m.group(1)}$$", text, flags=re.S)
    return re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1)}$", text, flags=re.S)


ATTACHABLE = ["csv", "xlsx", "xls", "json", "txt", "md"]


def show_tables(paths: list[str], key: str, titles: dict | None = None) -> None:
    """Result tables of an answer: interactive grid (sort, search, full screen) plus CSV download."""
    for i, path in enumerate(paths):
        full = config.SANDBOX_DIR / path
        if full.is_file():
            title = (titles or {}).get(path) or Path(path).stem.replace("_", " ")
            results_page.show_table(full, title, key=f"dl_{key}_{i}")


def approval_target(inp: dict) -> str:
    name = inp.get("name") or inp.get("path") or ""
    return f"→ `{name}`" if name else ""


@fragment
def render_model_calls(state: dict) -> None:
    """Read from the log only while open; opening it or showing raw replies redraws this section alone."""
    box, is_open = lazy_expander("Model calls (effort and latency)", key="model_calls_open")
    if not is_open:
        return
    with box:
        calls = [e for e in read_log(state) if e["kind"] == "model"]
        if calls:
            st.dataframe([{k: c.get(k) for k in ("step", "purpose", "effort", "backend", "model",
                                                  "prompt_tokens", "latency_ms", "error")}
                          for c in calls], **STRETCH)
            # Off by default: raw replies are long and only needed when comparing models.
            if st.checkbox("Show raw model replies", key="show_raw"):
                for i, c in enumerate(calls, 1):
                    st.caption(f"{i}. step {c.get('step')} · {c.get('purpose')} · effort {c.get('effort')}"
                               f" · {c.get('model') or c.get('backend')} · {c.get('latency_ms')} ms")
                    scroll_code(c.get("output") or c.get("error") or "(empty)", "json")
        else:
            st.caption("No model calls yet.")


ss = st.session_state
ss.setdefault("state", None)
ss.setdefault("autorun", True)
ss.setdefault("verbose", False)
ss.setdefault("max_steps", 0)                    # 0 = automatic step limit
ss.setdefault("session_toolkits", basic_toolkits())
if ss.get("pending_toolkits") is not None:      # set by /commands or an accepted suggestion last rerun
    ss.session_toolkits = [t for t in ss.pop("pending_toolkits") if t in extensions.TOOLKITS]
s = ss.state
if s and needs_login():                  # this conversation's queries use this session's sign-ins (memory only)
    lend_sign_ins(s["run_id"])

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.button("New conversation", key="new_conversation", on_click=new_conversation, type="primary")
    conversation_picker()
    if s and s["status"] not in DONE:
        st.button("Stop", key="stop", on_click=stop_run)
    if s and s.get("results"):
        if ss.get("results_page_for") == s["run_id"]:
            st.link_button("Open results page", f"?results={s['run_id']}")
        elif st.button("Create results page", key="create_results",
                       help="A separate page with every result table of this conversation (or type /results)."):
            ss.results_page_for = s["run_id"]
            st.rerun()

    st.divider()
    st.header("Settings")
    backends = config.BACKENDS
    backend = st.selectbox("Backend", backends, index=backends.index(config.BACKEND), key="backend")
    if backend == "ollama":
        pick_ollama_model()
    elif backend == "chatgpt":
        st.caption(f"ChatGPT (OpenAI-compatible API): {os.getenv('API_URL') or 'set API_URL'} · "
                   f"{os.getenv('API_MODEL') or 'set API_MODEL'}")
    elif backend == "langchain":
        st.caption(f"LangChain model: {config.LC_MODEL or 'set LC_MODEL=provider:model'}")
    elif backend == "claude":
        pick_claude_model()
    ss.autorun = st.toggle("Auto-run steps", value=ss.autorun, key="autorun_toggle")
    st.number_input("Step limit per instruction", min_value=0, max_value=config.MAX_STEPS_LIMIT, step=1,
                    key="max_steps", **NO_RERUN,
                    help=f"Tool steps the agent may take for one instruction; applies from your next message. "
                         f"0 = automatic: {config.MAX_STEPS}, or {config.STEPS_PER_PLAN_STEP} per plan step for a "
                         f"longer plan. If it stops at the limit, raise this and send a follow-up to carry on.")
    st.toggle("Verbose", key="verbose", help="On: thoughts, tool inputs and results. Off: only the steps.")
    kits = extensions.TOOLKITS
    st.multiselect("Tools (this session)", list(kits), key="session_toolkits",
                   format_func=lambda n: f"{n} — {kits[n]['description']}",
                   help="Kept for every message until End session. The agent may suggest more; you decide.")
    skill_choice = st.selectbox("Skill", ["Auto"] + [k.name for k in list_skills()], key="skill",
                                help="Auto: the agent may suggest a skill and you decide. "
                                     "Or type /skill_name in your message.")
    with st.expander("Commands"):
        st.caption("Type these in a message to enable a toolkit or tool, or to use a skill.")
        st.markdown("\n".join(f"- `/{i['name']}` · {i['kind']} · {i['description']}"
                              for i in extensions.catalogue()))
    if "datalake" in ss.session_toolkits:
        for name in needs_login():
            connector_sign_in(name)
        datasets_list()
    for err in extensions.ERRORS + connectors.ERRORS:
        st.warning(f"Extension not loaded: {err}")
    prompt_settings()
    st.button("End session", key="end_session", on_click=end_session,
              help="Reset tools to the basic set and the skill to Auto. Conversations stay saved.")

model = model_for(backend)

# ---------------------------------------------------------------- header
head, edit = st.columns([5, 1])
head.title(s.get("title") or "New conversation" if s else "New conversation")
if s:
    with edit.popover("Rename"):
        new_title = st.text_input("Conversation name", value=s.get("title") or "", key="rename_text")
        if st.button("Save", key="rename_save"):
            rename(s, new_title)
            st.rerun()
    st.caption(f"`{s['run_id']}` · status **{s['status']}** · "
               f"steps {s['step_count'] - s.get('task_start', 0)}/{config.step_budget(s.get('plan'), s.get('max_steps'))} · "
               f"backend {getattr(model, 'name', backend)} · tools: {', '.join(s.get('toolkits', []))}"
               + (f" · skill `{s['skill']}`" if s.get("skill") else ""))

# ---------------------------------------------------------------- conversation


def explain_request(p: dict) -> None:
    """What the request does, in plain words (from the reviewer's check), and anything it still flagged."""
    if p.get("explain"):
        text = p["explain"]
        lead = "Changed from the previous request:"
        if text.startswith(lead):                     # the change first, in bold, then what the request does
            change, _, rest = text[len(lead):].partition(". ")
            text = f"**{lead}{change}.** {rest}".rstrip()
        st.info(text)
    elif p.get("thought"):
        st.caption(p["thought"])
    for note in p.get("reviewer_notes") or []:
        st.warning(f"Reviewer: {note}")
    changes = request_diff(p)
    if changes:
        with section("What changed since the previous request (diff)"):
            st.code(changes, language="diff")


GROUPED = {"on": False}       # rendering inside a folded step list, where Streamlit allows no nested expanders


def section(label: str, expanded: bool = False):
    """An expander, or a captioned block when already inside one."""
    if GROUPED["on"]:
        st.caption(label)
        return st.container()
    return st.expander(label, expanded=expanded)


def request_diff(p: dict) -> str:
    """Line diff between this request and the previous one of the same tool (SQL compared formatted)."""
    inp = p.get("input") or {}
    now, before = str(inp.get("sql") or inp.get("code") or ""), str(p.get("previous") or "")
    if not before or not now or before.strip() == now.strip():
        return ""
    if inp.get("sql"):
        now, before = format_sql(now), format_sql(before)
    lines = difflib.unified_diff(before.splitlines(), now.splitlines(), "previous", "this request", lineterm="", n=1)
    return "\n".join(list(lines)[2:])


def render_event(ev: dict, verbose: bool) -> None:
    """Verbose off: one line per step and never any data read in. Verbose on: thoughts, inputs, results."""
    kind, p = ev["kind"], ev["payload"]
    with st.chat_message("user" if kind in ("user", "task") else "assistant"):
        if kind == "task":
            st.markdown(f'<span class="req-mark">Your request</span>\n\n{p["goal"]}', unsafe_allow_html=True)
            if p.get("files"):                       # what was attached with this instruction
                st.markdown(" ".join(f"📎 `{Path(f).name}`" for f in p["files"]))
        elif kind == "plan":
            title = f"**Plan** · skill `{p['skill']}`" if p.get("skill") else "**Plan**"
            st.markdown(title + "\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(p["plan"], 1)))
        elif kind == "suggest":
            st.markdown("💡 **Suggested:** " + ", ".join(
                [f"toolkit `{t}`" for t in p["toolkits"]] + ([f"skill `{p['skill']}`"] if p.get("skill") else [])))
            if p.get("why"):
                st.caption(p["why"])
        elif kind == "tool":
            ok = p["obs"].get("ok")
            st.markdown(f"{'🔧' if ok else '⚠️'} **{p['tool']}** · {step_summary(p['obs'])}"
                        + (f" · effort `{p.get('effort')}`" + (" · repaired" if p.get("attempts", 1) > 1 else "")
                           if verbose else ""))
            if verbose:
                if p.get("thought"):
                    st.caption(p["thought"])
                st.caption("Input")
                if p["input"].get("sql"):
                    scroll_code(format_sql(p["input"]["sql"]), "sql")
                else:
                    scroll_code(json.dumps(p["input"], indent=2), "json")
                st.caption("Output")
                show_output(str(p["obs"].get("result") or p["obs"].get("error")))
        elif kind == "precheck":
            st.markdown(f"🔍 Reviewer sent the `{p['tool']}` request back to the agent before asking you")
            if verbose:
                st.markdown("\n".join(f"- {i}" for i in p.get("issues", [])))
        elif kind == "approval" and p["tool"] == "run_python":
            st.markdown("**Approval requested:** run this analysis code")
            explain_request(p)
            st.code(normalize_code(p["input"].get("code", "")), language="python")   # what will run
            if p["input"].get("tables"):
                st.caption("Tables: " + ", ".join(f"{k} = {v}" for k, v in p["input"]["tables"].items()))
        elif kind == "approval" and p["tool"] == "sql_query":
            st.markdown("**Approval requested:** run this query on the data lake")
            explain_request(p)
            st.code(format_sql(p["input"].get("sql", "")), language="sql")
            st.caption(f"Read-only. The server returns at most {config.MAX_SQL_ROWS:,} rows; the result is saved as "
                       f"`{p['input'].get('save_as', '')}` and later steps reuse it without asking again.")
        elif kind == "approval" and p["tool"] == "export_query":
            st.markdown(f"**Approval requested:** export this query to `{p['input'].get('name', '')}`")
            explain_request(p)
            st.code(format_sql(p["input"].get("sql", "")), language="sql")
            st.caption(f"Read-only. Every row (up to {config.EXPORT_MAX_ROWS:,}) is written to a CSV file for you; "
                       "the agent sees only the row count and the first rows.")
        elif kind == "approval" and p["tool"] == "read_file":
            st.markdown(f"**Approval requested:** read your file `{p['input'].get('path', '')}`")
            explain_request(p)
        elif kind == "approval":
            st.markdown(f"**Approval requested:** `{p['tool']}` {approval_target(p['input'])}")
            if verbose:
                scroll_code(json.dumps(p["input"], indent=2), "json")
            else:
                with section("Details"):
                    scroll_code(json.dumps(p["input"], indent=2), "json")
        elif kind == "ask_user":
            st.markdown(f"**Question:** {p['question']}")
        elif kind == "user":
            if "answer" in p:
                st.markdown(f"Answered: {p['answer']}")
            elif "choice" in p:
                c = p["choice"]
                used = [f"toolkit `{t}`" for t in c["toolkits"]] + ([f"skill `{c['skill']}`"] if c["skill"] else [])
                st.markdown("Accepted " + ", ".join(used) if used else "Continued without the suggestion")
            elif p.get("approved") and p.get("obs"):
                st.markdown(f"Approved `{p.get('tool')}` · {step_summary(p['obs'])}")
                if verbose:
                    st.caption("Output")
                    show_output(str(p["obs"].get("result") or p["obs"].get("error")))
            else:
                st.markdown(f"{'Approved' if p.get('approved') else 'Rejected'} `{p.get('tool')}`"
                            + (f" · reason: {p['reason']}" if p.get("reason") else ""))
        elif kind == "review":
            st.markdown("🔍 Reviewer asked for changes")
            if verbose:
                st.markdown("\n".join(f"- {i}" for i in p.get("issues", [])))
        elif kind == "final":
            if p.get("via") == "finisher":
                st.caption("The model kept repeating itself, so the answer was requested directly (finisher).")
            key = f"final_{ev.get('ts')}_{ev.get('step')}"
            timing = answer_timing(s).get(ev.get("ts"))
            st.markdown('<span class="ans-mark">Answer</span>', unsafe_allow_html=True)
            if timing:
                st.caption(describe_timing(timing))
            render_answer(s, p["text"], key, md)        # [ref: …] → numbered links to a Sources list
            show_tables(p.get("tables", []), key=key, titles=p.get("table_titles"))
        elif kind == "abort":
            st.markdown("**Stopped**")
            st.caption(p.get("reason", ""))
        elif kind == "model_error":
            st.markdown("**Model call failed** · step not taken")
            st.caption(p["error"])
        else:
            st.markdown(f"**{kind}**")
            if verbose:
                st.json(p, expanded=False)


@fragment
def folded_steps(steps: list[dict], verbose: bool, key: str) -> None:
    """A finished instruction's steps, built only while its section is open: in a long conversation they are most
    of the page. Opening or closing it redraws this section alone."""
    n_tools = sum(e["kind"] in ("tool", "user") and bool(e["payload"].get("tool")) for e in steps)
    box, is_open = lazy_expander(f"How I got here · {len(steps)} steps · {n_tools} tool calls", key=key)
    if not is_open:
        return
    with box:
        GROUPED["on"] = True
        try:
            for ev in steps:
                render_event(ev, verbose)
        finally:
            GROUPED["on"] = False


def render_conversation(state: dict, verbose: bool) -> None:
    """Each instruction as: the request, its steps, the answer. A finished instruction folds its steps into one
    "How I got here" section, so requests and answers stand out; the current one shows its steps as they happen."""
    first = state["tasks"][0]["goal"] if state.get("tasks") else state["goal"]
    segments = [[{"kind": "task", "payload": {"goal": first, "files": state.get("first_files", [])}}]]
    for ev in list(state["events"]):                # a copy: a background step may be appending
        if ev["kind"] == "task":
            segments.append([])
        segments[-1].append(ev)
    for i, seg in enumerate(segments):
        request, steps = seg[0], [e for e in seg[1:] if e["kind"] != "final"]
        answers = [e for e in seg[1:] if e["kind"] == "final"]
        render_event(request, verbose)
        live = i == len(segments) - 1 and state["status"] not in DONE
        if steps and not live:
            folded_steps(steps, verbose, key=f"how_{state['run_id']}_{i}")
        else:
            for ev in steps:
                render_event(ev, verbose)
        for ev in answers:
            render_event(ev, verbose)


if s:
    render_conversation(s, ss.verbose)

# ---------------------------------------------------------------- drive the step machine
JOB_NOTES = {
    "sql_query": ("Querying the data lake", f"Large queries can take up to {config.SQL_TIMEOUT_S // 60} minutes. The "
                                            "result appears here when it finishes; you can leave this page open."),
    "export_query": ("Exporting from the data lake", f"Large exports can take up to {config.SQL_TIMEOUT_S // 60} "
                                                    "minutes. The file appears here when it finishes."),
    "run_python": ("Running the analysis", ""),
    "read_file": ("Reading your file", ""),
}


def start_job(label: str, fn, note: str = "") -> None:
    """Run a blocking step (a lake query can take minutes) in the background; the page shows its progress
    and offers no actions meanwhile, so the same query can never be started twice."""
    job = {"label": label, "note": note, "t0": time.monotonic(), "error": None}

    def work():
        try:
            fn()
        except Exception as e:                     # shown on the page; the run itself stays consistent
            job["error"] = f"{type(e).__name__}: {e}"

    job["thread"] = threading.Thread(target=work, daemon=True)
    ss.io_job = job
    job["thread"].start()
    st.rerun()


POLL = getattr(st, "fragment", None) is not None      # the progress box refreshes itself (no blocking loop)


def show_progress() -> None:
    """While a background step runs, show what it is and the elapsed time. With fragments the box redraws itself
    every second and nothing else on the page waits for it: opening a section during a 15-minute query works,
    and the page reruns once when the step ends. Without fragments it waits here, as before."""
    job = ss.get("io_job")
    if not job:
        return
    box = st.empty()
    while True:
        job["thread"].join(0.5)
        if not job["thread"].is_alive():
            break
        secs = int(time.monotonic() - job["t0"])
        box.info(f"⏳ **{job['label']}** · {secs // 60}:{secs % 60:02d} elapsed"
                 + (f"  \n{job['note']}" if job["note"] else ""))
        if POLL:
            return                                     # the fragment's timer draws the next update
    box.empty()
    ss.io_job = None
    if job["error"]:
        ss.job_error = f"{job['label']} failed: {job['error']}"
    st.rerun()


if POLL:
    show_progress = st.fragment(run_every=1)(show_progress)


def set_rejecting(pending_id: str | None) -> None:
    """Button callback: open (or close) the reject form for this request, redrawing only the action area."""
    ss.rejecting = pending_id


@fragment
def actions(s: dict, model, backend: str) -> None:
    """What the user can do now: approve or reject, answer, pick suggested tools, or step on. A fragment: opening
    the reject form, cancelling it or ticking a toolkit redraws only this box; anything that changes the
    conversation (approve, send, answer, a step) reruns the whole page once (st.rerun), as it must."""
    if ss.get("io_job"):
        return                                         # still running (show_progress)
    if ss.get("job_error"):
        st.error(ss.pop("job_error"))
    if s["status"] == "awaiting_approval":
        p = s["pending"]
        st.warning("Run this analysis code?" if p["tool"] == "run_python" else f"Approve `{p['tool']}`?")
        c1, c2 = st.columns(2)
        if c1.button("Approve", key="approve", type="primary"):
            label, note = JOB_NOTES.get(p["tool"], (f"Running {p['tool']}", ""))
            start_job(label, lambda: resolve_pending(s, approved=True), note)
        pending_id = f"{s['run_id']}-{len(s['events'])}"     # a new request never inherits an open reject form
        c2.button("Reject", key="reject", on_click=set_rejecting, args=(pending_id,))
        if ss.get("rejecting") == pending_id:
            with st.form("reject_form"):
                reason = st.text_area("Why reject? The agent will follow your reason.", key="reject_reason",
                                      placeholder="e.g. Only the latest date is needed, not the whole year.")
                sent = st.form_submit_button("Send rejection", type="primary")
            st.button("Cancel", key="cancel_reject", on_click=set_rejecting, args=(None,))
            if sent:
                if reason.strip():
                    ss.rejecting = None
                    resolve_pending(s, approved=False, reason=reason)
                    st.rerun()
                st.error("Please give a reason, so the agent knows what to change.")

    elif s["status"] == "awaiting_choice":
        p = s["pending"]
        st.info("The agent suggests more tools for this task (reason above). You decide.")
        picked = [t for t in p["toolkits"]
                  if st.checkbox(f"Enable toolkit `{t}` — {extensions.TOOLKITS[t]['description']}", value=True,
                                 key=f"suggest_{t}")]
        use_skill = bool(p.get("skill")) and st.checkbox(
            f"Use skill `{p['skill']}` — {get_skill(p['skill']).description}", value=True, key="suggest_skill")
        c1, c2 = st.columns(2)
        if c1.button("Use selected", key="use_suggestion", type="primary"):
            resolve_pending(s, choice={"toolkits": picked, "skill": p["skill"] if use_skill else None})
            add_to_session(s["toolkits"])         # accepted toolkits stay on for this session
            st.rerun()
        if c2.button("Continue without", key="decline_suggestion"):
            resolve_pending(s, choice={})
            st.rerun()

    elif s["status"] == "awaiting_user":
        # the question itself is already in the chat above: only the ways to answer it here
        if st.button("Yes, go ahead", key="confirm_proposal", type="primary",
                     help="Confirms what the agent proposed in its question."):
            resolve_pending(s, answer="Yes, confirmed: go ahead with your proposal.")
            st.rerun()
        with st.form("answer_form", clear_on_submit=True):
            answer = st.text_input("Or type your answer / correction", key="answer")
            submitted = st.form_submit_button("Send answer")
        if submitted:
            if not answer.strip():
                st.error("Enter an answer first.")
            else:
                resolve_pending(s, answer=answer)
                st.rerun()

    elif s["status"] in ("planning", "running"):
        last = s["events"][-1] if s["events"] else None
        if last and last["kind"] == "model_error":
            # Auto-run pauses here so a dead backend isn't called in a loop.
            hint = " Is Ollama running? Start it with `ollama serve`." if backend == "ollama" else ""
            st.error(f"Model call failed: {last['payload']['error']}.{hint}")
            go = st.button("Retry", key="retry", type="primary")
        else:
            go = ss.autorun or st.button("Next step", key="next")
        if go:
            start_job(f"Step {s['step_count'] - s.get('task_start', 0) + 1}: the agent is working",
                      lambda: run_one_step(s, model))

    elif s["status"] == "finished":
        for w in s.get("warnings", []):
            st.warning(w)
        files = exports(s)
        a, c, p_ = st.columns(3)
        a.download_button("Download answer (.md)", s["final"] or "", file_name="answer.md", key="download")
        c.download_button("Download conversation (.html)", files["html"], file_name=f"conversation_{s['run_id']}.html",
                          mime="text/html", key="download_html",
                          help="The whole conversation: requests, steps, answers, tables and sources, in one page.")
        if files["script"] is None:
            p_.caption("No analysis script: this conversation read no data and ran no analysis code.")
        elif isinstance(files["script"], Exception):
            p_.error(f"Could not build the analysis script: {files['script']}")
        else:
            p_.download_button("Download analysis script (.py)", files["script"], file_name=f"analysis_{s['run_id']}.py",
                               mime="text/x-python", key="download_script",
                               help="Replays the analysis: the data reads, queries and approved code in order, then "
                                    "rebuilds each answer and checks each step against what the agent saw.")


if s and ss.get("io_job"):
    show_progress()
if s:
    actions(s, model, backend)
    render_model_calls(s)

# ---------------------------------------------------------------- message box (+ attachments)
ready = not s or s["status"] in DONE
submitted = st.chat_input(("Send a follow-up instruction" if s else "Describe the task") if ready
                          else "The agent is working on the current instruction (Stop to interrupt)",
                          key="goal", disabled=not ready, accept_file="multiple", file_type=ATTACHABLE)
if submitted and (submitted if isinstance(submitted, str) else submitted.text).strip() == "/results" and s:
    ss.results_page_for = s["run_id"]               # created only on request
    st.rerun()
if submitted:
    message, files = (submitted, []) if isinstance(submitted, str) else (submitted.text or "", submitted.files)
    attached = save_uploads(files)                  # attachments become inputs of the conversation
    message = message.strip() or ("Look at the attached file(s)." if attached else "")
    cmd = parse_commands(message)               # /toolkit, /tool or /skill in the message
    skill = cmd["skill"] or (None if skill_choice == "Auto" else skill_choice)
    toolkits = list(dict.fromkeys(ss.session_toolkits + cmd["toolkits"]
                                  + (get_skill(skill).toolkits if get_skill(skill) else [])))
    if toolkits != ss.session_toolkits:
        add_to_session(toolkits)
    existing = sorted(p.name for p in config.inputs_dir().glob("*")) if config.inputs_dir().exists() else []
    mentioned = [f"inputs/{n}" for n in existing if n in message]       # files named in the message
    inputs = sorted(set(attached + mentioned))
    if s:
        continue_with(s, cmd["goal"] or message, inputs=inputs, skill=skill, toolkits=toolkits,
                      max_steps=ss.get("max_steps", 0))
    else:
        ss.state = new_state(cmd["goal"] or message, inputs=inputs, skill=skill, toolkits=toolkits,
                             max_steps=ss.get("max_steps", 0))
    st.rerun()
