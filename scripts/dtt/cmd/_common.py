"""Helpers shared by the CLI commands.

CLI exit codes: 0 ok, 1 finding / validation failure, 2 environment blocker, 3 launcher / usage error."""
from __future__ import annotations

import os
import sys
import unicodedata

from ..core import jsonio, paths
from ..core.textsafe import clean_text, safe_ascii

MAX_OUTPUT_CHARS = 4 * 1024 * 1024


class CmdError(Exception):
    """A command-level failure with a fixed message and an exit code (default 1)."""

    def __init__(self, message, code=1, unicode_text=False):
        Exception.__init__(self, message)
        self.code = code
        self.unicode_text = unicode_text      # the message shows a path / file name: keep letters (clean_text), not the ASCII-only filter


def project_root(args):
    try:
        return paths.get_project_root(getattr(args, "project_root", None))
    except paths.PathError as exc:
        raise CmdError(str(exc), 1)


def emit(text):
    """UTF-8 bytes to stdout (no BOM, no console code page)."""
    jsonio.write_stdout(text, MAX_OUTPUT_CHARS)


def emit_json(obj):
    emit(jsonio.dumps(obj) + "\n")


def resolve(root, candidate):
    try:
        return paths.resolve_inside(root, candidate)
    except paths.PathError as exc:
        raise CmdError(str(exc), 1)


def safe_msg(exc, limit=200):
    if getattr(exc, "unicode_text", False):
        return clean_text(str(exc), limit)
    return safe_ascii(str(exc), limit)


def target_missing_message(root, parts, shown):
    """'Target ... does not exist', plus a hint when the same name exists in another Unicode normalisation form (NFC / NFD)."""
    base = "does not exist: " + clean_text(shown, 100)
    try:
        cur = root
        for i, part in enumerate(parts):
            exact = os.path.join(cur, part)
            if os.path.lexists(paths.fs_path(exact)):
                cur = exact
                continue
            want = unicodedata.normalize("NFC", part)
            for entry in os.listdir(paths.fs_path(cur)):
                if entry != part and unicodedata.normalize("NFC", entry) == want:
                    return base + "; a file with the same name in another Unicode normalisation exists: " + clean_text(entry, 100)
            return base
    except OSError:
        pass
    return base


def run_command(name, func, args):
    """Calls func(args) and maps well-known exceptions to exit codes. func returns an int exit code (None = 0)."""
    try:
        code = func(args)
        return 0 if code is None else int(code)
    except CmdError as exc:
        sys.stderr.write("dtt %s: %s\n" % (name, safe_msg(exc)))
        return exc.code
    except (paths.PathError, jsonio.JsonError) as exc:
        sys.stderr.write("dtt %s: %s\n" % (name, safe_msg(exc)))
        return 1
    except OSError as exc:
        sys.stderr.write("dtt %s: file system error (%s)\n" % (name, type(exc).__name__))
        return 1
    except Exception as exc:  # a bug must not print a traceback into the model's context
        sys.stderr.write("dtt %s: internal error (%s)%s" % (name, type(exc).__name__, chr(10)))
        return 1
