"""Path confinement by filesystem identity.

resolve_inside(root, candidate) answers one question: does `candidate` name something at or below `root`, reached
without crossing a symlink or Windows reparse point? It never decides by string prefix: the root is located inside
the candidate with samefile semantics, so case-insensitive volumes, 8.3 names and a root that itself lives below a
symlink (macOS /var -> /private/var) all behave. Every component below the root is lstat'ed; links are refused.
A '..' is allowed only while walking existing directories inside the root; after the first missing component the
unresolved tail must not contain '..'. Windows-only filename rules (colon, trailing dot/space, device names, 8.3 names) apply in
Windows mode to every component; in Unix mode they are off (those are ordinary names there) unless the caller asks for
new_name_rules=True, which applies the non-8.3 rules to names that do not exist yet (guard-writes does, so an agent cannot create
files that break a Windows checkout). TOCTOU between this check and the later use remains
(see README, Safety).
"""
from __future__ import annotations

import os
import re
import stat
from collections import namedtuple

from .platform import os_family


class PathError(Exception):
    """The path is not acceptable. The message is a fixed sentence and never contains the input."""


class Resolved(namedtuple("Resolved", "path parts root exists")):
    """path: root joined with parts; parts: components below the root (no '', '.', '..'); exists: the whole path exists."""
    __slots__ = ()

    @property
    def rel(self) -> str:
        return "/".join(self.parts)


_CONTROL_RX = re.compile("[\x00-\x1f\x7f]")
_DEVICE_RX = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3]|CONIN\$|CONOUT\$|CLOCK\$)(?:\..*)?\Z",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)
_SHORT_NAME_RX = re.compile(r"~[0-9]")
_DRIVE_RX = re.compile(r"^([A-Za-z]):(.*)\Z", re.DOTALL)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REPARSE_NAME_SURROGATE = 0x20000000
_REPARSE_LINK_TAGS = frozenset((0xA000000C, 0xA0000003, 0x8000001B, 0xA000001D, 0x80000023))


def is_windows_mode(windows: Optional[bool] = None) -> bool:
    if windows is not None:
        return bool(windows)
    return os_family() == "windows"


def fs_path(path: str) -> str:
    """Path to hand to the OS. Long Windows paths get the long-path prefix (only for normalised drive paths)."""
    if os.name == "nt" and len(path) >= 240 and not path.startswith("\\\\") and _DRIVE_RX.match(path):
        return "\\\\?\\" + path.replace("/", "\\")
    return path


