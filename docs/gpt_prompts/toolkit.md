You are helping maintain a Python analysis agent that runs on the GPT CLI. You cannot see the project;
everything you need is below. Write or update ONE toolkit: a Python module of tools the agent can call.

THE REQUEST:
{request}

RULES (from the contract below; do not break them):
- Reply with two complete files in fenced blocks: `extensions/toolkits/<name>.py` and `tests/test_ext_<name>.py`,
  then a short list of what you changed and why.
- Standard library plus pydantic, pandas and openpyxl only. Import from `agent.tools` only; do not change any
  other part of the project.
- Every tool: a Pydantic args model whose fields all have descriptions, and a description that says what it
  returns, how big, and when NOT to use it. All arithmetic happens inside the tool.
- Files only inside the sandbox (`sandbox_path`). Anything that writes, sends or deletes is `risky=True`.
- Errors: raise ToolError with a message the model can act on. Keep the preview small; put full data in `full`.
- Tests: load the plugin, assert no load errors, and call each tool through `dispatch` for normal use, bad input
  and sandbox limits. Synthetic data only.

After my reply I will save the files and run the unit tests, and paste back any failures.
