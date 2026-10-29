"""Run folders below <project>/.test-generator/runs."""
from __future__ import annotations

import itertools
import os
import stat
import time
from collections import namedtuple

from . import jsonio, state as state_mod
from .paths import PathError, resolve_inside, fs_path, is_link_stat

TG_DIR = ".test-generator"
RUNS_DIR = "runs"

# Work bounds for hooks (a repository can ship thousands of padded fake runs; a hook that is killed by its timeout does not block).
HOOK_MAX_OPEN_RUNS = 20          # open runs examined, newest first
HOOK_MAX_CANDIDATES = 60         # run folders whose state.json is read at all
HOOK_MAX_LISTING = 5000          # directory entries looked at
HOOK_TIME_BUDGET_SECONDS = 2.0   # overall budget for discovery
ENV_FILE_CAP_BYTES = 256 * 1024  # 00-environment.json read by the guards

# index: 1-based among open runs
OpenRun = namedtuple("OpenRun", "index run_id run_path state_path state")


def runs_root(project_root: str) -> str:
    return os.path.join(project_root, TG_DIR, RUNS_DIR)


def new_deadline(seconds: float = HOOK_TIME_BUDGET_SECONDS) -> float:
    return time.monotonic() + seconds


def get_open_runs(project_root: str, windows=None, limit=None, max_candidates=None, max_listing=None, deadline=None,
                  newest_first=False, max_state_bytes=jsonio.MAX_STATE_BYTES) -> list:
    """Every run whose state.json says status == 'open' (sorted by run id, indexes 1..n). Never raises: unreadable, oversized
    or malformed state is skipped, and anything reached through a symlink / junction is refused.

    limit / max_candidates / max_listing / deadline (time.monotonic value) bound the work; with newest_first the run folders are
    examined by modification time (newest first) so the bound keeps the most recent ones."""
    found = []
    try:
        root_res = resolve_inside(project_root, os.path.join(TG_DIR, RUNS_DIR), windows=windows)
    except PathError:
        return []
    try:
        with os.scandir(fs_path(root_res.path)) as it:
            entries = list(itertools.islice(it, max_listing)) if max_listing else list(it)
    except OSError:
        return []
    candidates = []
    for entry in entries:
        try:
            if not state_mod.is_run_id(entry.name):
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            mtime = entry.stat(follow_symlinks=False).st_mtime if newest_first else 0
            candidates.append((mtime, entry.name))
        except OSError:
            continue
    if newest_first:
        candidates.sort(key=lambda c: (-c[0], c[1]))
    else:
        candidates.sort(key=lambda c: c[1])
    examined = 0
    for _mtime, name in candidates:
        if deadline is not None and time.monotonic() > deadline:
            break
        if max_candidates is not None and examined >= max_candidates:
            break
        if limit is not None and len(found) >= limit:
            break
        examined += 1
        try:
            run_res = resolve_inside(project_root, os.path.join(TG_DIR, RUNS_DIR, name), windows=windows)
            state_path = os.path.join(run_res.path, "state.json")
            state_res = resolve_inside(project_root, os.path.join(TG_DIR, RUNS_DIR, name, "state.json"), windows=windows)
            if not state_res.exists:
                continue
            obj = jsonio.read_json_file(state_path, max_state_bytes)
            if not isinstance(obj, dict):
                continue
            status = obj.get("status")
            if isinstance(status, str) and status == "open":
                found.append((name, run_res.path, state_path, obj))
        except (PathError, jsonio.JsonError, OSError, ValueError):
            continue
    found.sort(key=lambda f: f[0])
    return [OpenRun(i, n, rp, sp, st) for i, (n, rp, sp, st) in enumerate(found, 1)]


def get_open_runs_bounded(project_root: str, windows=None, deadline=None) -> list:
    """get_open_runs with the hook limits: 20 open runs, 60 state files, 5000 directory entries, 2 s, newest first."""
    return get_open_runs(project_root, windows, limit=HOOK_MAX_OPEN_RUNS, max_candidates=HOOK_MAX_CANDIDATES, max_listing=HOOK_MAX_LISTING,
                         deadline=deadline if deadline is not None else new_deadline(), newest_first=True)


# ---------------------------------------------------------------- cleanup of a finished run
KEEP_AFTER_DONE = ("state.json", "04-findings.md", "report.md")
_CLEANUP_MAX_DEPTH = 8


def _remove_entry(path, depth, failed):
    """Delete one file, link or directory tree. A link (symlink, junction, other reparse point) is removed as a link and never
    entered, so nothing outside `path` is touched. Failures are collected, not raised."""
    try:
        info = os.lstat(fs_path(path))
        if is_link_stat(info):
            try:
                os.unlink(fs_path(path))
            except OSError:
                os.rmdir(fs_path(path))             # a directory junction goes with rmdir, which does not follow it
        elif stat.S_ISDIR(info.st_mode):
            if depth >= _CLEANUP_MAX_DEPTH:
                raise OSError("too deep")
            with os.scandir(fs_path(path)) as it:
                names = [e.name for e in it]
            for name in names:
                _remove_entry(os.path.join(path, name), depth + 1, failed)
            os.rmdir(fs_path(path))
        else:
            os.unlink(fs_path(path))
    except OSError:
        failed.append(path)


def cleanup_finished_run(project_root: str, run_id: str) -> int:
    """After `update-state --status done`: delete the working files of the run folder and keep state.json, 04-findings.md and
    report.md. The folder must be a direct child of <project>/.test-generator/runs and must not be a link (resolve_inside refuses
    links). Returns the number of items that could not be removed (0 = clean); never raises."""
    failed = []
    try:
        if not state_mod.is_run_id(run_id):
            return 1
        res = resolve_inside(project_root, "/".join((TG_DIR, RUNS_DIR, run_id)))
        if not res.exists or len(res.parts) != 3 or res.parts[0].casefold() != TG_DIR or res.parts[1].casefold() != RUNS_DIR:
            return 1
        keep = set(k.casefold() for k in KEEP_AFTER_DONE)
        with os.scandir(fs_path(res.path)) as it:
            names = [e.name for e in it]
        for name in names:
            if name.casefold() not in keep:
                _remove_entry(os.path.join(res.path, name), 1, failed)
    except (PathError, OSError):
        failed.append(run_id)
    return len(failed)
