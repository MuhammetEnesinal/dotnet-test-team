"""validate-scenarios: validates scenario-analyst output with rules that mirror schemas/scenarios.schema.json (the file itself is not read) and against the real source files.

Checks structure, enums, length bounds, unique ids, that every cited file exists inside the project root and that every cited line
number exists. Optional scenario fields (origin, group, conflict, existingTest) and the optional top-level scope are checked when
present. The optional top-level `request` block (the user's request: userText, targets, count, cases, focus, exclude, form, names)
is validated with core/request_block.py and, when present, the scenarios are also checked against it (count cap, user cases,
expected basis of user scenarios, group size and members, grouped form; target files must be targets of the run when the file is
a run's 01-scenarios.json). Never fails on malformed input: it reports errors instead.
Prints {valid, scenarioCount, errors[]} plus, only when a valid request block exists, {narrow, request: "request: narrow|full, N
scenarios max"}. Exit 0 = valid, 1 = invalid."""
from __future__ import annotations

import os
import re

from ..core import jsonio, request_block as request_mod, state as st
from ..core.paths import PathError, fs_path, resolve_inside
from ..core.textsafe import decode_source, has_bad_chars, safe_ascii
from ._common import emit_json, project_root

CONSTRUCT_TYPES = ("branch", "constant", "throw", "null-conditional", "switch-arm", "loop", "dependency-call", "await", "signature", "other")
BASES = ("name", "xml-doc", "usage", "contract", "user-stated", "unknown")
KINDS = ("normal", "boundary", "exception", "dependency", "async", "state", "ambiguous")
SCENARIO_PROPS = ("id", "member", "construct", "input", "expected", "expectedBasis", "rationale", "kind")
OPTIONAL_SCENARIO_PROPS = ("origin", "group", "conflict", "existingTest")
ORIGINS = ("user", "derived")
SCOPE_PROPS = ("requested", "produced", "leftOut")
CONSTRUCT_PROPS = ("type", "file", "line")
MAX_SOURCE_BYTES = 20 * 1024 * 1024
_ID_RX = re.compile(r"S[0-9]{3}")
_GROUP_RX = re.compile(r"G[0-9]{1,2}")
_NEWLINE = re.compile(r"\r\n|\r|\n")


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--scenarios-path", required=True)


def text_ok(value, maximum):
    """Single-line printable text of 1..maximum characters (code points)."""
    if not isinstance(value, str):
        return False
    if len(value) < 1 or len(value) > maximum:
        return False
    return not has_bad_chars(value)


def count_lines(path):
    """Number of lines like File.ReadAllLines: line terminators are CR, LF, CRLF; a final terminator does not add a line."""
    data = jsonio.read_bytes_capped(path, MAX_SOURCE_BYTES)
    text = decode_source(data)         # UTF-8 / UTF-16 / UTF-32 BOMs, cp1252 fallback; line breaks normalised
    if text == "":
        return 0
    parts = _NEWLINE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return len(parts)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_scope(scope, err):
    if not isinstance(scope, dict):
        err("scope must be an object.")
        return
    for p in scope:
        if p not in SCOPE_PROPS:
            err("Unknown scope property '%s'." % safe_ascii(p, 40))
    if "requested" not in scope or not text_ok(scope["requested"], 200):
        err("scope.requested must be a single-line string of 1-200 characters.")
    if "produced" not in scope or not _is_int(scope["produced"]) or scope["produced"] < 0 or scope["produced"] > 200:
        err("scope.produced must be an integer from 0 to 200.")
    if "leftOut" in scope:
        left = scope["leftOut"]
        if not isinstance(left, list) or len(left) > 10:
            err("scope.leftOut must be a list of at most 10 strings.")
        elif not all(text_ok(x, 100) for x in left):
            err("scope.leftOut items must be single-line strings of 1-100 characters.")


def _run_state_path(root, parts):
    """state.json of the run when `parts` are exactly .test-generator/runs/<id>/01-scenarios.json, else None."""
    if len(parts) == 4 and str(parts[0]).casefold() == ".test-generator" and str(parts[1]).casefold() == "runs" \
            and str(parts[3]).casefold() == "01-scenarios.json" and st.is_run_id(parts[2]):
        try:
            return resolve_inside(root, ".test-generator/runs/%s/state.json" % parts[2]).path
        except PathError:
            return None
    return None


def _state_targets(state_path):
    """Paths of the run's targets, or None when state.json cannot be read."""
    try:
        obj = st.read_state(state_path)
    except (jsonio.JsonError, OSError, ValueError):
        return None
    raw = obj.get("targets") if isinstance(obj, dict) else None
    return [t["path"] for t in (raw if isinstance(raw, list) else []) if isinstance(t, dict) and isinstance(t.get("path"), str)]


