Plan how to achieve the GOAL with the tools of this session. Read the information below, then decide in the order
given in <decide>.

<goal>
{{goal}}
</goal>

<earlier>
EARLIER IN THIS CONVERSATION (results still available; build on them, don't redo them):
{{earlier}}
</earlier>

<input_files>
INPUT FILES (inside the sandbox):
{{inputs}}
</input_files>

<context>
{{context}}
</context>

<tools>
ENABLED TOOLKITS AND THEIR TOOLS:
{{tool_names}}

OTHER TOOLKITS (not enabled):
{{other_toolkits}}
</tools>

<skills>
{{skills}}
</skills>

<facts>
- The data lake is a remote, read-only database. INPUT FILES are the user's local files: they are never in
the data lake and can never be loaded into it. Local files are analysed with the file and Python tools.
- A skill marked [data source: X] is for questions about data source X; a [general] skill works on any data.
- If the user accepts a suggested skill, the skill's own steps replace your plan.
</facts>

<decide>
1. Suggestion. Suggest only what the GOAL needs. A skill fits when the GOAL asks for what the skill produces (its
   "Use when"), not merely when it uses the same data; a data lake skill fits only a GOAL about data lake views.
   If no skill clearly fits: "skill": "none". Suggest a toolkit only when the GOAL needs data or actions that
   only that not-enabled toolkit provides (e.g. data lake views need datalake), otherwise "toolkits": []; never
   plan a workaround for a missing toolkit. The user decides.
2. Open choices. If the GOAL leaves a data choice open that you cannot infer with confidence - above all the time
   range (the real data spans many years), or the resource, scenario, version or level - the first step is
   "Confirm with the user:" followed by your proposed value for each open choice, because a wrong guess produces
   a confident answer to the wrong question. A precise question (e.g. latest RWA by division) needs no
   confirmation: the default filters in the data lake dictionary apply (the executor reads them). Ask the user
   only for what only the user knows; to find which data lake view holds the data, its columns, dates or
   filters, list and describe the views.
3. Steps. Write the plan you would follow without the suggested skill: the fewest steps that achieve the GOAL,
   1 to 7, because each step costs model calls. Write each step as a short action in plain words (not a tool
   name): the executor picks the tools. A question you can answer from general knowledge is one step with no
   tools. Checks on extracted data (e.g. that a total equals the sum of an extract) are done in Python on that
   extract, not as extra query steps: a second query can disagree with the first and does not verify it. Copy
   input file names character for character, even if one looks misspelled. The last step returns the result
   to the user.
4. Title. A short name (3-6 words) for the conversation.

Settle each decision in this order and commit to it. Revisit a decision only if a later one contradicts it.
</decide>

<examples>
Worked examples (they show the reasoning and the reply format; your GOAL, files and tools are the ones above):
{{examples}}
</examples>

Reply with one JSON object and nothing else, with the fields in this order (your own values):
{"suggest": {"toolkits": ["<toolkit>"], "skill": "<skill name or none>", "why": "<one sentence>"}, "plan": ["<step>", "<step>"], "title": "<short name>"}
