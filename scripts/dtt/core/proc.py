"""Subprocess helper: argv list only, shell=False, timeout, output cap, environment allowlist."""
from __future__ import annotations

import os
from collections import namedtuple

from .paths import fs_path

ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "COMPUTERNAME",
    "USERNAME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ",
    "DOTNET_ROOT", "DOTNET_ROOT(X86)", "DOTNET_MULTILEVEL_LOOKUP", "NUGET_PACKAGES", "NUGET_HTTP_CACHE_PATH",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
FORCED_ENV = {
    "DOTNET_CLI_UI_LANGUAGE": "en",
    "DOTNET_NOLOGO": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "VSLANG": "1033",
}
# docker client only (build_env(docker=True)); the dotnet environment above is not widened by these
DOCKER_ENV_ALLOWLIST = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
# only a unix socket path or the LOCAL Windows named pipe: npipe://host/... (a pipe on another machine) is refused
LOCAL_DOCKER_HOST_PREFIXES = ("unix:///", "npipe:////./pipe/")
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_OUTPUT = 1024 * 1024


# returncode is None when the process was killed after the timeout
ProcResult = namedtuple("ProcResult", "returncode stdout stderr timed_out truncated")


def build_env(extra: Optional[Dict[str, str]] = None, docker: bool = False) -> Dict[str, str]:
    """Allowlisted copy of the environment. docker=True is for the docker client: the base allowlist plus DOCKER_HOST (only a local
    unix:// or npipe:// value), DOCKER_CONTEXT, DOCKER_CONFIG and XDG_RUNTIME_DIR, and none of the dotnet forced variables."""
    env: Dict[str, str] = {}
    upper = {k.upper(): k for k in os.environ}
    for name in ENV_ALLOWLIST + (DOCKER_ENV_ALLOWLIST if docker else ()):
        real = upper.get(name)
        if real is not None:
            if name == "DOCKER_HOST" and not os.environ[real].startswith(LOCAL_DOCKER_HOST_PREFIXES):
                continue
            env[real] = os.environ[real]
    if not docker:
        env.update(FORCED_ENV)
    if extra:
        env.update(extra)
    return env


def _under(path: str, directory: str) -> bool:
    try:
        p = os.path.normcase(os.path.realpath(path))
        d = os.path.normcase(os.path.realpath(directory))
    except OSError:
        return False
    return p == d or p.startswith(d.rstrip("\\/") + os.sep)


def resolve_dotnet(exclude_dirs=(), env=None) -> Optional[str]:
    """Absolute path of dotnet from DOTNET_ROOT, then PATH. Relative PATH entries and entries inside the cwd or any of
    `exclude_dirs` (the project folder) are ignored, so a planted dotnet(.exe) in the project is never run."""
    env = os.environ if env is None else env
    exe = "dotnet.exe" if os.name == "nt" else "dotnet"
    banned = [os.getcwd()] + [d for d in exclude_dirs if d]
    candidates: list = []
    root = env.get("DOTNET_ROOT", "")
    if root and os.path.isabs(root):
        candidates.append(root)
    for entry in env.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and os.path.isabs(entry):
            candidates.append(entry)
    for directory in candidates:
        if any(_under(directory, b) for b in banned):
            continue
        full = os.path.join(directory, exe)
        if os.path.isfile(fs_path(full)) and (os.name == "nt" or os.access(full, os.X_OK)):
            return os.path.abspath(full)
    return None


PIPE_GRACE_SECONDS = 2.0      # after the child ended or was killed: how long the output readers may still drain
KILL_WAIT_SECONDS = 5.0


def _kill_tree(proc) -> None:
    """Kills the child and everything it started. POSIX: the child leads its own session/process group (start_new_session), so the whole
    group gets SIGKILL. Windows: `taskkill /F /T /PID` (argv list) kills the tree while the root is still alive. Never raises."""
    import subprocess
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        if os.name == "nt":
            exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            try:
                subprocess.run([exe, "/F", "/T", "/PID", str(pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, shell=False)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            import signal
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError, PermissionError):
                pass
    try:
        proc.kill()
    except (OSError, ValueError):
        pass


def run(argv: list, cwd: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
        max_output: int = DEFAULT_MAX_OUTPUT, extra_env: Optional[Dict[str, str]] = None,
        docker: bool = False) -> ProcResult:
    """Run argv (a list of str, never a string) without a shell. Output beyond max_output bytes per stream is dropped.
    The timeout is HARD: the child runs in its own process group (POSIX session / Windows process group) and on timeout the whole tree
    is killed; a grandchild that keeps the output pipes open can delay the return by PIPE_GRACE_SECONDS at most (its reader threads are
    then abandoned - daemon threads that close their own stream when the pipe ends - and the process group is killed again)."""
    import subprocess
    import threading
    import time
    if isinstance(argv, (str, bytes)) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of str")
    if "\x00" in "".join(argv):
        raise ValueError("NUL in argument")
    group = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        argv, cwd=cwd, shell=False, env=build_env(extra_env, docker),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group
    )
    buffers = {"out": bytearray(), "err": bytearray()}
    flags = {"truncated": False}

    def pump(stream, key):
        buf = buffers[key]
        try:
            while True:
                chunk = (getattr(stream, "read1", None) or stream.read)(65536)      # read1: what is there now, not 'wait for 64 KB or EOF'
                if not chunk:
                    break
                room = max_output - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                if len(chunk) > room:
                    flags["truncated"] = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()                # only the reader closes its stream: a stream a thread still reads is never closed from outside
            except (OSError, ValueError):
                pass

    threads = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def drain(seconds):
        until = time.monotonic() + seconds
        for t in threads:
            t.join(timeout=max(0.0, until - time.monotonic()))
        return [t for t in threads if t.is_alive()]

    alive = drain(PIPE_GRACE_SECONDS)
    if alive:
        _kill_tree(proc)                  # a grandchild still holds a pipe: end its group, then give the readers one more moment
        alive = drain(1.0)
    # readers that are still alive are abandoned (daemon threads) and close their stream when the pipe ends
    return ProcResult(
        None if timed_out else proc.returncode,
        bytes(buffers["out"]).decode("utf-8", errors="replace"),
        bytes(buffers["err"]).decode("utf-8", errors="replace"),
        timed_out,
        flags["truncated"],
    )