def check_request(request, infos, err, run_targets=None):
    """Extra checks against the request block. infos: one tuple per scenario (sid, file key, origin, basis, kind, group, member).
    run_targets: the run's target paths, or None when the scenarios file is not a run's (then the per-target count uses the
    request's own targets)."""
    count = request.get("count")
    if count is not None:
        if count["per"] == "target":
            # the targets of the request (or, without any, of the run); a scenario citing another file counts only toward the total
            names = [t["file"] for t in (request.get("targets") or [])] or list(run_targets or [])
            keys = set(request_mod.path_key(n) for n in names)
            allowed = count["max"] * max(1, len(keys))
            per = {}
            for info in infos:
                if info[1] in keys:
                    per[info[1]] = per.get(info[1], 0) + 1
            worst = max(per.values()) if per else 0
            if worst > count["max"]:
                err("count: %d scenarios for one target exceed the requested maximum of %d per target." % (worst, count["max"]))
            if len(infos) > allowed:
                err("count: %d scenarios exceed the requested maximum of %d per target for %d target%s (%d in total)."
                    % (len(infos), count["max"], max(1, len(keys)), "" if len(keys) <= 1 else "s", allowed))
        elif len(infos) > count["max"]:
            err("count: %d scenarios exceed the requested maximum of %d." % (len(infos), count["max"]))
    users = sum(1 for info in infos if info[2] == "user")
    cases = len(request.get("cases") or [])
    if users > cases:
        err("origin: %d scenarios have origin 'user' but the request has %d cases." % (users, cases))
    elif cases and users < cases:
        err("origin: the request has %d cases but only %d scenarios have origin 'user'; every case needs exactly one scenario." % (cases, users))
    groups = {}
    for sid, _file, origin, basis, kind, group, member in infos:
        if origin == "user" and basis != "user-stated" and kind != "ambiguous":
            err("[%s] a scenario with origin 'user' needs expectedBasis 'user-stated' (or kind 'ambiguous')." % sid)
        if group is not None:
            groups.setdefault(group, []).append(member)
    for gid in sorted(groups):
        members = groups[gid]
        if len(members) < 2 or len(members) > 50:
            err("group %s: %d scenarios; a group needs 2 to 50 scenarios." % (gid, len(members)))
        if len(set(members)) > 1:
            err("group %s: scenarios of different members are mixed." % gid)
    if request.get("form") == "grouped" and not groups:
        err("form: the request asks for a grouped test but no scenario has a group.")
    if request.get("form") == "separate" and groups:
        err("form: the request asks for separate tests but %d scenario group%s exist." % (len(groups), "" if len(groups) == 1 else "s"))


def validate(root, scenarios_path):
    """(errors, scenario count). See validate_ex for the validated request block."""
    errors, count, _request = validate_ex(root, scenarios_path)
    return errors, count


