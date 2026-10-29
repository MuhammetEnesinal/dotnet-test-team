"""Docker helper for throw-away test databases (the plugin's only Docker code).

Rules: the docker client is run with an argv list (shell=False), a timeout, an output cap and a reduced environment (proc.run(docker=True)).
Every argv value comes from the validated engine table (dbengines) or is generated here (container name, labels, ports); a value that
starts with '-' is refused. The database password never appears in an argument: it travels in the docker client's own environment
(`-e NAME` without a value) and is read back with `docker inspect` of OUR container only. Docker's own error text is never passed on
as it is: callers get fixed sentences (DockerError) and at most a short ASCII snippet."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections import namedtuple

from . import dbengines, jsonio, proc, state as st
from .paths import PathError, resolve_inside
from .textsafe import safe_ascii

NAME_RX = re.compile(r"dtt-(run|keep)-[a-z0-9][a-z0-9-]{3,80}")
_RUN_NAME_RX = re.compile(r"dtt-run-([a-z0-9][a-z0-9-]{3,80})")
_PORT_LINE_RX = re.compile(r"127\.0\.0\.1:([0-9]{1,5})")
LABEL_PLUGIN = "dtt.plugin"
STALE_AFTER_SECONDS = 3 * 3600           # a container of THIS project
FOREIGN_STALE_AFTER_SECONDS = 12 * 3600  # a container of any other project
READY_INTERVAL_SECONDS = 2.0
READY_SUCCESSES = 2
CALL_TIMEOUT = 30
RUN_TIMEOUT = 120
PULL_TIMEOUT = 900
MAX_OUTPUT = 256 * 1024
MAX_SWEEP_LINES = 500
DB_JSON = "db.json"
_DB_JSON_KEYS = {"engine", "tag", "name", "port", "created", "reused", "pulled"}
_DB_JSON_MAX_BYTES = 4096

# fixed reasons (short forms are what `dtt db check` prints)
R_NOT_FOUND = "not found"
R_DAEMON = "daemon not reachable"
R_NOT_LINUX = "not Linux containers"
R_REMOTE = "remote host refused"

_sleep = time.sleep          # replaced in tests
_monotonic = time.monotonic
_time = time.time


class DockerError(Exception):
    """A docker problem with a fixed message. code: 1 = the user can fix it by choosing differently, 2 = environment."""

    def __init__(self, message, code=2):
        Exception.__init__(self, message)
        self.code = code


Info = namedtuple("Info", "os arch mem_mb")
Inspected = namedtuple("Inspected", "running status exit_code plugin engine image id")
_ID_RX = re.compile(r"[0-9a-f]{12,64}")


# ------------------------------------------------------------------ locating docker
def resolve_docker(exclude_dirs=(), env=None):
    """Absolute path of docker(.exe) from an ABSOLUTE PATH entry that is neither the current folder nor below it nor below any
    of `exclude_dirs` (the project), like proc.resolve_dotnet. None when not found."""
    env = os.environ if env is None else env
    exe = "docker.exe" if os.name == "nt" else "docker"
    banned = [os.getcwd()] + [d for d in exclude_dirs if d]
    for entry in env.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry) or any(proc._under(entry, b) for b in banned):
            continue
        full = os.path.join(entry, exe)
        if os.path.isfile(full) and (os.name == "nt" or os.access(full, os.X_OK)):
            return os.path.abspath(full)
    return None


def docker_env(extra=None):
    """The environment the docker client gets (allowlist + local DOCKER_HOST/CONTEXT/CONFIG, XDG_RUNTIME_DIR)."""
    return proc.build_env(extra, docker=True)


def host_problem(environ=None):
    """R_REMOTE when DOCKER_HOST points anywhere but a local unix:// or npipe:// socket, else None."""
    environ = os.environ if environ is None else environ
    host = environ.get("DOCKER_HOST", "")
    if host and not host.startswith(proc.LOCAL_DOCKER_HOST_PREFIXES):
        return R_REMOTE
    return None


def _val(value):
    """A generated or table value used as an argv element: never empty, never a flag, no control characters."""
    if not isinstance(value, str) or not value or value.startswith("-") or re.search(r"[\x00-\x1f\x7f]", value):
        raise DockerError("internal error: unsafe docker argument", 2)
    return value


_HOST_CHECKED = {}          # (exe, DOCKER_HOST, DOCKER_CONTEXT) -> True once the daemon was seen to be LOCAL (per process)


