"""lint-tests: heuristic lint for weak or vacuous unit tests in .cs test files.

Scans a test file (or all .cs files below a test directory) for test methods that cannot fail or are non-deterministic. This is a text
heuristic (it does not compile C#); it is a cheap first gate, not proof. A scan that finds no test methods is NOT clean.
Prints {clean, filesScanned, testsFound, blocking, findings[]}. Exit 0 = clean, 1 = blocking findings (or no tests found),
2 = the scan itself failed. A scan that stops early or skips anything (time budget, test cap, over-long line, big file, unclosed
test body, special file) reports a BLOCKING SCAN_INCOMPLETE finding: a vacuous test must never pass as clean."""
from __future__ import annotations

import bisect
import os
import re
import stat
import time

from ..core import jsonio
from ..core.paths import PathError, fs_path, is_link_stat, resolve_inside
from ..core.textsafe import decode_source, safe_ascii
from ._common import CmdError, emit_json, project_root

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_CHARS = 4000            # a longer line is not hand-written C#: the file is reported as incompletely scanned (blocking)
MAX_ATTR_CHARS = 300             # an attribute list longer than this is not a test attribute
MAX_TESTS_PER_FILE = 5000
FILE_BUDGET_SECONDS = 8.0
SKIP_DIRS = ("bin", "obj", ".git", "node_modules")

# Test attributes of xUnit (Fact/Theory), NUnit (Test/TestCase/TestCaseSource) and MSTest (TestMethod/DataTestMethod).
_ATTR_NAME = re.compile(r"\b(Fact|Theory|Test|TestCase|TestCaseSource|TestMethod|DataTestMethod)\b")
# Assertions of the built-in frameworks, FluentAssertions/AwesomeAssertions (.Should()), Shouldly (.ShouldBe...), exception helpers,
# and mock verification (Moq .Verify, NSubstitute .Received, FakeItEasy MustHaveHappened).
ASSERT_RX = re.compile(
    r"\bAssert[A-Z]\w*\s*\(|Assert\s*\.|\b(CollectionAssert|StringAssert|FileAssert|DirectoryAssert)\s*\.|\.Should\s*[(.]|"
    r"\.Should[A-Z]\w*\s*\(|\bShould(Throw|NotThrow)\w*\s*[<(]|\bShould\s*\.\s*(Throw|NotThrow)\w*\s*[<(]|Throws(Async)?\s*[<(]|"
    r"\.Received\s*\(|\.DidNotReceive\s*\(|\bVerify(All|NoOtherCalls)?\s*\(|\bMustHaveHappened\w*|\bMustNotHaveHappened\w*|"
    r"Record\s*\.\s*(Exception|ExceptionAsync)\s*\(")
# Assertions that prove almost nothing on their own.
WEAK_RX = re.compile(
    r"Assert\s*\.\s*(NotNull|NotEmpty|IsNotNull|IsNotEmpty)\s*\(|Assert\s*\.\s*That\s*\([^;]{0,300}Is\s*\.\s*Not\s*\.\s*(Null|Empty)\s*\)|"
    r"\.Should\s*\(\s*\)\s*\.\s*(NotBeNull|NotBeEmpty)\s*\(|\.ShouldNotBe(Null|Empty)\s*\(")
