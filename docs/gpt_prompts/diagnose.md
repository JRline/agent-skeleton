You are helping maintain a Python analysis agent that runs on the GPT CLI. You cannot see the project;
below is the record of one run that went wrong, plus the skill it used. Find out why.

WHAT WENT WRONG (my words):
{request}

HOW THE AGENT WORKS (so you can read the record):
- planner: writes the plan (or suggests a skill whose steps become the plan).
- executor: one JSON action per step: a tool call or the final answer.
- action_review: checks each data read or analysis request before the user approves it.
- reviewer: checks the final answer against the facts; the agent retries if it fails.
- summariser: compresses old turns. USER ANSWER / USER REJECTED facts are the user's own words.

PLEASE REPLY WITH:
1. The first step where the run went wrong, quoting the record.
2. The root cause: the skill text, a prompt rule, missing data documentation, or the model itself.
3. The smallest fix, as an exact text change to the skill or dictionary (preferred) or a described change to a
   prompt or code (these need the owner's approval). Say how an eval task could catch it next time.
