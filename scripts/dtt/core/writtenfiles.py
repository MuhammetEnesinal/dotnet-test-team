"""written-files.txt: the test files the test-writer was ALLOWED to write in a run (script-owned: only guard-writes appends, no agent
and not the main conversation may write it). `dtt run` lints exactly these files and never opens any other .cs file.

One project-relative path per line (forward slashes), deduplicated, at most MAX_ENTRIES lines of at most MAX_PATH_CHARS characters.
Recording is best effort: it never raises and never turns an allow into a deny."""
from __future__ import annotations

import os

from . import jsonio
from .paths import PathError, resolve_inside, fs_path

FILE_NAME = "written-files.txt"
MAX_ENTRIES = 500
MAX_PATH_CHARS = 300
_LINE_BREAKS = "".join(chr(c) for c in (0x2028, 0x2029, 0x85))
MAX_FILE_BYTES = MAX_ENTRIES * (MAX_PATH_CHARS * 4 + 2)


def _key(rel):
    return rel.casefold() if os.name == "nt" else rel


def _clean_line(rel):
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_CHARS:
        return False
    return not any(ord(c) < 32 or ord(c) == 127 or c in _LINE_BREAKS for c in rel)


def load(run_dir):
    """Valid recorded paths (list of str, deduplicated, at most MAX_ENTRIES). A missing or unreadable file gives []."""
    try:
        data = jsonio.read_bytes_capped(os.path.join(run_dir, FILE_NAME), MAX_FILE_BYTES, refuse_hardlinks=True)
    except (jsonio.JsonError, OSError):
        return []
    seen = set()
    out = []
    for line in data.decode("utf-8", errors="replace").split("\n"):
        line = line.rstrip("\r")
        if not _clean_line(line) or _key(line) in seen:
            continue
        seen.add(_key(line))
        out.append(line)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def record(project_root, run_id, rel):
    """Append `rel` (project-relative, forward slashes) to the list of the run. Returns True when a line was added."""
    try:
        if not _clean_line(rel):
            return False
        target = resolve_inside(project_root, ".test-generator/runs/%s/%s" % (run_id, FILE_NAME))
        run_dir = os.path.dirname(target.path)
        if not os.path.isdir(fs_path(run_dir)):
            return False
        existing = load(run_dir) if target.exists else []
        if len(existing) >= MAX_ENTRIES or _key(rel) in {_key(e) for e in existing}:
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(fs_path(target.path), flags, 0o600)
        try:
            os.write(fd, ("\n" if _needs_separator(fd) else "").encode("ascii") + rel.encode("utf-8") + b"\n")
        finally:
            os.close(fd)
        return True
    except (PathError, OSError, ValueError):
        return False


def _needs_separator(fd):
    """True when the file does not end in a newline (someone else wrote it): the new entry must start on its own line."""
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return False
        with open(fd, "rb", closefd=False) as f:
            f.seek(size - 1)
            return f.read(1) != b"\n"
    except (OSError, ValueError):
        return False
