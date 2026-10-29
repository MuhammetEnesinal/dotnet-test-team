"""verify-integrity --gate: checks one run (any status) and exits 1 on ANY mismatch; never blocks, never counts.

Used by the skill in its final report. Prints {"ok": true} (exit 0) or {"ok": false, "changedOrMissingTargets": n} (exit 1).
When state.json says a test database was used (dbUsed), the gate also fails while the run's container dtt-run-<id> still exists
({"ok": false, "databaseContainerLeft": true}); without a usable docker the result carries a note and is not failed for that.
Exit 2 = the gate could not run (message on stderr)."""
from __future__ import annotations

import os
import re
import sys

from ..core import docker as dk_mod, jsonio, state as st
from ..core.hashing import file_sha256
from ..core.paths import PathError, fs_path, resolve_inside
from ..core.textsafe import safe_ascii
from ._common import emit_json, project_root

_RUN_RX = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def add_arguments(p):
    p.add_argument("--gate", action="store_true", required=True, help="check one run and exit 1 on any mismatch")
    p.add_argument("--run-id", default="")
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")


def _fail(message):
    sys.stderr.write(message + "\n")
    return 2


class NoTargets(ValueError):
    """state.json records no target: "unchanged" would be vacuous, so the check cannot be passed."""


def changed_targets(root, state):
    """Number of targets of the run whose file is missing or differs from the recorded sha256 and is not covered by an approved fix.
    Raises NoTargets (a ValueError) when no target is recorded and ValueError when a target has no path. (Also used by `dtt run` for its closing `production:` line.)"""
    targets = state.get("targets")
    if targets is None:
        raise NoTargets("the run has no target recorded")
    if not isinstance(targets, list):
        targets = [targets]
    if not targets:
        raise NoTargets("the run has no target recorded")
    approved = state.get("approvedFixes")
    approved = approved if isinstance(approved, list) else []
    bad = 0
    for t in targets:
        if not isinstance(t, dict) or not isinstance(t.get("path"), str):
            raise ValueError("a target has no path")
        cur = resolve_inside(root, t["path"])
        if not (cur.exists and os.path.isfile(fs_path(cur.path))):
            bad += 1
            continue
        h = file_sha256(cur.path)
        if h == t.get("sha256"):
            continue
        if not any(isinstance(a, dict) and st.same_state_path(a.get("path"), t["path"]) and a.get("sha256") == h for a in approved):
            bad += 1
    return bad


def database_container_state(run_id, state, root=None):
    """'none' (no database was used / no such container), 'left' (the run's container still exists), 'unchecked' (docker not usable)."""
    if state.get("dbUsed") is not True:
        return "none"
    try:
        name = dk_mod.run_name(run_id)
        info = dk_mod.inspect(dk_mod.Docker.find(exclude_dirs=(root,) if root else ()), name)
    except dk_mod.DockerError as exc:
        return "none" if "bad container name" in str(exc) else "unchecked"
    return "none" if info is None else "left"


def run(args):
    try:
        if not _RUN_RX.fullmatch(args.run_id or ""):
            return _fail("Gate: --run-id is required.")
        root = project_root(args)
        state_res = resolve_inside(root, ".test-generator/runs/%s/state.json" % args.run_id)
        # The gate must work whatever the run status is (also after "done").
        if not (state_res.exists and os.path.isfile(fs_path(state_res.path))):
            return _fail("Gate: run not found.")
        state = jsonio.read_json_file(state_res.path, jsonio.MAX_STATE_BYTES)
        if not isinstance(state, dict) or "targets" not in state:
            return _fail("Gate: state.json is not usable.")
        bad = changed_targets(root, state)
        result = {"ok": True}
        if bad:
            result = {"ok": False, "changedOrMissingTargets": bad}
        db_state = database_container_state(args.run_id, state, root)
        if db_state == "left":
            result["ok"] = False
            result["databaseContainerLeft"] = True
        elif db_state == "unchecked":
            result["note"] = "database container not checked (docker is not usable)"
        emit_json(result)
        return 0 if result["ok"] else 1
    except (PathError, jsonio.JsonError, OSError, ValueError, KeyError) as exc:
        return _fail("Gate could not run: " + safe_ascii(exc, 200))