_I = re.IGNORECASE      # the patterns are case-insensitive
SKIP_RX = re.compile(r"Skip\s*=", _I)
TAUTOLOGY_RXS = (
    re.compile(r"Assert\s*\.\s*(True|IsTrue)\s*\(\s*(true|1\s*==\s*1)\s*\)", _I),
    re.compile(r"Assert\s*\.\s*(False|IsFalse)\s*\(\s*(false|1\s*!=\s*1)\s*\)", _I),
    re.compile(r"Assert\s*\.\s*That\s*\(\s*true\s*(,\s*Is\s*\.\s*True\s*)?\)", _I),
    re.compile(r"\btrue\s*\.\s*Should\s*\(\s*\)|\btrue\s*\.\s*ShouldBeTrue\s*\(", _I),
)
SELF_CMP_RXS = (
    re.compile(r"Assert\s*\.\s*(Equal|AreEqual)\s*\(\s*([\w\.]+)\s*,\s*\2\s*\)", _I),
    re.compile(r"Assert\s*\.\s*That\s*\(\s*([\w\.]+)\s*,\s*Is\s*\.\s*EqualTo\s*\(\s*\1\s*\)\s*\)", _I),
)
EMPTY_CATCH_RX = re.compile(r"catch\s*(\([^)]{0,300}\))?\s*\{\s*\}", _I)
SLEEP_RX = re.compile(r"Thread\s*\.\s*Sleep|Task\s*\.\s*Delay", _I)
NONDET_RX = re.compile(r"DateTime(Offset)?\s*\.\s*(Utc)?Now|DateTime\s*\.\s*Today|new\s+Random\s*\(\s*\)|Guid\s*\.\s*NewGuid\s*\(", _I)
_LINE_COMMENT = re.compile(r"//[^\r\n]*")
_BRACKET_CHARS = re.compile(r"[\[\]{}]")
# A string literal (one line) that holds a login and a server part is a hand-written connection string: tests must read the environment
# variable DTT_TEST_DB_CONNECTION instead. Linear: no overlapping alternation, a literal cannot cross a line.
_STRING_LITERAL_RX = re.compile(r'"(?:[^"\\\r\n]|\\.)*"')
_CRED_RX = re.compile(r"\b(?:password|pwd|user\s*id|username|uid)\s*=", _I)
_SERVER_RX = re.compile(r"\b(?:server|host|data\s*source)\s*=", _I)
MAX_CONNECTION_FINDINGS = 3


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--path", required=True)


def _collect_files(root, full, skipped):
    """The .cs files at/below `full`, without following links. Returns absolute paths; .cs entries that are not regular files
    (pipe, socket, device) are added to `skipped` and never opened."""
    if os.path.isfile(fs_path(full)):
        return [full]
    if not os.path.isdir(fs_path(full)):
        raise CmdError("Path does not exist.")
    files = []
    stack = [full]
    while stack:
        d = stack.pop()
        with os.scandir(fs_path(d)) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            st_ = entry.stat(follow_symlinks=False)
            if is_link_stat(st_):
                continue
            path = os.path.join(d, entry.name)
            if entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() not in SKIP_DIRS:
                    stack.append(path)
            elif entry.name.lower().endswith(".cs"):
                if stat.S_ISREG(st_.st_mode):
                    files.append(path)
                else:
                    skipped.append(path)
    return sorted(files)


def blank_comments(raw):
    """Comments are blanked (block comments keep their newlines) so '// Assert.x' or commented-out code is not counted. Linear time."""
    pieces = []
    pos = 0
    while True:
        i = raw.find("/*", pos)
        if i < 0:
            break
        j = raw.find("*/", i + 2)
        if j < 0:
            break               # unclosed: no later closer exists either, the rest stays as it is
        pieces.append(raw[pos:i])
        pieces.append(re.sub(r"[^\r\n]", " ", raw[i:j + 2]))
        pos = j + 2
    pieces.append(raw[pos:])
    return _LINE_COMMENT.sub("", "".join(pieces))


def _bracket_tables(text):
    """One pass: matching '}' for every '{' and matching ']' for every '[' (stack matching)."""
    braces, brackets = {}, {}
    open_braces, open_brackets = [], []
    for m in _BRACKET_CHARS.finditer(text):
        ch = m.group(0)
        i = m.start()
        if ch == "{":
            open_braces.append(i)
        elif ch == "}":
            if open_braces:
                braces[open_braces.pop()] = i
        elif ch == "[":
            open_brackets.append(i)
        elif open_brackets:
            brackets[open_brackets.pop()] = i
    return braces, brackets


