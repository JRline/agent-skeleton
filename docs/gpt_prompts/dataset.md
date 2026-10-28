You are helping maintain a Python analysis agent that runs on the GPT CLI. You cannot see the project; everything
you need is below. A dataset (one table or view on a data source) is described by one Markdown file in its
connector's folder. The agent reads it before querying, and its request checker uses it to check filters. Write or
update the file for ONE dataset.

THE REQUEST (with the dataset's column list and notes I copied from the source system):
{request}

RULES:
- Reply with the complete file `extensions/connectors/<connector>/datasets/<dataset>.md` in one fenced block, then a
  short list of what you changed.
- Follow the dataset template and the "Datasets" section of docs/extensions.md below, and the existing datasets:
  front matter (name, description, partition_column, required_filters, default_filters), then grain and keys,
  filters needed for correct numbers (each with why it double counts if left out), time and the latest date,
  hierarchies, measures and notes.
- Write only what my notes state. Mark anything you are unsure about as "TO CONFIRM" so I check it.
- State units, the grain, and which columns must never be added together.

After my reply I will check the TO CONFIRM items, save the file and run the evals that use this dataset.
