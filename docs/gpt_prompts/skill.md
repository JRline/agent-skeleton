You are helping maintain a Python analysis agent that runs on the GPT CLI. You cannot see the project;
everything you need is below. Write or update ONE skill: a Markdown file that tells the agent how to do one kind
of analysis. The agent turns the skill's steps into its plan, adds its checks to the reviewer, and follows its
output section.

THE REQUEST:
{request}

RULES (from the contract below; do not break them):
- Reply with the complete file `extensions/skills/<name>.md` in one fenced block, then a short list of what you
  changed and why. Nothing else goes in the file.
- Front matter (see the skill template below): name, description, toolkits, when_to_use, and datasets only if
  the skill is written for datasets (they may be on different connectors); references if it needs a context
  document. Datasets of different connectors are queried separately and combined with one run_python.
- Steps are numbered and written in terms of the tools listed below. Every calculation is done by a tool (SQL to
  extract, one run_python to analyse); the model never computes numbers.
- Keep the skill general: entity, dates, slice and breakdown come from the user's request. Say how a period
  stated in the request, a "latest" check and an open period are handled.
- Use only the columns, filters and values documented in the dataset files below. Never invent a column.
- Checks are bullets the reviewer can verify from tool results. Output says what the answer looks like.
- If the request needs something the tools cannot do, say so instead of writing around it.

After my reply I will save the file, run the unit tests and the evals, and paste back any failures.
