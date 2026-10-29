"""db: start and remove a throw-away database container for a test run (see core/docker.py and data/db-engines.json).

  db check --engine E [--tag T]
  db up --project-root R --run-id ID --engine E [--tag T] [--accept-eula] [--allow-pull]
  db down --project-root R --run-id ID [--keep]        |  db down --project-root R --engine E --kept
  db sweep [--project-root R]

Exit codes: 0 ok, 1 the user can fix it (consent flag missing, image not present, ...), 2 environment (Docker), 3 bad options.
All messages are fixed sentences; the password and the connection string are never printed."""
from __future__ import annotations

import os

from ..core import dbengines, docker as dk_mod, state as st
from ..core.docker import Docker, DockerError
from ._common import CmdError, emit, project_root, resolve

SUBCOMMANDS = ("check", "up", "down", "sweep")


def add_arguments(p):
    p.add_argument("action", choices=SUBCOMMANDS)
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--run-id", help="run id of the run the database belongs to")
    p.add_argument("--engine", choices=dbengines.ENGINE_IDS, help="database engine id (firebird, db2, cosmos are only recognised, no recipe)")
    p.add_argument("--tag", help="image tag to use instead of the one found in the project")
    p.add_argument("--accept-eula", action="store_true")
    p.add_argument("--allow-pull", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the container after the run")
    p.add_argument("--kept", action="store_true", help="remove the kept container of --engine")


def _fail(message, code):
    raise CmdError(message, code, unicode_text=True)


def _table():
    try:
        return dbengines.load()
    except dbengines.DbTableError as exc:
        _fail(str(exc), 2)


def _docker(root=None):
    try:
        return Docker.find(exclude_dirs=(root,) if root else ())
    except DockerError as exc:
        _fail(str(exc), exc.code)


def _guard(func, *args):
    try:
        return func(*args)
    except DockerError as exc:
        _fail(str(exc), exc.code)


def _entry(table, engine, need_recipe=True):
    if engine is None:
        _fail("--engine is required.", 3)
    entry = table[engine]
    if need_recipe and not entry["recipe"]:
        _fail(entry["message"], 1)
    return entry


def _tag(args, entry):
    tag = args.tag if args.tag is not None else entry["defaultTag"]
    if not dbengines.valid_tag(tag):
        _fail("--tag is not a valid image tag.", 3)
    return tag


def _run_folder(root, run_id):
    if not st.is_scan_run_id(run_id):
        _fail("Invalid run id.", 3)
    res = resolve(root, ".test-generator/runs/%s" % run_id)
    if not res.exists or not os.path.isdir(res.path):
        _fail("The run folder does not exist.", 1)
    return res.path


def _require_open_run(root, run_id):
    """A database is only started for a run that is open (a finished run never gets a container that nobody would remove)."""
    res = resolve(root, ".test-generator/runs/%s/state.json" % run_id)
    if not res.exists:
        _fail("No state.json for run " + run_id, 1)
    try:
        status = st.read_state(res.path).get("status")
    except Exception:
        _fail("state.json is not usable.", 1)
    if status != "open":
        _fail("The run is not open; no database is started for it.", 1)


# ------------------------------------------------------------------ check
def _check(args):
    table = _table()
    entry = _entry(table, args.engine, need_recipe=False)
    if not entry["recipe"]:
        emit("docker: unavailable (engine has no recipe)\n%s\n" % entry["message"])
        return 1
    tag = _tag(args, entry)
    try:
        dk = Docker.find(exclude_dirs=(project_root(args),) if args.project_root else ())
        info = dk_mod.preflight(dk)
    except DockerError as exc:
        reason = str(exc)
        reason = reason[reason.find("(") + 1:reason.rfind(")")] if "(" in reason else "daemon not reachable"
        emit("docker: unavailable (%s)\nengine: %s tag %s\n" % (reason, args.engine, tag))
        return 1
    lines = ["docker: ok", "engine: %s tag %s" % (args.engine, tag), "arch: %s" % info.arch, "docker memory: %d MB" % info.mem_mb]
    code = 0
    if info.arch not in entry["archs"]:
        lines.append("%s on %s: unsupported (emulation untested)" % (args.engine, info.arch))
        code = 1
    ref = dk_mod.image_ref(entry, tag)
    if dk_mod.image_present(dk, ref):
        lines.append("image: present")
    else:
        lines.append("image: needs pull (~%d MB)" % entry["pullSizeHintMb"])
    if entry["needsEula"]:
        lines.append("licence: accept at %s (use --accept-eula)" % entry["eulaUrl"])
    emit("\n".join(lines) + "\n")
    return code


# ------------------------------------------------------------------ up
def _set_db_used(root, run_id):
    res = resolve(root, ".test-generator/runs/%s/state.json" % run_id)
    if not res.exists:
        _fail("No state.json for run " + run_id, 1)

    def mutate(s):
        s["dbUsed"] = True
        s["updatedUtc"] = st.utc_now_iso()

    st.update_state_file(res.path, mutate)


def _up(args):
    table = _table()
    entry = _entry(table, args.engine)
    tag = _tag(args, entry)
    if args.run_id is None or not st.is_scan_run_id(args.run_id):
        _fail("Invalid run id.", 3)
    if entry["needsEula"] and not args.accept_eula:
        _fail("%s needs the licence to be accepted (%s); run again with --accept-eula after the user agreed." % (args.engine, entry["eulaUrl"]), 1)
    root = project_root(args)
    run_dir = _run_folder(root, args.run_id)
    _require_open_run(root, args.run_id)
    dk = _docker(root)
    info = _guard(dk_mod.preflight, dk)
    if info.arch not in entry["archs"]:
        _fail("%s is not supported on %s (emulation untested)." % (args.engine, info.arch), 2)
    entry = dict(entry, tag=tag)
    ref = dk_mod.image_ref(entry, tag)
    try:
        dk_mod.sweep(dk, root)
    except DockerError:
        pass                                     # the sweep is housekeeping; a failure must not stop the run
    name = dk_mod.run_name(args.run_id)
    kname = dk_mod.keep_name(args.engine, root)
    dk_mod.delete_db_json(run_dir)               # a file left from a container that is gone must never be read for the new one
    _set_db_used(root, args.run_id)              # before the container exists: a crash in between is still caught by the gate
    reused = False
    pulled = False
    created = False
    ok = False
    try:
        if _guard(dk_mod.find_reusable, dk, kname, args.engine, ref) is not None:
            reused, name = True, kname
        else:
            _guard(dk_mod.remove_container, dk, name)        # a leftover container of this very run (resume)
            if not dk_mod.image_present(dk, ref):
                if not args.allow_pull:
                    _fail("The image %s is not present locally (download about %d MB); run again with --allow-pull after the user agreed."
                          % (ref, entry["pullSizeHintMb"]), 1)
                _guard(dk_mod.pull, dk, ref)
                pulled = True
            password = dk_mod.new_password()
            created = True
            _guard(dk_mod.start, dk, entry, name, args.run_id, password, root)
        port = _guard(dk_mod.host_port, dk, name, entry)
        if not reused:
            _guard(dk_mod.wait_ready, dk, name, entry)
        data = {"engine": args.engine, "tag": tag, "name": name, "port": port, "created": int(dk_mod._time()),
                "reused": reused, "pulled": pulled}
        dk_mod.write_db_json(run_dir, data)
        ok = True
    finally:
        if created and not ok:
            try:
                dk_mod.remove_container(Docker(dk.exe), name)    # no deadline: the cleanup always runs
            except DockerError:
                pass
    emit("db: ready engine=%s tag=%s port=%d reused=%s pulled=%s\n"
         % (args.engine, tag, port, "yes" if reused else "no", "yes" if pulled else "no"))
    return 0


# ------------------------------------------------------------------ down
def _db_used(root, run_id):
    try:
        res = resolve(root, ".test-generator/runs/%s/state.json" % run_id)
        return res.exists and st.read_state(res.path).get("dbUsed") is True
    except Exception:
        return False


def down_run(root, run_id, keep=False):
    """Library: remove (or keep) the database container of a run and delete db.json. Returns a short status word:
    'none' (nothing to do), 'removed', 'kept'. Raises DockerError (container possibly still there) - db.json stays then."""
    if not st.is_scan_run_id(run_id):
        return "none"
    folder = resolve(root, ".test-generator/runs/%s" % run_id)
    if not folder.exists:
        return "none"
    run_dir = folder.path
    try:
        data = dk_mod.read_db_json(run_dir)
    except DockerError:
        data = None
    name = engine = None
    if data is not None and data["name"] in (dk_mod.run_name(run_id), dk_mod.keep_name(data["engine"], root)):
        name, engine = data["name"], data["engine"]
    elif _db_used(root, run_id) or os.path.lexists(os.path.join(run_dir, dk_mod.DB_JSON)):
        name = dk_mod.run_name(run_id)             # state says a database was used but db.json is missing or unusable
    if name is None:
        return "none"
    dk = Docker.find(exclude_dirs=(root,))
    if keep and engine is not None:
        info = dk_mod.inspect(dk, name)
        if info is not None and info.running and info.plugin == "1":
            dk_mod.rename_to_keep(dk, name, dk_mod.keep_name(engine, root))
            dk_mod.delete_db_json(run_dir)
            return "kept"
    existed = dk_mod.remove_container(dk, name)
    dk_mod.delete_db_json(run_dir)
    return "removed" if existed else "none"


def _down(args):
    root = project_root(args)
    if args.kept:
        if args.engine is None or args.run_id is not None or args.keep:
            _fail("--kept needs --engine and cannot be combined with --run-id or --keep.", 3)
        _entry(_table(), args.engine)
        dk = _docker(root)
        gone = _guard(dk_mod.remove_container, dk, dk_mod.keep_name(args.engine, root))
        emit("db: %s\n" % ("removed kept container" if gone else "no kept container"))
        return 0
    if args.run_id is None:
        _fail("--run-id is required.", 3)
    try:
        result = down_run(root, args.run_id, args.keep)
    except DockerError as exc:
        _fail(str(exc), exc.code)
    if result == "kept":
        emit("db: kept (remove later with: dtt db down --project-root <root> --engine <engine> --kept)\n")
    else:
        emit("db: %s\n" % ("nothing to do" if result == "none" else "removed"))
    return 0


# ------------------------------------------------------------------ sweep
def _sweep(args):
    root = project_root(args) if args.project_root else None
    dk = _docker(root)
    count = _guard(dk_mod.sweep, dk, root)
    emit("db: swept %d\n" % count)
    return 0


def run(args):
    if args.action == "check":
        return _check(args)
    if args.action == "up":
        return _up(args)
    if args.action == "down":
        return _down(args)
    return _sweep(args)
