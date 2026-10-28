"""Shape the production API module must have (copy into the closed environment and fill in).

The agent imports the module named by PROD_API_MODULE (default `existing_api_module`)
and calls `call(text, effort)`. If your existing module has a different signature,
write a 10-line wrapper like this one rather than changing the agent.

Session memory mode (MODEL_MEMORY=session, docs/design.md §4a), only if each production instance keeps its own
memory: add a `channel` parameter ("executor", "check" or "review"; send each to its own fixed instance, whose
effort is config.CHANNEL_EFFORT[channel]) and a `new_session()` that starts fresh instances. See
agent/backends/session_sim.py for the expected behaviour. Without `channel`, the agent stays stateless.
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
