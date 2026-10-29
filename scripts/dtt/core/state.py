"""state.json schema v1 and read-modify-write (atomic replace; the writers are the main conversation's sequential `dtt` calls, so there is no lock)."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone

from . import jsonio

RUN_ID_RX = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SCAN_RUN_ID_RX = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,40}")      # the form `dtt scan` generates: <date>-<time>-<slug>
AGENT_PREFIX = "dotnet-test-team:"
RESTRICTED_AGENT = "restricted"
OUR_AGENT_RX = re.compile(
    r"dotnet-test-team:(scenario-analyst|test-writer|failure-classifier)"
)
STEPS = ("consent", "analyze", "approval", "scaffold", "write", "run", "report")
MODES = ("standard", "deep")      # a state file with another mode value still loads; status shows it as unknown
SCHEMA_VERSION = 1
MAX_ROUNDS = 3                  # the same cap as `dtt run`


def is_run_id(value) -> bool:
    """fullmatch (not match + $): a trailing newline can never slip through."""
    return isinstance(value, str) and RUN_ID_RX.fullmatch(value) is not None


def is_scan_run_id(value) -> bool:
    """True for ids of the form `dtt scan` generates (only these are printed by status / the SessionStart hint)."""
    return isinstance(value, str) and SCAN_RUN_ID_RX.fullmatch(value) is not None


def our_agent(agent) -> Optional[str]:
    """Short agent name ('test-writer') if `agent` is exactly one of this plugin's agents. Any other name that starts with
    'dotnet-test-team:' (case-insensitive, also after NFKC and surrounding blanks) is a plugin agent this code does not know: it
    gets RESTRICTED_AGENT, which may write nothing. Everything else (the main conversation, other plugins) is None."""
    if not isinstance(agent, str):
        return None
    m = OUR_AGENT_RX.fullmatch(agent)
    if m:
        return m.group(1)
    if unicodedata.normalize("NFKC", agent).strip().casefold().startswith(AGENT_PREFIX):
        return RESTRICTED_AGENT
    return None


def enum_or_unknown(value, allowed: Iterable[str]) -> str:
    """Value only if it is exactly one of the allowed strings, else 'unknown'. Used before echoing state-derived text."""
    if isinstance(value, str) and value in tuple(allowed):
        return value
    return "unknown"


def coerce_int(value) -> int:
    """Integer coercion: ints, bools, integral-ish floats and numeric strings; int32 range; ValueError otherwise."""
    if isinstance(value, bool):
        n = int(value)
    elif isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("not finite")
        n = int(round(value))
    elif isinstance(value, str):
        n = int(round(float(value.strip()))) if value.strip() else 0
    else:
        raise ValueError("not a number")
    if n < -2147483648 or n > 2147483647:
        raise ValueError("out of range")
    return n


def int_or(value, default: int = 0) -> int:
    try:
        return coerce_int(value)
    except (ValueError, OverflowError):
        return default


def utc_now_iso() -> str:
    """Like .NET ToString('o') in UTC: 2026-10-18T14:00:00.1234567Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def to_state_path(path: str) -> str:
    """Paths inside state.json use '/'."""
    return path.replace("\\", "/")


def state_path_parts(path: str):
    """Both separators accepted on read."""
    return [p for p in re.split(r"[\\/]", path) if p not in ("", ".")]


def _fold(part):
    """NFC first (macOS lists names decomposed), then case-fold."""
    return unicodedata.normalize("NFC", part).casefold()


def same_state_path(a, b) -> bool:
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return [_fold(p) for p in state_path_parts(a)] == [_fold(p) for p in state_path_parts(b)]


def new_state(run_id: str, mode: str, targets: list, now: Optional[str] = None) -> dict:
    now = now or utc_now_iso()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "mode": mode,
        "status": "open",
        "step": "consent",
        "round": 0,
        "maxRounds": MAX_ROUNDS,
        "targets": targets,          # [{"path": "src/A.cs", "sha256": "...", "snapshot": "snapshot/01-A.cs"}]
        "approvedFixes": [],         # [{"path": ..., "sha256": ..., "approvedUtc": ...}]
        "createdUtc": now,
        "updatedUtc": now,
        "notes": [],
    }


def read_state(state_path: str) -> dict:
    obj = jsonio.read_json_file(state_path, jsonio.MAX_STATE_BYTES)
    if not isinstance(obj, dict):
        raise jsonio.JsonError("state.json is not a JSON object.")
    return obj


def update_state_file(state_path: str, mutator: Callable[[dict], None]) -> None:
    """Read, change, write back atomically. Unknown keys are kept."""
    state = read_state(state_path)
    mutator(state)
    jsonio.write_json_atomic(state_path, state)