class Docker(object):
    """One resolved docker client with an optional overall deadline (time.monotonic value). Before its first call in a process the
    client checks (cached) that DOCKER_HOST / the current context is a LOCAL socket or pipe; a remote daemon is refused."""

    def __init__(self, exe, deadline=None):
        self.exe = exe
        self.deadline = deadline

    @classmethod
    def find(cls, exclude_dirs=(), deadline=None):
        """exclude_dirs: the project root(s); PATH entries below them (and below CLAUDE_PROJECT_DIR and the cwd) are never used."""
        banned = tuple(d for d in exclude_dirs if d) + ((os.environ["CLAUDE_PROJECT_DIR"],) if os.environ.get("CLAUDE_PROJECT_DIR") else ())
        exe = resolve_docker(banned)
        if exe is None:
            raise DockerError("docker is not available (%s)." % R_NOT_FOUND, 2)
        return cls(exe, deadline)

    def _budget(self, timeout):
        if self.deadline is not None:
            remaining = self.deadline - _monotonic()
            if remaining <= 0.2:
                raise DockerError("docker did not answer within the time budget.", 2)
            timeout = min(timeout, remaining)
        return timeout

    def ensure_local(self):
        """Raises DockerError(R_REMOTE) unless the daemon is a local one. One `docker context inspect` per process (cached)."""
        key = (self.exe, os.environ.get("DOCKER_HOST", ""), os.environ.get("DOCKER_CONTEXT", ""))
        if _HOST_CHECKED.get(key):
            return
        if host_problem():
            raise DockerError("docker is unavailable (%s)." % R_REMOTE, 2)
        if not key[1]:
            r = self._raw(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], 10)
            if r.timed_out or r.returncode != 0:
                raise DockerError("docker is unavailable (%s)." % R_DAEMON, 2)
            if not r.stdout.strip().startswith(proc.LOCAL_DOCKER_HOST_PREFIXES):
                raise DockerError("docker is unavailable (%s)." % R_REMOTE, 2)
        _HOST_CHECKED[key] = True

    def _raw(self, args, timeout, extra_env=None, max_output=MAX_OUTPUT):
        return proc.run([self.exe] + list(args), timeout=self._budget(timeout), max_output=max_output, extra_env=extra_env, docker=True)

    def call(self, args, timeout=CALL_TIMEOUT, extra_env=None, max_output=MAX_OUTPUT):
        for a in args:
            if not isinstance(a, str):
                raise DockerError("internal error: unsafe docker argument", 2)
        self._budget(timeout)
        self.ensure_local()
        return self._raw(args, timeout, extra_env, max_output)


def snippet(result, limit=120):
    """First non-empty stderr line, ASCII only (docker's text, not a project's)."""
    for line in (result.stderr or "").splitlines():
        if line.strip():
            return safe_ascii(line.strip(), limit)
    return ""


# ------------------------------------------------------------------ preflight
def preflight(dk, environ=None):
    """Checks that docker talks to a LOCAL daemon in Linux-containers mode. Returns Info or raises DockerError (reason in the message)."""
    environ = os.environ if environ is None else environ
    if host_problem(environ):
        raise DockerError("docker is unavailable (%s)." % R_REMOTE, 2)
    dk.ensure_local()
    r = dk.call(["info", "--format", "{{.OSType}}|{{.Architecture}}|{{.MemTotal}}"], timeout=20)
    parts = r.stdout.strip().split("|")
    if r.timed_out or r.returncode != 0 or len(parts) != 3:
        raise DockerError("docker is unavailable (%s)." % R_DAEMON, 2)
    if parts[0] != "linux":
        raise DockerError("docker is unavailable (%s)." % R_NOT_LINUX, 2)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(parts[1], safe_ascii(parts[1], 20))
    try:
        mem = int(parts[2]) // (1024 * 1024)
    except ValueError:
        mem = 0
    return Info("linux", arch, mem)


# ------------------------------------------------------------------ names, images, containers
def image_ref(entry, tag):
    return "%s:%s" % (entry["image"], tag)


def run_name(run_id):
    name = "dtt-run-" + str(run_id)
    return _name(name)


def project_hash(project_root):
    """sha256 (hex) of the real project root."""
    real = os.path.normcase(os.path.realpath(project_root))
    return hashlib.sha256(real.encode("utf-8", "surrogatepass")).hexdigest()


def keep_name(engine, project_root):
    return _name("dtt-keep-%s-%s" % (engine, project_hash(project_root)[:8]))


