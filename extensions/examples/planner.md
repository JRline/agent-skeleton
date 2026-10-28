<!--
Worked examples for the planner prompt (agent/prompts/planner.md, slot {{examples}}).
Swap this file for each environment: production can replace it, or point AGENT_EXAMPLES at another folder.
Rules (checked by tests/test_examples.py):
- Synthetic data only. Use skill and toolkit names that exist in the same environment.
- Each example: <example> with GOAL, INPUT FILES, a one-line Reasoning, and a REPLY JSON in the planner's shape.
- Keep 3-5 short, varied examples; never copy an eval (golden) task.
This comment is removed before the prompt is sent.
-->
<example>
GOAL: What does this workbook contain?
INPUT FILES: inputs/Q3 exposure pack.xlsx
Reasoning: The GOAL asks what a local file contains, which is what file_summary produces. No data choice is open.
REPLY: {"suggest": {"toolkits": [], "skill": "file_summary", "why": "The goal asks what the attached workbook contains."}, "plan": ["Read inputs/Q3 exposure pack.xlsx and its layout", "Return a summary of its sheets, tables and key figures"], "title": "Q3 exposure pack contents"}
</example>

<example>
GOAL: Which month had the highest total exposure in the attached file?
INPUT FILES: inputs/expsoures_2025.csv
Reasoning: One figure from a local file, not a summary of it, so no skill fits. The file name is copied exactly.
REPLY: {"suggest": {"toolkits": [], "skill": "none", "why": "A single figure from a local file needs no skill."}, "plan": ["Read inputs/expsoures_2025.csv", "Find the month with the highest total exposure in Python", "Return the month and its total"], "title": "Peak exposure month 2025"}
</example>

<example>
GOAL: How did leverage exposure for the Asia region change between quarter ends?
INPUT FILES: (none)
Reasoning: Comparing a data lake resource between two dates is what usage_comparison produces. The quarter ends are not given and the data spans many years, so they are confirmed first.
REPLY: {"suggest": {"toolkits": [], "skill": "usage_comparison", "why": "The goal compares leverage exposure for a region between two dates."}, "plan": ["Confirm with the user: compare FY2025Q2 end with FY2025Q3 end", "Find the dataset with leverage exposure by region and date", "Extract Asia leverage exposure at both quarter ends and compute the change in Python", "Return the change and its main drivers"], "title": "Asia leverage quarter change"}
</example>

<example>
GOAL: What is a capital conservation buffer?
INPUT FILES: (none)
Reasoning: A definition from general knowledge: one step, no tools, no skill.
REPLY: {"suggest": {"toolkits": [], "skill": "none", "why": "A definition needs no data."}, "plan": ["Explain what a capital conservation buffer is"], "title": "Capital conservation buffer"}
</example>
