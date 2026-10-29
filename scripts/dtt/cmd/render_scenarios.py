"""render-scenarios: renders a validated 01-scenarios.json as a compact Markdown table, writes 01-scenarios.md next to it and
prints the table. Text fields are single-line and length-limited by validate-scenarios; '|' is escaped so a value cannot break the table.

Optional extras, only when the file has them: a first line "Scope: requested ..., produced N,
left out: ..." (top-level scope), a "Form: one [Theory] group G1 (n rows)" line (scenario group), and the columns conflict and
existing (scenario conflict / existingTest), and a line "request: narrow|full, N scenarios max" when a valid request block exists.
Everything echoed goes through the sanitiser."""
from __future__ import annotations

from ..core import jsonio, request_block, state as st
from ..core.textsafe import clean_text
from ._common import CmdError, emit, project_root, resolve


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--scenarios-path", required=True)


CELL_MAX = 300         # the longest field validate-scenarios accepts


def cell(value):
    if value is None:
        value = ""
    elif isinstance(value, bool):
        value = "True" if value else "False"
    text = clean_text(str(value), -1)
    if len(text) > CELL_MAX:
        text = text[:CELL_MAX - 3] + "..."       # never cut silently (only a file that skipped validate-scenarios gets here)
    return text.replace("\\", "\\\\").replace("|", "\\|")


def line_text(value, limit):
    """Text for a line outside the table: sanitised, one line."""
    return clean_text("" if value is None else str(value), limit)


def scope_line(scope):
    if not isinstance(scope, dict):
        return None
    left = scope.get("leftOut")
    items = [line_text(x, 100) for x in left[:10]] if isinstance(left, list) else []
    items = [x for x in items if x]
    return "Scope: requested %s, produced %d, left out: %s" % (
        line_text(scope.get("requested"), 200), st.int_or(scope.get("produced"), 0), "; ".join(items) if items else "none")


def form_line(scenarios):
    groups = {}
    for s in scenarios:
        g = s.get("group") if isinstance(s, dict) else None
        if isinstance(g, str):
            groups[g] = groups.get(g, 0) + 1
    if not groups:
        return None
    return "Form: " + "; ".join("one [Theory] group %s (%d row%s)" % (line_text(g, 10), n, "" if n == 1 else "s")
                                for g, n in sorted(groups.items()))


def render_text(doc):
    """The Markdown text for a scenario document (raises CmdError for a document that cannot be rendered)."""
    if not isinstance(doc, dict) or "scenarios" not in doc:
        raise CmdError("Not a scenario document.")
    scenarios = doc["scenarios"]
    if not isinstance(scenarios, list):
        scenarios = [scenarios]
    show_conflict = any(isinstance(s, dict) and "conflict" in s for s in scenarios)
    show_existing = any(isinstance(s, dict) and "existingTest" in s for s in scenarios)
    header = "| id | member | where | input | expected (basis) | kind |"
    rule = "|---|---|---|---|---|---|"
    if show_conflict:
        header += " conflict |"
        rule += "---|"
    if show_existing:
        header += " existing |"
        rule += "---|"
    lines = [header, rule]
    ambiguous = 0
    for s in scenarios:
        if not isinstance(s, dict) or not isinstance(s.get("construct"), dict):
            raise CmdError("A scenario has no construct; run validate-scenarios first.")
        c = s["construct"]
        where = "%s:%d" % (cell(c.get("file")), st.int_or(c.get("line"), 0))
        row = "| %s | %s | %s | %s | %s (%s) | %s |" % (
            cell(s.get("id")), cell(s.get("member")), where, cell(s.get("input")), cell(s.get("expected")),
            cell(s.get("expectedBasis")), cell(s.get("kind")))
        if show_conflict:
            row += " %s |" % cell(s.get("conflict"))
        if show_existing:
            row += " %s |" % cell(s.get("existingTest"))
        lines.append(row)
        if s.get("kind") == "ambiguous":
            ambiguous += 1
    head = []
    scope = scope_line(doc["scope"]) if "scope" in doc else None
    if scope:
        head.append(scope)
    head.append("Scenarios: %d (ambiguous: %d)" % (len(scenarios), ambiguous))
    if "request" in doc:
        try:
            head.append(request_block.request_line(request_block.validate_block(doc["request"])))
        except request_block.RequestError:
            pass            # validate-scenarios reports an invalid block; the table is still rendered
    form = form_line(scenarios)
    if form:
        head.append(form)
    text = "%s\n\n%s\n" % ("\n".join(head), "\n".join(lines))
    return text


def run(args):
    root = project_root(args)
    res = resolve(root, args.scenarios_path)
    if res.parts[-1:] != ("01-scenarios.json",):
        raise CmdError("Expected a file named 01-scenarios.json.")
    doc = jsonio.read_json_file(res.path, jsonio.MAX_SCENARIOS_BYTES)
    text = render_text(doc)
    md_path = res.path[:-len("01-scenarios.json")] + "01-scenarios.md"
    resolve(root, "/".join(res.parts[:-1] + ("01-scenarios.md",)))     # same confinement rules for the output file
    jsonio.write_atomic(md_path, text)
    emit(text)
    return 0
