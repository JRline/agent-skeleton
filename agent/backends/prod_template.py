"""Shape the production API module must have (copy into the closed environment and fill in).

The agent imports the module named by PROD_API_MODULE (default `existing_api_module`)
and calls `call(text, effort)`. If your existing module has a different signature,
write a 10-line wrapper like this one rather than changing the agent.
"""


def call(text: str, effort: str = "none") -> str:
    """Send one prompt to the production model and return the reply text.

    text   : the full prompt (role-tagged messages flattened by the adapter)
    effort : "none" or "medium"
    """
    # Example only. Replace with the real internal client:
    # from internal_llm import client
    # return client.generate(model="<production model>", input=text, reasoning_effort=effort).text
    raise NotImplementedError("Wire this to the production API module")
