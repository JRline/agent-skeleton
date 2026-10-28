You are helping maintain the evaluation set of a Python analysis agent. You cannot see the project; everything
you need is below. Write golden tasks: questions with the facts a correct answer must contain. The eval runs each
question as a new conversation and checks the answer.

THE REQUEST:
{request}

RULES:
- Reply with one fenced JSON block per task, each named `tests/golden/<id>.json`, then one line per task saying
  what behaviour it tests.
- Fields: id, instruction, and any of: inputs (files), expect_numbers ([{"value": ..., "tol": ...}]),
  expect_keywords (a string, or a list meaning "any one of"), expect_ask (true if the agent must ask first),
  max_asks, max_tool_calls, max_steps.
- Expected numbers must come from the data, worked out by me or a tool, never estimated. Mark any value you
  cannot verify as "TO CHECK" in your notes so I compute it before saving.
- One behaviour per task. Vary the wording from existing tasks; do not copy them.
- Use only datasets, columns and values in the dataset files below.

After my reply I will compute any value marked TO CHECK, save the files and run the evals.
