"""SessionStart hook (startup|resume): reports unfinished runs so an interrupted run can be resumed (same function as `dtt status`).

Plain stdout of a SessionStart hook is added to the model's context. The text is built only from fixed sentences, a validated run
id, a step from a fixed list, a round number and scenario ids of the form S000 - never from free text found in the project."""
from __future__ import annotations

from ..core import jsonio, paths
from ..core.textsafe import safe_ascii
from ..cmd import status


SWEEP_BUDGET_SECONDS = 5.0
_MAX_RUN_FOLDERS = 200


def _project_has_db_json(root) -> bool:
    """Cheap file check: does any run folder of this project hold a db.json? (No docker call otherwise.)"""
    import itertools
    import os
    runs_dir = os.path.join(root, ".test-generator", "runs")
    try:
        with os.scandir(runs_dir) as it:
            for entry in itertools.islice(it, _MAX_RUN_FOLDERS):
                if entry.is_dir(follow_symlinks=False) and os.path.lexists(os.path.join(entry.path, "db.json")):
                    return True
    except OSError:
        pass
    return False


def _sweep_line(root):
    """Removes this plugin's stale test database containers (3 h for this project, 12 h for others), within a hard time budget.
    Returns the fixed sentence or None. Never raises."""
    try:
        if not _project_has_db_json(root):
            return None
        from ..core import docker
        dk = docker.Docker.find(exclude_dirs=(root,), deadline=docker._monotonic() + SWEEP_BUDGET_SECONDS)
        count = docker.sweep(dk, root)
        if count > 0:
            return "dotnet-test-team: removed %d old test database container(s)" % count
    except Exception:
        pass
    return None


def main() -> int:
    try:
        jsonio.read_hook_input()
        root = paths.get_project_root(None)
        lines = status.summaries(root, status.MAX_RUNS, status.MAX_IDS_SESSION)
        out = ["dotnet-test-team, unfinished " + line for line in lines]
        note = _sweep_line(root)
        if note:
            out.append(note)
        if out:
            jsonio.write_stdout("\n".join(out))
        return 0
    except Exception as exc:  # fail open
        jsonio.write_stderr("dotnet-test-team session-resume: " + safe_ascii(str(exc), 200))
        return 0