def _trailing_word(sig):
    j = len(sig.rstrip())
    k = j
    while k > 0 and (sig[k - 1].isalnum() or sig[k - 1] == "_"):
        k -= 1
    return sig[k:j]


class _Attr(object):
    """Start, end and the matched text of one test attribute list."""
    __slots__ = ("s", "e", "text")

    def __init__(self, s, e, text):
        self.s, self.e, self.text = s, e, text

    def start(self):
        return self.s

    def end(self):
        return self.e

    def group(self, _n=0):
        return self.text


def _attributes(text, deadline):
    """Linear pairing: every '[' is paired with the next ']' (found by bisect over the ']'
    positions); only a pair on one line and at most MAX_ATTR_CHARS long is looked at, so no regex ever scans a long line.
    Yields _Attr in file order without overlaps (like finditer); yields None once when the time budget is used up."""
    closes = [m.start() for m in re.finditer(r"\]", text)]
    pos = 0
    count = 0
    while True:
        i = text.find("[", pos)
        if i < 0:
            return
        count += 1
        if count % 2048 == 0 and time.monotonic() > deadline:
            yield None
            return
        k = bisect.bisect_left(closes, i + 1)
        if k >= len(closes):
            return                      # no ']' after this '[' (or any later one)
        close = closes[k]
        pos = i + 1
        if close - i > MAX_ATTR_CHARS or "\n" in text[i:close] or "\r" in text[i:close]:
            continue
        if _ATTR_NAME.search(text[i + 1:close]):
            pos = close + 1
            yield _Attr(i, close + 1, text[i:close + 1])


def _incomplete(findings, rel, line, message):
    findings.append({"file": rel, "line": line, "method": "", "code": "SCAN_INCOMPLETE", "severity": "blocking",
                     "message": message + " The rest was not examined, so this is not a pass."})


def scan_text(raw, rel, findings):
    """Appends findings for one file's text; returns the number of test methods found."""
    if any(len(line) > MAX_LINE_CHARS for line in raw.split("\n")):
        _incomplete(findings, rel, 1, "File skipped (a line is longer than %d characters)." % MAX_LINE_CHARS)
        return 0
    deadline = time.monotonic() + FILE_BUDGET_SECONDS
    text = blank_comments(raw)
    shown = 0
    for lit in _STRING_LITERAL_RX.finditer(text):
        body = lit.group(0)
        if _CRED_RX.search(body) and _SERVER_RX.search(body):
            findings.append({"file": rel, "line": text.count(chr(10), 0, lit.start()) + 1, "method": "", "code": "literal-connection-string",
                             "severity": "blocking",
                             "message": "A string literal holds a connection string with credentials; read the environment variable DTT_TEST_DB_CONNECTION instead."})
            shown += 1
            if shown >= MAX_CONNECTION_FINDINGS:
                break
    braces, brackets = _bracket_tables(text)
    tests = 0
    line_at, last_idx = 1, 0     # incremental line counting: matches arrive in order
    unclosed = 0
    first_unclosed = 0
    for m in _attributes(text, deadline):
        if m is None or tests >= MAX_TESTS_PER_FILE or time.monotonic() > deadline:
            _incomplete(findings, rel, line_at, "Scan stopped (test count or time budget of the file exceeded).")
            break
        after_attr = m.end()
        # Skip further attributes such as [InlineData(17)] between the test attribute and the signature.
        while True:
            nxt = after_attr
            while nxt < len(text) and text[nxt].isspace():
                nxt += 1
            if nxt >= len(text) or text[nxt] != "[":
                break
            close_b = brackets.get(nxt, -1)
            if close_b < 0:
                break
            after_attr = close_b + 1
        paren = text.find("(", after_attr)
        if paren < 0:
            continue
        brace = text.find("{", paren)
        arrow = text.find("=>", paren)
        if brace < 0 and arrow < 0:
            continue
        sig = text[after_attr:paren]
        name = _trailing_word(sig)

        line_at += text.count("\n", last_idx, m.start())
        last_idx = m.start()
        line = line_at
        tests += 1

        def add(code, severity, message):
            findings.append({"file": rel, "line": line, "method": name, "code": code, "severity": severity, "message": message})

        if SKIP_RX.search(m.group(0)):
            add("SKIPPED_TEST", "blocking", "Test is skipped, so it proves nothing.")

        if brace >= 0 and (arrow < 0 or brace < arrow):
            end = braces.get(brace, -1)
            if end < 0:
                unclosed += 1
                first_unclosed = first_unclosed or line
                continue
            body = text[brace:end + 1]
        else:
            semi = text.find(";", arrow)
            if semi < 0:
                continue
            body = text[arrow:semi + 1]

        asserts = len(ASSERT_RX.findall(body))
        if asserts == 0:
            add("NO_ASSERTION", "blocking", "Test method contains no assertion, verification or expected exception.")
        else:
            weak = len(WEAK_RX.findall(body))
            if weak > 0 and weak >= asserts:
                add("ONLY_NOT_NULL", "blocking", "The only assertions are NotNull / NotEmpty; assert the actual value.")
        if any(rx.search(body) for rx in TAUTOLOGY_RXS):
            add("TAUTOLOGY", "blocking", "Assertion is always true.")
        if any(rx.search(body) for rx in SELF_CMP_RXS):
            add("SELF_COMPARISON", "blocking", "An assertion compares a value with itself.")
        if EMPTY_CATCH_RX.search(body):
            add("EMPTY_CATCH", "blocking", "Empty catch block swallows failures.")
        if SLEEP_RX.search(body):
            add("WAITS_FOR_TIME", "warning", "Sleep/Delay makes the test slow and potentially flaky; use a fake clock.")
        if NONDET_RX.search(body):
            add("NON_DETERMINISTIC", "warning", "Uses current time, unseeded Random or a random Guid; inject a fixed value.")
    if unclosed:
        _incomplete(findings, rel, first_unclosed, "%d test method(s) have no closing brace (first at this line)." % unclosed)
    return tests


def lint(root, path):
    """Scans `path` (project-relative file or directory). Returns (files_scanned, tests_found, findings). Raises CmdError, PathError
    or OSError when the scan itself cannot run."""
    full = resolve_inside(root, path).path
    special = []
    files = _collect_files(root, full, special)
    return lint_files(root, files, special)


def lint_files(root, files, special=()):
    """Like lint(), for an explicit list of absolute .cs paths (used by `dtt run` for the files written in this run)."""
    findings = []
    tests_found = 0
    files_scanned = 0
    for file in files:
        files_scanned += 1
        rel = os.path.relpath(file, root).replace(os.sep, "/")
        try:
            data = jsonio.read_bytes_capped(file, MAX_FILE_BYTES)          # regular files only, never blocks on a pipe
        except jsonio.JsonError:
            _incomplete(findings, rel, 1, "File skipped (over 2 MB, or not a readable regular file).")
            continue
        tests_found += scan_text(decode_source(data), rel, findings)
    for file in special:
        rel = os.path.relpath(file, root).replace(os.sep, "/")
        _incomplete(findings, rel, 1, "File skipped (not a regular file: pipe, socket or device).")
    if tests_found == 0:
        findings.append({"file": "", "line": 0, "method": "", "code": "NO_TESTS_FOUND", "severity": "blocking",
                         "message": "No test methods were found; an empty scan is not a pass."})
    return files_scanned, tests_found, findings


def run(args):
    try:
        root = project_root(args)
        files_scanned, tests_found, findings = lint(root, args.path)
    except (CmdError, PathError, OSError) as exc:
        emit_json({"clean": False, "error": safe_ascii(exc, 200)})
        return 2
    blocking = sum(1 for f in findings if f["severity"] == "blocking")
    emit_json({"clean": blocking == 0, "filesScanned": files_scanned, "testsFound": tests_found, "blocking": blocking, "findings": findings})
    return 0 if blocking == 0 else 1
