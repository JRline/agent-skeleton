You are helping maintain the data dictionary of a Python analysis agent. You cannot see the project; everything
you need is below. The agent reads this dictionary before querying a data lake view, and its request checker
uses it to check filters. Add or update the section for ONE view.

THE REQUEST (with the view's column list and notes I copied from the source system):
{request}

RULES:
- Reply with the complete updated `sandbox/references/datalake_dictionary.md` in one fenced block, then a short
  list of what you changed.
- Follow the format in docs/datasource_entry.md (below) and the existing `usage_limits` section: description, required filters, default filters,
  grain and keys, filters needed for correct numbers (each with why it double counts if left out), time and the
  latest date, hierarchies, measures and notes.
- Write only what my notes state. Mark anything you are unsure about as "TO CONFIRM" so I check it.
- State units, the grain, and which columns must never be added together.

After my reply I will check the TO CONFIRM items, save the file and run the evals that use this view.
