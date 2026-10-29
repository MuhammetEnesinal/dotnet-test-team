"""status: one line per unfinished run (newest first, at most 3): run id, step, round and the scenario ids of 01-scenarios.json that
still have no `// S0xx` trace comment in the test files recorded for the run (written-files.txt; only those .cs files are read).

The same function feeds the SessionStart hint. Everything printed is built from validated values: the run id (only the form
<yyyymmdd>-<hhmmss>-<slug> that `dtt scan` generates, else a fixed placeholder), the
step (fixed list), the round (integer) and scenario ids that match S000 - never free text from the project."""
from __future__ import annotations

import re
import time

from ..core import docker as dk_mod, jsonio, runs, state as st, writtenfiles
from ..core.paths import PathError, resolve_inside
from ..core.textsafe import decode_source
from ._common import emit, project_root

MAX_RUNS = 3
MAX_IDS = 20
MAX_IDS_SESSION = 5
MAX_FILES_PER_RUN = 100
MAX_FILE_BYTES = 512 * 1024
MAX_SCENARIO_IDS = 500
TIME_BUDGET_SECONDS = 2.0
UNRECOGNISED_ID = "<unrecognised id skipped>"      # a run id that is not of the form `dtt scan` generates is never echoed
_ID_RX = re.compile(r"S[0-9]{3}")
_COMMENT_RX = re.compile(r"//[^\n]*")
_TRACE_RX = re.compile(r"\bS[0-9]{3}\b")


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")


def _scenario_ids(root, run_id):
    """Ids of 01-scenarios.json (None when the file is missing or unusable)."""
    try:
        res = resolve_inside(root, ".test-generator/runs/%s/01-scenarios.json" % run_id)
        if not res.exists:
            return None
        doc = jsonio.read_json_file(res.path, jsonio.MAX_SCENARIOS_BYTES)
        items = doc.get("scenarios") if isinstance(doc, dict) else None
        if not isinstance(items, list):
            return None
        ids = []
        for item in items:
            sid = item.get("id") if isinstance(item, dict) else None
            if isinstance(sid, str) and _ID_RX.fullmatch(sid) and sid not in ids:
                ids.append(sid)
            if len(ids) >= MAX_SCENARIO_IDS:
                break
        return ids
    except (PathError, jsonio.JsonError, OSError, ValueError):
        return None


def _traced_ids(root, run_dir, deadline):
    """(ids found in trace comments of the recorded .cs files, complete?)."""
    found = set()
    complete = True
    for rel in writtenfiles.load(run_dir)[:MAX_FILES_PER_RUN]:
        if time.monotonic() > deadline:
            complete = False
            break
        if not rel.lower().endswith(".cs"):
            continue
        try:
            res = resolve_inside(root, rel)
            if not res.exists:
                continue
            text = decode_source(jsonio.read_bytes_capped(res.path, MAX_FILE_BYTES, refuse_hardlinks=True))
        except (PathError, jsonio.JsonError, OSError):
            continue
        for comment in _COMMENT_RX.findall(text):
            found.update(_TRACE_RX.findall(comment))
    return found, complete


def _database_line(run, root=None):
    """'database container: running|missing' for an open run that has a db.json (one docker call, only then). Fixed words only."""
    try:
        data = dk_mod.read_db_json(run.run_path)
    except dk_mod.DockerError:
        return "database container: missing" if st.is_scan_run_id(run.run_id) else None
    if data is None:
        return None
    return "database container: %s" % ("running" if dk_mod.is_alive(data["name"], root=root) else "missing")


def summaries(root, max_runs=MAX_RUNS, max_ids=MAX_IDS, database=False):
    """One text line per unfinished run, newest first. Never raises on bad project data."""
    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    open_runs = runs.get_open_runs(root, limit=max_runs, max_candidates=runs.HOOK_MAX_CANDIDATES, max_listing=runs.HOOK_MAX_LISTING,
                                   deadline=deadline, newest_first=True)
    lines = []
    for run in sorted(open_runs, key=lambda r: r.run_id, reverse=True)[:max_runs]:
        s = run.state
        step = st.enum_or_unknown(s.get("step"), st.STEPS) if "step" in s else "unknown"
        rnd = min(max(st.int_or(s.get("round"), 0), 0), 99) if "round" in s else 0
        ids = _scenario_ids(root, run.run_id)
        if ids is None:
            tail = "no scenarios yet"
        else:
            traced, complete = _traced_ids(root, run.run_path, deadline)
            missing = [i for i in ids if i not in traced]
            if not missing:
                tail = "all %d scenario(s) traced" % len(ids) if complete else "untraced: unknown (time budget)"
            else:
                tail = "untraced: " + " ".join(missing[:max_ids]) + (" (+%d more)" % (len(missing) - max_ids) if len(missing) > max_ids else "")
                if not complete:
                    tail += " (partial)"
        shown = run.run_id if st.is_scan_run_id(run.run_id) else UNRECOGNISED_ID
        lines.append("run %s: step %s, round %d, %s" % (shown, step, rnd, tail))
        db_line = _database_line(run, root) if database else None        # one docker call per run with a db.json: `dtt status` only, never the hook
        if db_line:
            lines.append(db_line)
    return lines


def run(args):
    root = project_root(args)
    lines = summaries(root, database=True)
    emit("\n".join(lines) + "\n" if lines else "no unfinished runs\n")
    return 0
