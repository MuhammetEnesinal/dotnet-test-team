"""Helpers shared by the hook entry points.

Hook contract: exit 0 = allow / nothing to say, exit 2 = deliberate block (reason on stderr). A hook exits 0 silently
when the payload is not about one of this plugin's agents; guards turn an internal error into a deny only for our own
agents; everything else fails open."""
from __future__ import annotations

from ..core import jsonio
from ..core.state import our_agent


def agent_of(payload: dict) -> str:
    agent = payload.get("agent_type", "")
    return agent if isinstance(agent, str) else ""


def our_agent_name(payload: Optional[dict]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    return our_agent(agent_of(payload))


def tool_input(payload: dict) -> dict:
    ti = payload.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def block(prefix: str, reason: str) -> int:
    jsonio.write_stderr(prefix + ": " + reason)
    return 2