def _name(name):
    if NAME_RX.fullmatch(name) is None:
        raise DockerError("internal error: bad container name", 2)
    return name


def image_present(dk, ref):
    r = dk.call(["image", "inspect", "--format", "{{.Id}}", _val(ref)], timeout=20)
    return r.returncode == 0 and not r.timed_out


def pull(dk, ref):
    r = dk.call(["pull", _val(ref)], timeout=PULL_TIMEOUT, max_output=64 * 1024)
    if r.timed_out or r.returncode != 0:
        detail = snippet(r)
        raise DockerError("image pull failed%s." % (": " + detail if detail else ""), 2)


_INSPECT_FORMAT = ('{{.Id}}|{{.State.Running}}|{{.State.Status}}|{{.State.ExitCode}}|{{index .Config.Labels "dtt.plugin"}}'
                   '|{{index .Config.Labels "dtt.engine"}}|{{.Config.Image}}')


def inspect(dk, name):
    """Inspected for our-named container, or None when it does not exist. Other failures raise DockerError.
    Only a CONTAINER is asked for (`--type container`): an image, volume or network of the same name is never looked at."""
    r = dk.call(["inspect", "--type", "container", "--format", _INSPECT_FORMAT, _val(_name(name))], timeout=20)
    if r.timed_out:
        raise DockerError("docker is unavailable (%s)." % R_DAEMON, 2)
    if r.returncode != 0:
        if "no such" in (r.stderr or "").lower():
            return None
        raise DockerError("docker is unavailable (%s)." % R_DAEMON, 2)
    parts = r.stdout.strip().split("|", 6)
    if len(parts) != 7 or _ID_RX.fullmatch(parts[0]) is None:
        raise DockerError("docker answered in an unexpected format.", 2)
    try:
        code = int(parts[3])
    except ValueError:
        code = -1
    return Inspected(parts[1] == "true", safe_ascii(parts[2], 20), code, parts[4], safe_ascii(parts[5], 20), parts[6], parts[0])


def is_alive(name, dk=None, root=None):
    """True when the named container of ours is running. Any docker problem counts as not alive. root: the project (never a docker
    from a PATH entry below it)."""
    try:
        dk = dk or Docker.find(exclude_dirs=(root,) if root else ())
        info = inspect(dk, name)
    except DockerError:
        return False
    return info is not None and info.running and info.plugin == "1"


def remove_container(dk, name):
    """docker rm -f -v of OUR container (name pattern and label dtt.plugin=1 checked), then verified gone. True if it existed.
    -v removes the anonymous volume of the image (postgres, mysql, mariadb) too, and the container is removed by the ID the label
    check saw, so a name swapped in between cannot redirect the removal."""
    info = inspect(dk, name)
    if info is None:
        return False
    if info.plugin != "1":
        raise DockerError("a container with the name %s exists but was not created by this plugin; it is left alone." % name, 1)
    r = dk.call(["rm", "-f", "-v", _val(info.id)], timeout=60)
    if (r.timed_out or r.returncode != 0) and inspect(dk, name) is not None:
        raise DockerError("the database container could not be removed.", 2)
    if inspect(dk, name) is not None:
        raise DockerError("the database container could not be removed.", 2)
    return True


def new_password():
    """Policy-safe for SQL Server (upper, lower, digit, 36 chars) and free of characters that need quoting."""
    return "Dtt1" + secrets.token_hex(16)


def start(dk, entry, name, run_id, password, project_root, created=None):
    """docker run -d ... Returns nothing; raises DockerError. The password is only in the docker client's environment."""
    created = int(_time()) if created is None else int(created)
    argv = ["run", "-d", "--pull", "never", "--name", _val(_name(name)),
            "--label", "dtt.plugin=1", "--label", "dtt.run=" + _val(run_id), "--label", "dtt.created=%d" % created,
            "--label", "dtt.engine=" + _val(entry["id"]), "--label", "dtt.project=" + project_hash(project_root)[:12],
            "--memory", "%dm" % entry["memoryMb"], "--pids-limit", "1024", "--security-opt", "no-new-privileges",
            "-p", "127.0.0.1::%d" % entry["port"]]
    child_env = {}
    for item in entry["env"]:
        key, value = item.split("=", 1)
        if value == dbengines.PASSWORD_PLACEHOLDER:
            argv += ["-e", _val(key)]
            child_env[key] = password
        else:
            argv += ["-e", _val(item)]
    argv.append(_val(image_ref(entry, entry["tag"])))
    r = dk.call(argv, timeout=RUN_TIMEOUT, extra_env=child_env, max_output=64 * 1024)
    if r.timed_out or r.returncode != 0:
        detail = snippet(r)
        raise DockerError("the database container could not be started%s." % (": " + detail if detail else ""), 2)


