"""JSON in and out with hard limits: size caps, depth cap, no duplicate keys, no NaN/Infinity, atomic writes."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time

from .paths import open_regular_read, fs_path

MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_SCENARIOS_BYTES = 1024 * 1024
MAX_STDIN_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 32
MAX_STDOUT_CHARS = 8000


class JsonError(Exception):
    """Unreadable, oversized, too deep or otherwise unacceptable JSON. The message is a fixed sentence."""


def _no_duplicates(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise JsonError("Duplicate key in JSON object.")
        seen.add(key)
    return dict(pairs)


def _reject_constant(_name):
    raise JsonError("NaN and Infinity are not allowed in JSON.")


def _parse_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise JsonError("Number out of range in JSON.")
    return value


MAX_INT_DIGITS = 18


def _parse_int(text):
    """Integers longer than 18 digits are refused BEFORE the conversion: on a Python without the interpreter's digit limit
    (3.9.6 and older patch releases) converting a 2 MB literal takes many seconds."""
    if len(text) - (1 if text.startswith("-") else 0) > MAX_INT_DIGITS:
        raise JsonError("A number in JSON has too many digits.")
    return int(text)


def _check_walk(obj, max_depth):
    """Iterative depth and string check (no recursion, so hostile nesting cannot exhaust the stack)."""
    stack = [(obj, 1)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            raise JsonError("JSON is nested too deeply.")
        if isinstance(cur, dict):
            for k, v in cur.items():
                _check_str(k)
                stack.append((v, depth + 1))
        elif isinstance(cur, list):
            for v in cur:
                stack.append((v, depth + 1))
        elif isinstance(cur, str):
            _check_str(cur)


def _check_str(s):
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise JsonError("JSON contains an invalid (lone surrogate) character.")


def loads_strict(data, max_depth: int = MAX_DEPTH):
    """Parse bytes or str. Raises JsonError for anything not acceptable."""
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise JsonError("JSON is not valid UTF-8.")
    else:
        text = data
        if text.startswith("\ufeff"):
            text = text[1:]
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant,
                         parse_float=_parse_float, parse_int=_parse_int)
    except JsonError:
        raise
    except RecursionError:
        raise JsonError("JSON is nested too deeply.")
    except (ValueError, OverflowError):
        raise JsonError("JSON is not valid.")
    _check_walk(obj, max_depth)
    return obj


def read_bytes_capped(path: str, max_bytes: int, refuse_hardlinks: bool = False) -> bytes:
    """Bytes of a regular file of at most max_bytes. refuse_hardlinks: a file with more than one hard link is refused (a link
    planted next to the file would otherwise let a user file outside the project be read as if it were ours)."""
    try:
        fd = open_regular_read(path)
    except OSError:
        raise JsonError("The file cannot be opened.")
    try:
        info = os.fstat(fd)
        if refuse_hardlinks and info.st_nlink > 1:
            raise JsonError("The file has more than one hard link.")
        size = info.st_size
        if size > max_bytes:
            raise JsonError("The file is larger than the allowed size.")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise JsonError("The file is larger than the allowed size.")
    return data


def read_json_file(path: str, max_bytes: int = MAX_STATE_BYTES, max_depth: int = MAX_DEPTH):
    return loads_strict(read_bytes_capped(path, max_bytes), max_depth)


HOOK_MAX_DEPTH = 64
# `"agent_type"` followed by a string that starts with our prefix (or with a unicode escape, which could spell it)
_OUR_MARKER = re.compile(rb'"agent_type"\s*:\s*"(?:dotnet-test-team:|\\u)')


def _depth_and_sanitise(obj):
    """Iterative: returns the maximum nesting depth and replaces lone surrogates by U+FFFD in place (no recursion)."""
    max_depth = 1
    stack = [(obj, 1)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        if isinstance(cur, dict):
            for key in list(cur.keys()):
                value = cur[key]
                clean_key = _fix_surrogates(key)
                if clean_key is not key:
                    del cur[key]
                    cur[clean_key] = value
                if isinstance(value, str):
                    cur[clean_key] = _fix_surrogates(value)
                elif isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(cur, list):
            for i, value in enumerate(cur):
                if isinstance(value, str):
                    cur[i] = _fix_surrogates(value)
                elif isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
    return max_depth


def _fix_surrogates(s):
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _last_key_wins(pairs):
    return dict(pairs)


def read_hook_payload(max_bytes: int = MAX_STDIN_BYTES):
    """Two-stage parse of the hook input. Returns (payload, flag).

    flag None            : strict parse, or relaxed parse without anything suspicious
    flag 'hostile'       : parsed only by the relaxed parser AND nested deeper than HOOK_MAX_DEPTH: deny if the agent is ours
    flag 'unparsed-ours' : not parseable at all, but the raw bytes name one of our agents: deny
    payload None, flag 'oversize': larger than max_bytes and no marker of our agents in the first max_bytes: the caller decides (guard-writes
                           denies while a run folder exists, because the agent may be named after the cut)
    payload None, flag None: empty, a terminal, not an object, or not ours (allow)

    Relaxed parse = duplicate keys (last one wins, like JavaScript JSON.parse), lone surrogates replaced, invalid UTF-8 replaced.
    It is used for classification; the guards still apply every rule to what it returns."""
    try:
        stream = sys.stdin.buffer
        if stream.isatty():
            return None, None
        data = stream.read(max_bytes + 1)
    except (OSError, ValueError, AttributeError):
        return None, None
    if not data or not data.strip():
        return None, None
    if len(data) > max_bytes:
        return None, ("unparsed-ours" if _OUR_MARKER.search(data) else "oversize")
    try:
        obj = loads_strict(data, max_depth=HOOK_MAX_DEPTH)
        return (obj, None) if isinstance(obj, dict) else (None, None)
    except JsonError:
        pass
    try:
        obj = json.loads(data.decode("utf-8-sig", errors="replace"), object_pairs_hook=_last_key_wins, parse_int=_parse_int)
    except (ValueError, RecursionError, OverflowError, JsonError):
        return None, ("unparsed-ours" if _OUR_MARKER.search(data) else None)
    if not isinstance(obj, dict):
        return None, None
    depth = _depth_and_sanitise(obj)
    return obj, ("hostile" if depth > HOOK_MAX_DEPTH else None)


def read_hook_input(max_bytes: int = MAX_STDIN_BYTES):
    """The payload of read_hook_payload (relaxed parse allowed), or None."""
    return read_hook_payload(max_bytes)[0]


def dumps(obj, indent=None) -> str:
    if indent is None:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, indent=indent)


def write_atomic(path: str, text: str, retries: int = 40, mode: int = 0o644) -> None:
    """Temp file in the same directory + fsync + os.replace (retried on Windows PermissionError)."""
    directory = os.path.dirname(os.path.abspath(path))
    tmp = "%s.%s.tmp" % (path, os.urandom(8).hex())
    data = text.encode("utf-8")
    try:
        fd = os.open(fs_path(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
        try:
            view = memoryview(data)
            while len(view):
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        delay = 0.02
        for attempt in range(retries):
            try:
                os.replace(fs_path(tmp), fs_path(path))
                break
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.6, 0.25)
        if os.name != "nt":
            try:
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    finally:
        try:
            os.unlink(fs_path(tmp))
        except OSError:
            pass


def write_json_atomic(path: str, obj) -> None:
    write_atomic(path, dumps(obj, indent=2) + "\n")


def write_stdout(text: str, max_chars: int = MAX_STDOUT_CHARS) -> None:
    """UTF-8 bytes (no BOM) to stdout whatever the console code page is."""
    if len(text) > max_chars:
        text = text[:max_chars]
    data = text.encode("utf-8", errors="replace")
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        out.write(data)
        out.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def write_stderr(text: str) -> None:
    data = (text + "\n").encode("utf-8", errors="replace")
    err = getattr(sys.stderr, "buffer", None)
    if err is not None:
        err.write(data)
        err.flush()
    else:
        sys.stderr.write(text + "\n")