def is_link_stat(st) -> bool:
    """Symlink, junction, mount point, app-exec link... A reparse point without a link tag (OneDrive placeholder) is a file."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", 0)
        if tag == 0:
            return True  # unknown reparse point: fail closed
        return tag in _REPARSE_LINK_TAGS or bool(tag & _REPARSE_NAME_SURROGATE)
    return False


def windows_name_problem(name: str, short_names: bool) -> Optional[str]:
    if ":" in name:
        return "alternate data streams and drive-relative paths are not allowed."
    if name.endswith(".") or name.endswith(" "):
        return "path segments must not end with a dot or space."
    if _DEVICE_RX.match(name):
        return "reserved device names are not allowed."
    if short_names and _SHORT_NAME_RX.search(name):
        return "short (8.3) names are not allowed; use the long name."
    return None


def _same_dir(a: str, b: str) -> bool:
    try:
        sa, sb = os.stat(fs_path(a)), os.stat(fs_path(b))
    except OSError:
        return False
    if not stat.S_ISDIR(sa.st_mode) or not stat.S_ISDIR(sb.st_mode):
        return False
    if sa.st_ino != 0 and sb.st_ino != 0:
        return sa.st_ino == sb.st_ino and sa.st_dev == sb.st_dev
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def canonical_path(path: str, windows: Optional[bool] = None) -> str:
    """Canonical path for a trusted but validated location such as the project root."""
    if not isinstance(path, str) or _CONTROL_RX.search(path):
        raise PathError("Path contains control characters.")
    win = is_windows_mode(windows)
    if win and (path.startswith("\\\\") or path.startswith("//")):
        raise PathError("UNC and device paths are not allowed.")
    full = os.path.abspath(path)
    if win:
        drive, rest = os.path.splitdrive(full)
        if ":" in rest:
            raise PathError("Alternate data streams are not allowed.")
        for seg in re.split(r"[\\/]", rest):
            if seg and _DEVICE_RX.match(seg):
                raise PathError("Reserved device names are not allowed.")
    return full


def strip_trailing_seps(path: str) -> str:
    stripped = path.rstrip("\\/")
    if not stripped or (len(stripped) == 2 and stripped[1] == ":"):
        return path  # filesystem root such as "/" or "C:\"
    return stripped


def get_project_root(candidate: Optional[str] = None, windows: Optional[bool] = None) -> str:
    explicit = bool(candidate and candidate.strip())
    if not explicit:
        candidate = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not candidate or not candidate.strip():
        candidate = os.getcwd()
    full = strip_trailing_seps(canonical_path(candidate, windows))
    if not os.path.isdir(fs_path(full)):
        raise PathError("Project root does not exist.")
    env_root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if explicit and env_root.strip():
        env_full = strip_trailing_seps(canonical_path(env_root, windows))
        try:
            resolve_inside(env_full, full, windows=windows, allow_links=True)
        except PathError:
            raise PathError("The project root must be the Claude Code project directory or a folder below it.")
    return full


def session_start_dir() -> str:
    """The folder Claude Code was started in: CLAUDE_PROJECT_DIR when set, otherwise the current working directory."""
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    return env_dir if env_dir.strip() else os.getcwd()


def is_session_root(root: str) -> bool:
    """True when `root` is (the same directory as) the folder Claude Code was started in (not a folder below or above it)."""
    try:
        return _same_dir(root, canonical_path(session_start_dir()))
    except PathError:
        return False


def _split(path: str, win: bool):
    return re.split(r"[\\/]", path) if win else path.split("/")


def resolve_inside(root: str, candidate: str, *, windows: Optional[bool] = None,
                   allow_links: bool = False, check_names: bool = True, new_name_rules: bool = False) -> Resolved:
    """See the module docstring. `root` is trusted (an existing directory); `candidate` is not.

    allow_links=True is for read-only classification (for example "is this the plugin's own state file?"); it skips the
    link and file-name rules but still requires the result to be inside the root."""
    if not isinstance(candidate, str) or candidate == "":
        raise PathError("The path is empty.")
    if _CONTROL_RX.search(candidate):
        raise PathError("Path contains control characters.")
    win = is_windows_mode(windows)
    root_abs = os.path.abspath(root)
    if win and (candidate.startswith("\\\\") or candidate.startswith("//")):
        raise PathError("UNC and device paths are not allowed.")

    absolute = False
    base = ""
    rest = candidate
    if win:
        m = _DRIVE_RX.match(candidate)
        if m:
            if m.group(2)[:1] not in ("\\", "/"):
                raise PathError("Drive-relative paths are not allowed.")
            absolute, base, rest = True, m.group(1) + ":" + os.sep, m.group(2)
        elif candidate[:1] in ("\\", "/"):
            # Ambiguous: Windows reads it as <current drive>:\x, Git Bash maps /x into its own MSYS root. Never guess.
            raise PathError("Rooted paths without a drive letter are not allowed.")
    elif candidate.startswith("/"):
        absolute, base = True, "/"

    comps = [c for c in _split(rest, win) if c not in ("", ".")]

    if absolute:
        anchor = -1
        for k in range(len(comps) + 1):
            prefix = base + os.sep.join(comps[:k]) if k else base
            if _same_dir(prefix, root_abs):
                anchor = k
                break
        if anchor < 0:
            raise PathError("Path is outside the project root.")
        comps = comps[anchor:]

    parts = []
    unresolved = False
    for comp in comps:
        if comp == "..":
            if unresolved:
                raise PathError("A '..' after a missing path component is not allowed.")
            if not parts:
                raise PathError("Path is outside the project root.")
            parts.pop()
            continue
        parts.append(comp)
        current = os.path.join(root_abs, *parts)
        st = None
        if not unresolved:
            try:
                st = os.lstat(fs_path(current))
            except (FileNotFoundError, NotADirectoryError):
                st = None
            except OSError:
                raise PathError("A path component cannot be inspected.")
            if st is None:
                unresolved = True
        if not allow_links and check_names:
            if win:
                problem = windows_name_problem(comp, short_names=True)
            elif unresolved and new_name_rules:
                problem = windows_name_problem(comp, short_names=False)
            else:
                problem = None
            if problem:
                raise PathError(problem[0].upper() + problem[1:])
        if st is not None and not allow_links and is_link_stat(st):
            raise PathError("Path crosses a symlink or junction.")

    full = os.path.join(root_abs, *parts) if parts else root_abs
    return Resolved(full, tuple(parts), root_abs, not unresolved)


def open_nofollow(path: str, flags: int, mode: int = 0o666) -> int:
    """os.open that refuses a symlink as the last component where the OS supports it (O_NOFOLLOW)."""
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(fs_path(path), flags, mode)


def open_regular_read(path: str) -> int:
    """Read-only descriptor of a REGULAR file. Never blocks on a FIFO (O_NONBLOCK), never follows a final symlink (O_NOFOLLOW),
    and refuses anything that is not a regular file (FIFO, device, socket, directory) after opening."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(fs_path(path), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd
