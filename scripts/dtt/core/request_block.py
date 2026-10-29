"""The optional `request` block of 01-scenarios.json: strict validation, the narrow test and the one-line summary.

The request text is the USER'S words. It is data: it is validated (control and direction characters are refused, nothing is
rewritten) and never copied into hook output, error messages or the summary. Error messages are fixed sentences with at most a
short ASCII-filtered field name. The block is written by the scenario analyst, so code cannot verify that userText is verbatim."""
from __future__ import annotations

import re

from . import state as st
from .textsafe import clean_verbatim, has_bad_chars, safe_ascii

USER_TEXT_MAX = 2000

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_MEMBER_RX = re.compile(_IDENT + r"(?:\." + _IDENT + r")*")      # dotted identifiers: no empty segment, no leading / trailing / double dot
MAX_MEMBER_CHARS = 101
_NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,100}")
_DRIVE_RX = re.compile(r"[A-Za-z]:")
FIELDS = ("userText", "targets", "count", "cases", "focus", "exclude", "form", "names")
FORMS = ("separate", "grouped")
PERS = ("total", "target")


class RequestError(Exception):
    """A request block that is not acceptable. The message is a fixed sentence."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _single_line(value, label, lo, hi):
    if not isinstance(value, str):
        raise RequestError("%s must be a string." % label)
    if len(value) < lo or len(value) > hi:
        raise RequestError("%s must be %d to %d characters." % (label, lo, hi))
    if has_bad_chars(value):
        raise RequestError("%s must be a single line without control characters." % label)
    return value


def _keys(obj, allowed, label):
    if not isinstance(obj, dict):
        raise RequestError("%s must be an object." % label)
    for key in obj:
        if key not in allowed:
            raise RequestError("%s has an unknown key '%s'." % (label, safe_ascii(key, 40)))


def _list(value, label, maximum):
    if not isinstance(value, list):
        raise RequestError("%s must be a list." % label)
    if len(value) > maximum:
        raise RequestError("%s may have at most %d items." % (label, maximum))
    return value


def _member(value, label):
    if not isinstance(value, str) or len(value) > MAX_MEMBER_CHARS or _MEMBER_RX.fullmatch(value) is None:
        raise RequestError("%s must be a member name (identifiers separated by single dots, at most 101 characters)." % label)
    return value


def path_key(path):
    """Comparable form of a project-relative path (separators, '.', case, Unicode normalisation)."""
    return tuple(st._fold(p) for p in st.state_path_parts(path)) if isinstance(path, str) else ()


def _syntactic_file(value):
    value = _single_line(value, "targets[].file", 1, 300)
    if value[0] in "/\\" or _DRIVE_RX.match(value) or ".." in st.state_path_parts(value) or not st.state_path_parts(value):
        raise RequestError("targets[].file must be a project-relative path.")
    return value


def validate_block(obj, target_lookup=None):
    """Validated fields of a request block. target_lookup(file) -> the run's own spelling of the path, or None when the file is
    not a target of the run (leave it None when the scenarios file is not inside a run). Returns the fields dict; raises
    RequestError with a fixed sentence."""
    _keys(obj, FIELDS, "The request")
    fields = {}

    if "userText" not in obj:
        raise RequestError("userText is required.")
    text = obj["userText"]
    if not isinstance(text, str):
        raise RequestError("userText must be a string.")
    if len(text) > USER_TEXT_MAX:
        raise RequestError("userText is limited to %d characters." % USER_TEXT_MAX)
    if clean_verbatim(text) != text:
        raise RequestError("userText must not contain control, direction or separator characters (a line feed is allowed).")
    if not text.strip():
        raise RequestError("userText must not be empty.")
    fields["userText"] = text

    if "targets" in obj:
        targets = _list(obj["targets"], "targets", 20)
        out = []
        for t in targets:
            _keys(t, ("file", "members"), "A target")
            if "file" not in t:
                raise RequestError("A target needs a file.")
            f = _syntactic_file(t["file"])
            if target_lookup is not None:
                found = target_lookup(f)
                if found is None:
                    raise RequestError("A target file is not one of the run's targets.")
                f = found
            entry = {"file": f}
            if "members" in t:
                entry["members"] = [_member(m, "targets[].members[]") for m in _list(t["members"], "targets[].members", 20)]
            out.append(entry)
        fields["targets"] = out

    if "count" in obj:
        c = obj["count"]
        _keys(c, ("max", "per"), "count")
        if "max" not in c:
            raise RequestError("count.max is required.")
        if not _is_int(c["max"]) or c["max"] < 1 or c["max"] > 200:
            raise RequestError("count.max must be an integer from 1 to 200.")
        per = c.get("per", "total")
        if not isinstance(per, str) or per not in PERS:
            raise RequestError("count.per must be 'total' or 'target'.")
        fields["count"] = {"max": c["max"], "per": per}

    if "cases" in obj:
        out = []
        for case in _list(obj["cases"], "cases", 20):
            _keys(case, ("member", "input", "expected"), "A case")
            if "input" not in case:
                raise RequestError("A case needs an input.")
            entry = {}
            if "member" in case:
                entry["member"] = _member(case["member"], "cases[].member")
            entry["input"] = _single_line(case["input"], "cases[].input", 1, 300)
            if "expected" in case:
                entry["expected"] = _single_line(case["expected"], "cases[].expected", 1, 300)
            out.append(entry)
        fields["cases"] = out

    if "focus" in obj:
        fields["focus"] = _single_line(obj["focus"], "focus", 1, 200)
    if "exclude" in obj:
        fields["exclude"] = [_single_line(e, "exclude[]", 1, 120) for e in _list(obj["exclude"], "exclude", 10)]
    if "form" in obj:
        if not isinstance(obj["form"], str) or obj["form"] not in FORMS:
            raise RequestError("form must be 'separate' or 'grouped'.")
        fields["form"] = obj["form"]
    if "names" in obj:
        names = _list(obj["names"], "names", 20)
        for n in names:
            if not isinstance(n, str) or _NAME_RX.fullmatch(n) is None:
                raise RequestError("names[] must be C# identifiers (letters, digits, '_', at most 101 characters).")
        fields["names"] = list(names)

    if fields.get("form") == "grouped":
        _check_grouped(fields)
    return fields


def _check_grouped(fields):
    """form 'grouped' needs at least two scenarios that can share one [Theory]: two cases on the same member, or a count of at
    least 2 with exactly one named member."""
    counts = {}
    for case in fields.get("cases", []):
        counts[case.get("member")] = counts.get(case.get("member"), 0) + 1
    if counts and max(counts.values()) >= 2:
        return
    members = [m for t in fields.get("targets", []) for m in t.get("members", [])]
    entries = fields.get("targets", [])
    count = fields.get("count")
    if count is not None and count["max"] >= 2 and len(entries) == 1 and len(members) == 1:
        return
    raise RequestError("form 'grouped' needs at least two cases for the same member, or count.max of at least 2 with exactly one "
                       "member target.")


def is_narrow(fields):
    """Narrow = named members, or explicit cases, or a small count (max 10 or less)."""
    if any(t.get("members") for t in fields.get("targets") or []):
        return True
    if fields.get("cases"):
        return True
    count = fields.get("count")
    return bool(count and count["max"] <= 10)


def request_line(fields):
    """The single line printed by validate-scenarios when a request block exists: 'request: narrow|full, N scenarios max'."""
    count = fields.get("count")
    if count:
        limit = "%d scenario%s max%s" % (count["max"], "" if count["max"] == 1 else "s", " per target" if count["per"] == "target" else "")
    else:
        limit = "no scenario limit"
    return "request: %s, %s" % ("narrow" if is_narrow(fields) else "full", limit)
