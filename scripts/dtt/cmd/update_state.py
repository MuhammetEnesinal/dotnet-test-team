"""update-state: updates a run's state.json (atomic write). Only a fixed set of fields can be changed. Prints "ok"."""
from __future__ import annotations

import re
import sys

from ..core import runs, state as st
from ..core.docker import DockerError
from ..core.hashing import file_sha256
from ..core.textsafe import clean_text
from ._common import CmdError, emit, project_root, resolve
from .db import down_run

STATUSES = ("open", "done", "escalated", "aborted")


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--run-id", required=True, help="run id of the open run")
    p.add_argument("--step", choices=st.STEPS)
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--approved-fix-path")
    p.add_argument("--note")


def run(args):
    if not st.is_run_id(args.run_id):
        raise CmdError("Invalid run id.")
    if args.note is not None and len(args.note) > 300:
        raise CmdError("--note is limited to 300 characters.")

    root = project_root(args)
    state_res = resolve(root, ".test-generator/runs/%s/state.json" % args.run_id)
    if not state_res.exists:
        raise CmdError("No state.json for run " + args.run_id)

    approved_rel = approved_hash = None
    if args.approved_fix_path is not None:
        # The user approved a change to this production file: record its current hash as the new expected hash.
        full = resolve(root, args.approved_fix_path)
        if not full.exists:
            raise CmdError("The approved file does not exist.")
        approved_rel = full.rel
        approved_hash = file_sha256(full.path)

    def mutate(s):
        if args.step is not None:
            s["step"] = args.step
        if args.status is not None:
            s["status"] = args.status
        if approved_rel is not None:
            targets = s.get("targets")
            known = [t for t in (targets if isinstance(targets, list) else []) if isinstance(t, dict) and st.same_state_path(t.get("path"), approved_rel)]
            if not known:
                raise CmdError("Not a target of this run: " + re.sub(r"[^A-Za-z0-9 ._/-]", "_", approved_rel)[:100])
            fixes = s.get("approvedFixes")
            fixes = fixes if isinstance(fixes, list) else ([] if fixes is None else [fixes])
            s["approvedFixes"] = fixes + [{"path": approved_rel, "sha256": approved_hash, "approvedUtc": st.utc_now_iso()}]
        if args.note:
            notes = s.get("notes")
            notes = notes if isinstance(notes, list) else ([] if notes is None else [notes])
            s["notes"] = notes + ["%s %s" % (st.utc_now_iso(), clean_text(args.note, 300))]
        s["updatedUtc"] = st.utc_now_iso()

    st.update_state_file(state_res.path, mutate)
    emit("ok\n")
    if args.status in ("done", "aborted", "escalated"):
        # A run that ends takes its test database container with it (no keep; a kept container was renamed by `dtt db down --keep` before).
        # Docker is only asked when db.json exists or state.json says a database was used. A failure is a warning, never a block.
        try:
            down_run(root, args.run_id, False)
        except DockerError as exc:
            sys.stderr.write("warning: the test database container of this run was not removed (%s). Run: dtt db down --run-id %s\n"
                             % (clean_text(str(exc), 160), args.run_id))
        except Exception:
            sys.stderr.write("warning: the test database container of this run could not be checked. Run: dtt db down --run-id %s\n" % args.run_id)
    if args.status == "done":
        # Only a finished run is cleaned: its working files go, state.json, 04-findings.md and report.md stay. A failure is a warning.
        left = runs.cleanup_finished_run(root, args.run_id)
        if left:
            sys.stderr.write("warning: %d item(s) of the finished run folder could not be removed.\n" % left)
    return 0
