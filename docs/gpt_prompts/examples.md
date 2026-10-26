You are helping maintain the planner of a Python analysis agent. You cannot see the project; everything you need
is below. Update the planner's worked examples: short examples of a goal and the planner's JSON reply, shown to
the planner so it suggests the right skill and writes good plans.

THE REQUEST:
{request}

RULES:
- Reply with the complete file `plugins/examples/planner.md` in one fenced block (keep the HTML comment at the
  top), then one line per example saying what decision it teaches.
- 3 to 5 examples, varied: at least one where no skill fits, one with an open choice the plan must confirm, and
  one general-knowledge question. Each has GOAL, INPUT FILES, a one-line Reasoning, and REPLY (one JSON line in
  the shape the current file uses).
- Use only skill and toolkit names from the lists below. Synthetic content only. Never copy an eval question.

After my reply I will save the file and run the unit tests and the evals.
