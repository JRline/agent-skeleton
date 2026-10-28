"""Inner model for tests of agent/backends/session_sim.py: records each call, answers from a queue."""
calls: list[dict] = []
replies: list[str] = []


def reset(answers=()):
    calls.clear()
    replies[:] = list(answers)


def call(messages, effort="none"):
    calls.append({"messages": [dict(m) for m in messages], "effort": effort})
    if replies and replies[0] == "FAIL":
        replies.pop(0)
        raise RuntimeError("inner backend down")
    return replies.pop(0) if replies else "ok"