def host_port(dk, name, entry):
    r = dk.call(["port", _val(_name(name)), "%d/tcp" % entry["port"]], timeout=20)
    for line in r.stdout.splitlines():
        m = _PORT_LINE_RX.fullmatch(line.strip())
        if m and 1 <= int(m.group(1)) <= 65535:
            return int(m.group(1))
    raise DockerError("the database container has no local port.", 2)


def wait_ready(dk, name, entry):
    """Runs the table's readiness command inside the container until it succeeds READY_SUCCESSES times in a row (a real login), or
    fails with a fixed reason: container gone / exited (137 = Docker has too little memory) / timeout."""
    deadline = _monotonic() + entry["readyTimeoutS"]
    good = 0
    while True:
        # the exec argv is table-validated; its "-c" is a flag of the shell, not a value
        r = dk.call(["exec", _val(_name(name))] + list(entry["exec"]), timeout=CALL_TIMEOUT)
        if r.returncode == 0 and not r.timed_out:
            good += 1
            if good >= READY_SUCCESSES:
                return
        else:
            good = 0
            info = inspect(dk, name)
            if info is None:
                raise DockerError("the database container disappeared while starting.", 2)
            if not info.running:
                if info.exit_code == 137:
                    raise DockerError("the database container exited with 137: Docker has not enough memory (give Docker at least %d MB)."
                                      % entry["memoryMb"], 2)
                raise DockerError("the database container exited with code %d." % info.exit_code, 2)
        if _monotonic() >= deadline:
            raise DockerError("the database was not ready within %d s." % entry["readyTimeoutS"], 2)
        _sleep(READY_INTERVAL_SECONDS)


def find_reusable(dk, kname, engine, ref):
    """The running kept container of this project and engine with the same image tag, else None (a dead or different one is removed)."""
    info = inspect(dk, kname)
    if info is None:
        return None
    if info.plugin == "1" and info.engine == engine and info.running and info.image == ref:
        return info
    remove_container(dk, kname)
    return None


def rename_to_keep(dk, name, kname):
    if name == kname:
        return
    remove_container(dk, kname)
    r = dk.call(["rename", _val(_name(name)), _val(_name(kname))], timeout=20)
    if r.timed_out or r.returncode != 0:
        raise DockerError("the database container could not be kept.", 2)


# ------------------------------------------------------------------ password and connection
def read_password(dk, name, entry):
    """The container's own password variable, read with docker inspect (label dtt.plugin=1 checked). Never printed or stored."""
    info = inspect(dk, name)
    if info is None or info.plugin != "1":
        raise DockerError("the database container is not running; run dtt db up again.", 2)
    r = dk.call(["inspect", "--type", "container", "--format", "{{json .Config.Env}}", _val(info.id)], timeout=20, max_output=64 * 1024)
    if r.timed_out or r.returncode != 0:
        raise DockerError("docker is unavailable (%s)." % R_DAEMON, 2)
    try:
        items = json.loads(r.stdout)
    except ValueError:
        items = None
    prefix = entry["passwordEnv"] + "="
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str) and item.startswith(prefix) and len(item) > len(prefix):
                return item[len(prefix):]
    raise DockerError("the database container has no password variable.", 2)


def read_db_json(run_dir):
    """Validated db.json of a run folder (dict) or None when absent. Raises DockerError when it is present but invalid."""
    path = os.path.join(run_dir, DB_JSON)
    if not os.path.lexists(path):
        return None
    try:
        data = jsonio.loads_strict(jsonio.read_bytes_capped(path, _DB_JSON_MAX_BYTES, refuse_hardlinks=True), 4)
    except jsonio.JsonError:
        raise DockerError("db.json of the run is invalid.", 2)
    ok = (isinstance(data, dict) and set(data) == _DB_JSON_KEYS and data["engine"] in dbengines.RECIPE_IDS
          and dbengines.valid_tag(data["tag"]) and isinstance(data["name"], str) and NAME_RX.fullmatch(data["name"]) is not None
          and type(data["port"]) is int and 1 <= data["port"] <= 65535 and type(data["created"]) is int
          and type(data["reused"]) is bool and type(data["pulled"]) is bool)
    if not ok:
        raise DockerError("db.json of the run is invalid.", 2)
    return data


