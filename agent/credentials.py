"""Data sign-in per connector, held in memory only (docs/design.md §6).

The user types their user ID and password for a connector into the UI (or the CLI's hidden prompt). They live only
in this process's memory, keyed by conversation and connector, and reach the connection kind's
`connect(connector, login)` when a query runs. They are
never written to state.json, the log, the checkpoints or a prompt, so the model never sees them, and any error text
that echoes the password is masked before it is shown or stored. Signing out, or the process ending, forgets them.
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field

DEFAULT = "*"                        # one person per process (CLI, eval): every conversation uses this login


@dataclass(frozen=True)
class Login:
    user: str
    password: str = field(repr=False)

    def __repr__(self) -> str:       # a login printed or logged by mistake shows no password
        return f"Login(user={self.user!r}, password=***)"


_LOGINS: dict[tuple[str, str], Login] = {}      # (conversation or DEFAULT, connector) -> login
# The conversation being checked or run, for code that has no run id at hand (a tool's static check).
CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("lake_login_run", default=None)
_LOCK = threading.Lock()


def use(key: str, connector: str, login: Login | None) -> None:
    """The login a conversation's queries on a connector use (the UI calls this before every step); None signs out."""
    with _LOCK:
        if login is None:
            _LOGINS.pop((key, connector), None)
        else:
            _LOGINS[(key, connector)] = login


def forget(*keys: str) -> None:
    """Sign these conversations out of every connector (no keys: nothing; other sessions are never touched)."""
    with _LOCK:
        for k in [k for k in _LOGINS if k[0] in keys]:
            del _LOGINS[k]


def login_for(key: str | None, connector: str) -> Login | None:
    key = key or CURRENT.get()
    with _LOCK:
        return (_LOGINS.get((key, connector)) if key else None) or _LOGINS.get((DEFAULT, connector))


def scrub(text: str) -> str:
    """Mask every stored password in a message (a driver error may echo the connection string)."""
    with _LOCK:
        secrets = [lg.password for lg in _LOGINS.values() if len(lg.password) >= 3]
    for secret in secrets:
        text = text.replace(secret, "***")
    return text