def validate_ex(root, scenarios_path):
    """(errors, scenario count, validated request block or None)."""
    errors = []
    count = 0
    request = None
    run_targets = None

    def err(message):
        errors.append(message)

    scenarios_parts = ()
    try:
        scenarios_res = resolve_inside(root, scenarios_path)
        path = scenarios_res.path
        scenarios_parts = scenarios_res.parts
        doc = jsonio.read_json_file(path, jsonio.MAX_SCENARIOS_BYTES)
        if not isinstance(doc, dict):
            raise ValueError("The document must be a JSON object.")
        for p in doc:
            if p not in ("target", "scenarios", "scope", "request"):
                err("Unknown top-level property '%s'." % safe_ascii(p, 40))
        if "target" not in doc or not text_ok(doc["target"], 300):
            err("target must be a single-line string of 1-300 characters.")
        if "scenarios" not in doc:
            raise ValueError("scenarios is missing.")
        scenarios = doc["scenarios"]
        if not isinstance(scenarios, list):
            scenarios = [scenarios]
        if "scope" in doc:
            check_scope(doc["scope"], err)
        if "request" in doc:
            state_path = _run_state_path(root, scenarios_parts)
            if state_path is not None:
                run_targets = _state_targets(state_path)

            def lookup(file):
                if run_targets is None:
                    return None
                for t in run_targets:
                    if st.same_state_path(file, t):
                        return st.to_state_path(t)
                return None

            try:
                request = request_mod.validate_block(doc["request"], lookup if state_path is not None else None)
            except request_mod.RequestError as exc:
                err("request: " + str(exc))
        count = len(scenarios)
        if count < 1 or count > 200:
            err("scenarios must contain between 1 and 200 items.")
        line_cache = {}
        seen = set()
        infos = []
        for s in scenarios:
            if not isinstance(s, dict):
                err("A scenario is not a JSON object.")
                continue
            raw_id = s["id"] if isinstance(s.get("id"), str) else "(no id)"
            sid = safe_ascii(raw_id, 40)        # ids come from the model / project: never echoed raw into the messages
            if not _ID_RX.fullmatch(raw_id):
                err("[%s] id must match S000." % sid)
            elif raw_id in seen:
                err("[%s] duplicate id." % sid)
            else:
                seen.add(raw_id)
            for req in SCENARIO_PROPS:
                if req not in s:
                    err("[%s] missing '%s'." % (sid, req))
            for p in s:
                if p not in SCENARIO_PROPS and p not in OPTIONAL_SCENARIO_PROPS:
                    err("[%s] unknown property '%s'." % (sid, safe_ascii(p, 40)))
            if "origin" in s and not (isinstance(s["origin"], str) and s["origin"] in ORIGINS):
                err("[%s] origin must be 'user' or 'derived'." % sid)
            if "group" in s and not (isinstance(s["group"], str) and _GROUP_RX.fullmatch(s["group"])):
                err("[%s] group must match G0 to G99." % sid)
            if "conflict" in s and not text_ok(s["conflict"], 300):
                err("[%s] conflict must be a single-line string of 1-300 characters." % sid)
            if "existingTest" in s and not text_ok(s["existingTest"], 200):
                err("[%s] existingTest must be a single-line string of 1-200 characters." % sid)
            if "member" in s and not text_ok(s["member"], 200):
                err("[%s] member must be a single-line string of 1-200 characters." % sid)
            for f in ("input", "expected", "rationale"):
                if f in s and not text_ok(s[f], 300):
                    err("[%s] %s must be a single-line string of 1-300 characters." % (sid, f))
            kind = None
            if "kind" in s:
                kind = s["kind"] if isinstance(s["kind"], str) else str(s["kind"])
                if kind not in KINDS:
                    err("[%s] kind '%s' is not allowed." % (sid, safe_ascii(kind, 40)))
            if "expectedBasis" in s:
                basis = s["expectedBasis"] if isinstance(s["expectedBasis"], str) else str(s["expectedBasis"])
                if basis not in BASES:
                    err("[%s] expectedBasis '%s' is not allowed." % (sid, safe_ascii(basis, 40)))
                elif basis == "unknown" and kind != "ambiguous":
                    err("[%s] expectedBasis 'unknown' is only allowed for kind 'ambiguous'." % sid)
            if "request" in doc:
                cons = s.get("construct")
                cfile = cons.get("file") if isinstance(cons, dict) else None
                infos.append((sid, request_mod.path_key(cfile),
                              s["origin"] if isinstance(s.get("origin"), str) else None,
                              s.get("expectedBasis") if isinstance(s.get("expectedBasis"), str) else None, kind,
                              s["group"] if isinstance(s.get("group"), str) and _GROUP_RX.fullmatch(s["group"]) else None,
                              s.get("member") if isinstance(s.get("member"), str) else None))
            if "construct" not in s:
                continue
            c = s["construct"]
            if not isinstance(c, dict):
                err("[%s] construct must be an object." % sid)
                continue
            for p in c:
                if p not in CONSTRUCT_PROPS:
                    err("[%s] unknown construct property '%s'." % (sid, safe_ascii(p, 40)))
            if "type" not in c or not isinstance(c["type"], str) or c["type"] not in CONSTRUCT_TYPES:
                err("[%s] construct.type invalid." % sid)
            if "file" not in c or "line" not in c:
                err("[%s] construct needs file and line." % sid)
                continue
            if not text_ok(c["file"], 300):
                err("[%s] construct.file invalid." % sid)
                continue
            if not _is_int(c["line"]) or c["line"] < 1:
                err("[%s] construct.line must be an integer >= 1." % sid)
                continue
            try:
                src = resolve_inside(root, c["file"])
                if not (src.exists and os.path.isfile(fs_path(src.path))):
                    raise ValueError("file does not exist")
                if src.path not in line_cache:
                    line_cache[src.path] = count_lines(src.path)
                if c["line"] > line_cache[src.path]:
                    err("[%s] line %d is beyond the end of the cited file (%d lines)." % (sid, c["line"], line_cache[src.path]))
            except (PathError, ValueError, jsonio.JsonError, OSError) as exc:
                err("[%s] construct.file: %s" % (sid, safe_ascii(exc, 120)))
    except (PathError, ValueError, jsonio.JsonError, OSError) as exc:
        err("Cannot validate: " + safe_ascii(exc, 200))
    else:
        if request is not None:
            check_request(request, infos, err, run_targets)
    return errors, count, request


def run(args):
    root = project_root(args)
    errors, count, request = validate_ex(root, args.scenarios_path)
    valid = not errors
    out = {"valid": valid, "scenarioCount": count, "errors": errors}
    if request is not None:
        out["narrow"] = request_mod.is_narrow(request)
        out["request"] = request_mod.request_line(request)
    emit_json(out)
    return 0 if valid else 1