def write_db_json(run_dir, data):
    """Script-owned file; no secret in it. Owner-only permission where the platform supports it."""
    path = os.path.join(run_dir, DB_JSON)
    jsonio.write_atomic(path, jsonio.dumps(data, indent=2) + "\n", mode=0o600)
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def delete_db_json(run_dir):
    try:
        os.unlink(os.path.join(run_dir, DB_JSON))
    except OSError:
        pass


def connection_for(run_dir, table=None, dk=None, project_root=None):
    return connection_with_secrets(run_dir, table, dk, project_root)[0]


def connection_with_secrets(run_dir, table=None, dk=None, project_root=None):
    """(connection_for result, [secret strings to redact: the connection string and the password]). Environment for `dotnet test`:
    {DTT_TEST_DB_CONNECTION, DTT_TEST_DB_ENGINE}. The password comes from docker inspect of the named container; the port is read from
    docker too and must agree with db.json (else the run's database was replaced). The container must be THIS run's (dtt-run-<run id>)
    or this project's kept one (dtt-keep-<engine>-<project hash>; needs project_root). The values are for the child process only and
    must never be printed or logged."""
    data = read_db_json(run_dir)
    if data is None:
        raise DockerError("this run has no database container.", 2)
    run_id = os.path.basename(os.path.normpath(run_dir))
    allowed = []
    try:
        allowed.append(run_name(run_id))
    except DockerError:
        pass
    if project_root:
        allowed.append(keep_name(data["engine"], project_root))
    if data["name"] not in allowed:
        raise DockerError("db.json of the run is invalid.", 2)
    table = table or dbengines.load()
    entry = table[data["engine"]]
    dk = dk or Docker.find(exclude_dirs=(project_root,) if project_root else ())
    password = read_password(dk, data["name"], entry)
    if host_port(dk, data["name"], entry) != data["port"]:
        raise DockerError("db.json does not match the database container; run dtt db up again.", 2)
    conn = entry["connection"].replace("{port}", str(data["port"])).replace("{password}", password)
    return {"DTT_TEST_DB_CONNECTION": conn, "DTT_TEST_DB_ENGINE": data["engine"]}, [conn, password]


_SECRET_PAIR_RX = re.compile(r"(?i)(password|pwd)\s*=\s*[^;\s\"'<>&]*")      # < > & stop it: a trx/xml text must stay well-formed
_GENERATED_PW_RX = re.compile(r"Dtt1[0-9a-fA-F]{4,}")                        # the shape of new_password()
_ESC_RX = re.compile(r"\\u([0-9a-fA-F]{4})|&#([xX][0-9a-fA-F]{1,6}|[0-9]{1,7});")
_TOKEN_RX = re.compile(r"\\u([0-9a-fA-F]{4})|&#([xX][0-9a-fA-F]{1,6}|[0-9]{1,7});|(\s)|(.)", re.DOTALL)
MIN_PIECE = 8


def _codepoint(hex4, ref):
    try:
        code = int(hex4, 16) if hex4 is not None else (int(ref[1:], 16) if ref[0] in "xX" else int(ref))
    except ValueError:
        return ""
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else ""


def _lower1(ch):
    low = ch.lower()
    return low if len(low) == 1 else ch


def _is_password_like(secret):
    """A single token (the generated password), not a connection string or sentence."""
    return len(secret) >= MIN_PIECE and re.fullmatch(r"[^\s;=]+", secret) is not None


def _redact_pieces(text, passwords):
    """Removes every run of MIN_PIECE or more characters of a password, even when the text broke it up: whitespace and line breaks
    inside it are ignored, backslash-u escapes and &#N; / &#xH; references are decoded, case is ignored."""
    grams = set()
    for pw in passwords:
        low = "".join(_lower1(c) for c in pw if not c.isspace())
        for i in range(len(low) - MIN_PIECE + 1):
            grams.add(low[i:i + MIN_PIECE])
    if not grams:
        return text
    # fast test at C speed: is there any piece at all?
    compact = _ESC_RX.sub(lambda m: _codepoint(m.group(1), m.group(2)), text)
    compact = re.sub(r"\s+", "", compact).lower()
    if not any(g in compact for g in grams):
        return text
    chars, spans = [], []
    for m in _TOKEN_RX.finditer(text):
        if m.group(3) is not None:
            continue
        ch = m.group(4) if m.group(4) is not None else _codepoint(m.group(1), m.group(2))
        if ch:
            chars.append(_lower1(ch))
            spans.append((m.start(), m.end()))
    norm = "".join(chars)
    marked = bytearray(len(norm))
    for g in grams:
        pos = norm.find(g)
        while pos >= 0:
            marked[pos:pos + MIN_PIECE] = b"\x01" * MIN_PIECE
            pos = norm.find(g, pos + 1)
    out, last, i, n = [], 0, 0, len(norm)
    while i < n:
        if marked[i]:
            j = i
            while j + 1 < n and marked[j + 1]:
                j += 1
            out.append(text[last:spans[i][0]])
            out.append("[redacted]")
            last = spans[j][1]
            i = j + 1
        else:
            i += 1
    out.append(text[last:])
    return "".join(out)


def redact(text, secrets=()):
    """Text that may be shown or stored: every given secret (the connection string, the password), any piece of the password of
    8 or more characters (also broken by a line break or written as a backslash-u escape or &#N;), the generated-password shape
    (Dtt1 + hex) and any `Password=...` / `Pwd=...` pair is replaced by [redacted]. Run `dotnet test` output through this before
    printing or writing it."""
    text = text if isinstance(text, str) else str(text)
    secrets = [x for x in secrets if isinstance(x, str) and x]
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = _redact_pieces(text, [x for x in secrets if _is_password_like(x)])
    text = _SECRET_PAIR_RX.sub(lambda m: m.group(1) + "=[redacted]", text)
    return _GENERATED_PW_RX.sub("[redacted]", text)


# ------------------------------------------------------------------ sweep
def _run_state(project_root, run_id):
    """'gone' (no run folder), 'closed' (done, aborted, escalated), or 'open' (also: unreadable - then nothing is removed)."""
    try:
        folder = resolve_inside(project_root, ".test-generator/runs/%s" % run_id)
        if not folder.exists:
            return "gone"
        res = resolve_inside(project_root, ".test-generator/runs/%s/state.json" % run_id)
        if not res.exists:
            return "open"
        status = st.read_state(res.path).get("status")
    except (PathError, jsonio.JsonError, OSError, ValueError):
        return "open"
    return "closed" if status in ("done", "aborted", "escalated") else "open"


def sweep(dk, project_root=None, now=None):
    """Removes our stale run containers: label dtt.plugin=1 AND name dtt-run-<id> (label dtt.run agrees) AND either
    (label dtt.project is this project's, and its run is closed or gone or the container is older than 3 h) OR (any project, older than
    12 h by dtt.created). Never dtt-keep-*, never anything else. Returns the number removed; a container that cannot be removed is skipped."""
    now = _time() if now is None else now
    mine = project_hash(project_root)[:12] if project_root else None
    r = dk.call(["ps", "-a", "--filter", "label=dtt.plugin=1", "--format",
                 '{{.Names}}|{{.Label "dtt.created"}}|{{.Label "dtt.run"}}|{{.Label "dtt.project"}}'], timeout=20)
    if r.timed_out or r.returncode != 0:
        raise DockerError("docker is unavailable (%s)." % R_DAEMON, 2)
    removed = 0
    for line in r.stdout.splitlines()[:MAX_SWEEP_LINES]:
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        name, created, run, project = parts
        m = _RUN_NAME_RX.fullmatch(name)
        if m is None or run != m.group(1):
            continue
        age = now - int(created) if created.isdigit() and len(created) <= 12 else None
        stale = age is not None and age > FOREIGN_STALE_AFTER_SECONDS
        if not stale and mine is not None and project == mine:
            stale = (age is not None and age > STALE_AFTER_SECONDS) or (st.is_scan_run_id(run) and _run_state(project_root, run) != "open")
        if stale:
            try:
                if remove_container(dk, name):
                    removed += 1
                if mine is not None and project == mine:
                    _forget_db_json(project_root, run, name)
            except DockerError:
                continue
    return removed


def _forget_db_json(project_root, run_id, name):
    """The container of this project's run is gone: its db.json (port of a container that no longer exists) goes too, so a later
    container with the same name can never be reached through the stale port. Only when db.json names exactly this container."""
    try:
        run_dir = resolve_inside(project_root, ".test-generator/runs/%s" % run_id)
        if not run_dir.exists:
            return
        try:
            data = read_db_json(run_dir.path)
        except DockerError:
            data = {"name": name}                    # unusable file: it describes nothing valid, remove it as well
        if data is not None and data["name"] == name:
            delete_db_json(run_dir.path)
    except (PathError, OSError):
        pass
