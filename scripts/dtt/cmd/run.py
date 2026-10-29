"""run: lint, build and test the generated tests in one call and print a short, sanitised result (at most 15 lines).

    dtt run --project-root P --run-id ID [--test-project REL] [--more]

Steps: lint the test files written in THIS run (exactly the files in written-files.txt, recorded by guard-writes; no other .cs file is opened; blocking findings abort), `dotnet --version`, `dotnet build`, `dotnet test --no-build` with a trx report.
When every test passed, two more `--no-build` passes run (flakiness check); tests whose outcome differed are `flaky`.
Failed tests get a deterministic hint: likely-test-bug, prod-exception <Type> at <file>, assertion-mismatch expected=.. actual=..
Everything that comes from the repository (file names, test names, messages) is data: control and ANSI characters are removed
and values are cut. The round counter lives in state.json ("round", written only here and by update-state); every call counts, a lint failure too; the 4th run needs --more.
A call that ends as a database ENVIRONMENT problem (EVERY failed test matches DB_ENV_PATTERNS in its own message/stack) is not a round; it is counted
in state.json envOutcomes instead, and after MAX_ENV_OUTCOMES of them the next call needs --more (the user decides).
When the run has a db.json (dtt db up): the container must be alive before any dotnet call (else exit 2); the connection string is passed
as DTT_TEST_DB_CONNECTION to the `dotnet test` calls only (never to build or the version probe), and the connection string and the
password are removed from all dotnet output and from the saved trx files.
Test runner mode (decided once, after `dotnet --version`, from three facts: the SDK major, global.json test.runner as recorded by scan, and the test project's own
packages/properties read with the scan helpers; all of it is data and only compared with fixed values):
  vstest      classic `dotnet test <csproj> --logger trx` (everything that is not Microsoft.Testing.Platform);
  mtp         SDK 10+, global.json selects MTP and the project is MTP: `dotnet test --project <csproj> --report-trx --report-trx-filename pass-N.trx`.
Refused before anything is built, with one fixed sentence and exit 2, no round counted: SDK 10 with an MTP project but no global.json runner key, SDK 10 with
the key but a VSTest project, an MTP project on an SDK older than 10 (the SDK 9 route cannot be trusted: code in the test process can forge the trx and
exit 0), a test project with several target frameworks (their results overwrite one trx file), and a test project (VSTest or MTP) whose path contains ';'.
The trx file stays the single source of truth; a non-zero dotnet exit code with no failed test in the trx is never green (exit 2, "not trusted", both
modes); MTP exit codes: 0 ok, 2 failed tests, 8 no tests, 9 minimum not met, anything else = no usable result. A run in which no test passed is never green.
The trx file (and any *.log next to it) is cleaned of the connection string and password on EVERY path, before anything else is looked at.
Exit: 0 all passed and stable, 1 build/lint/test findings or round refused, 2 environment problem (no dotnet, timeout, no usable trx)."""
from __future__ import annotations

import os
import re

from ..core import docker as dk_mod, jsonio, proc, state as st, writtenfiles
from ..core.paths import PathError, fs_path, resolve_inside
from ..core.textsafe import clean_text
from ..core.xmlsafe import XmlRefused, parse_xml_bytes
from . import lint_tests, scan as scan_mod, verify_gate
from ._common import CmdError, emit, project_root, resolve

MAX_ROUNDS = 3
HARD_MAX_ROUND = 50
MODE_VSTEST, MODE_MTP = "vstest", "mtp"
MTP_NEEDS_GLOBAL_JSON = ("This test project runs on Microsoft.Testing.Platform, but the .NET 10 SDK runs it only when global.json has "
                         "test.runner = Microsoft.Testing.Platform. Tell the user and stop.")
MTP_GLOBAL_JSON_VSTEST = ("global.json selects Microsoft.Testing.Platform, but this test project uses VSTest, which the .NET 10 SDK refuses in that mode. "
                          "Tell the user and stop.")
MTP_SDK_UNSUPPORTED = ("This test project runs on Microsoft.Testing.Platform; dtt run supports that with .NET SDK 10 only. "
                       "Use SDK 10 or a VSTest test project. Tell the user and stop.")
MULTI_TFM = ("This test project has several target frameworks; their results overwrite one result file, so dtt run cannot judge it. "
             "Use a test project with one TargetFramework. Tell the user and stop.")
BAD_PATH = ("The path of this test project or of the run folder contains ';', which dotnet test cannot pass on. "
            "Use a folder path without ';'. Tell the user and stop.")
MAX_LINES = 15
MAX_BUILD_ERRORS = 5
MAX_LINT_LINES = 10
PREFLIGHT_TIMEOUT = 30
BUILD_TIMEOUT = 600
TEST_TIMEOUT = 900
BUILD_OUTPUT_CAP = 1024 * 1024
TEST_OUTPUT_CAP = 256 * 1024
TRX_MAX_BYTES = 20 * 1024 * 1024
MAX_RESULTS = 200000
MAX_MESSAGE = 4000
MAX_STACK_LINES = 60
MAX_STACK_LINE = 500
VALUE_CAP = 80
NAME_CAP = 100
MAX_ENV_OUTCOMES = 2          # database environment outcomes per run before the user must decide (--more)
MAX_ENV_NAMES = 8
DB_NOT_RUNNING = "database container is not running; run dtt db up again"
DB_ENV_MESSAGE = ("the test database could not be used (login failed, connection refused or a SQLite error instead of the real database); "
                  "this round is not counted | hint: environment")
# Fixed patterns (only used when the run has a database container) that make a failed test run an environment problem, not a test bug.
DB_ENV_PATTERNS = re.compile(
    r"login failed for user|NOT NULL constraint failed|SQLite Error|SQLite does not support|"
    r"database container is not running|"
    r"failed to connect to 127\.0\.0\.1|"
    r"(?:connection refused|actively refused|connection timed out|timeout expired|timed out)[^\n]{0,200}(?:127\.0\.0\.1|localhost)|"
    r"(?:127\.0\.0\.1|localhost)[^\n]{0,120}(?:connection refused|actively refused|timed out)", re.IGNORECASE)


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--run-id", required=True, help="run id of the open run")
    p.add_argument("--test-project", default=None, help="project-relative .csproj (or its folder); default: strategy.existingTestProject of the run")
    p.add_argument("--more", action="store_true", help="allow more than %d rounds or more than 2 database environment outcomes (only after the user agreed)" % MAX_ROUNDS)


# ---------------------------------------------------------------- text safety
_ANSI_RX = re.compile(r"\x1b\][^\x07\x1b]{0,2000}(?:\x07|\x1b\\)|\x1b\[[0-?]{0,100}[ -/]{0,20}[@-~]|\x1b[@-Z\\-_]")


def clean(text, limit):
    """Repository text as data: ANSI sequences, control/format/direction characters, line breaks removed; whitespace collapsed; cut."""
    if text is None:
        return ""
    s = _ANSI_RX.sub("", str(text)[:20000])
    s = re.sub(r"\s+", " ", s)
    return clean_text(s, limit).strip()


class DbEnvironment(Exception):
    """The failures are explained by the database environment (the round is not counted). names: the failed tests (may be empty)."""

    def __init__(self, names=()):
        Exception.__init__(self, "database environment")
        self.names = list(names)


class RunIssue(Exception):
    """An environment problem (exit 2) or a refusal (exit 1) with a fixed message."""

    def __init__(self, message, code=2):
        Exception.__init__(self, message)
        self.code = code


# ---------------------------------------------------------------- paths
def _under(path, directory):
    p = os.path.normcase(os.path.abspath(path))
    d = os.path.normcase(os.path.abspath(directory)).rstrip("\\/")
    return p == d or p.startswith(d + os.sep)


def _rel_in_root(raw, root):
    """Project-relative form of a path printed by dotnet, or None when it is not inside the project."""
    if not raw or "\x00" in raw:
        return None
    raw = raw.strip()
    try:
        if not os.path.isabs(raw):
            return None
        if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", raw):
            return None             # a Windows path from a trx made elsewhere: not this tree
        if not _under(raw, root):
            return None
        return os.path.relpath(raw, root).replace(os.sep, "/")
    except (ValueError, OSError):
        return None


def _read_env(root, run_id):
    """The run's 00-environment.json as a dict, or None when it is missing or unusable."""
    env_res = resolve(root, ".test-generator/runs/%s/00-environment.json" % run_id)
    if not env_res.exists:
        return None
    try:
        env = jsonio.read_json_file(env_res.path, 256 * 1024)
    except jsonio.JsonError:
        return None
    return env if isinstance(env, dict) else None


def _global_json_mtp(env):
    """True when scan recorded that global.json selects Microsoft.Testing.Platform (an exact fixed value, nothing else counts)."""
    runner = env.get("runner") if isinstance(env, dict) else None
    return isinstance(runner, dict) and runner.get("dotnetTestMode") == "mtp"


def _project_facts(root, csproj):
    """(runs on Microsoft.Testing.Platform, number of target frameworks) of the test project (same detection as scan: packages and properties only)."""
    try:
        doc = scan_mod.read_xml(csproj)
        stack = scan_mod.get_test_stack(scan_mod.get_package_refs(doc), doc)
        tfms = scan_mod.get_tfms(doc, csproj, root)["tfms"]
        return stack["runner"] == "mtp", len(tfms)
    except Exception:
        return False, 0              # unreadable project: the classic path reports what is wrong


def decide_mode(sdk_major, global_json_mtp, project_mtp):
    """(mode, refusal sentence or None). See the module docstring."""
    if sdk_major >= 10:
        if global_json_mtp and project_mtp:
            return MODE_MTP, None
        if global_json_mtp:
            return MODE_VSTEST, MTP_GLOBAL_JSON_VSTEST
        if project_mtp:
            return MODE_VSTEST, MTP_NEEDS_GLOBAL_JSON
        return MODE_VSTEST, None
    if project_mtp:
        return MODE_VSTEST, MTP_SDK_UNSUPPORTED
    return MODE_VSTEST, None         # SDK 9 and older ignore the global.json test.runner key


def _find_test_project(root, args, run_id):
    env = _ctx["env"] if "env" in _ctx else _read_env(root, run_id)
    cand = args.test_project
    if cand is None:
        if env is None:
            raise CmdError("No --test-project given and the run has no usable 00-environment.json; give --test-project.")
        strategy = env.get("strategy")
        cand = strategy.get("existingTestProject") if isinstance(strategy, dict) else None
        if not isinstance(cand, str) or not cand:
            raise CmdError("The run has no test project recorded in its environment file; give --test-project.")
    res = resolve(root, cand)
    if not res.exists:
        raise CmdError("The test project does not exist.")
    full = res.path
    if os.path.isdir(fs_path(full)):
        found = sorted(n for n in os.listdir(fs_path(full)) if n.lower().endswith(".csproj") and os.path.isfile(fs_path(os.path.join(full, n))))
        if len(found) != 1:
            raise CmdError("Give --test-project as the .csproj file (the folder has %d project files)." % len(found))
        res = resolve(root, "/".join(res.parts + (found[0],)))
        full = res.path
    if not res.parts[-1].lower().endswith(".csproj") or not os.path.isfile(fs_path(full)):
        raise CmdError("--test-project must be a .csproj file.")
    if any(p.casefold() in ("bin", "obj", ".test-generator", ".git") for p in res.parts[:-1]):
        raise CmdError("The test project must not be inside bin, obj, .test-generator or .git.")
    return res


# ---------------------------------------------------------------- build output
_ERR_FILE_RX = re.compile(r"^(?P<file>.{1,500}?)\((?P<line>\d{1,7})(?:,\d{1,7}){0,3}\):\s*error\s+(?P<code>CS\d{4}):\s*(?P<msg>.*?)(?:\s*\[[^\]]{0,500}\])?\s*$")
_ERR_OTHER_RX = re.compile(r"error\s+(?P<code>[A-Za-z]{2,6}\d{2,6}):\s*(?P<msg>[^\r\n]{0,300})")


def parse_build_errors(output, root, test_dir):
    """Returns (cs_errors, other_errors, in_test_count). cs_errors: unique (rel_file, line, code, message, in_test) in output order."""
    seen = set()
    cs = []
    other = []
    in_test = 0
    for raw in output.splitlines()[:20000]:
        line = raw[:1500].strip()
        m = _ERR_FILE_RX.match(line)
        if m:
            rel = _rel_in_root(m.group("file"), root)
            is_test = bool(rel and _under(os.path.join(root, rel), test_dir))
            key = (rel, m.group("line"), m.group("code"), m.group("msg"))
            if key in seen:
                continue
            seen.add(key)
            if is_test:
                in_test += 1
            cs.append((rel or "?", int(m.group("line")), m.group("code"), clean(m.group("msg"), 120), is_test))
            continue
        m = _ERR_OTHER_RX.search(line)
        if m and (m.group("code"), m.group("msg")) not in seen:
            seen.add((m.group("code"), m.group("msg")))
            other.append((m.group("code").upper(), clean(m.group("msg"), 120)))
    return cs, other, in_test


# ---------------------------------------------------------------- trx
def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _bucket(outcome):
    if outcome == "Passed":
        return "passed"
    if outcome in ("Failed", "Error", "Timeout", "Aborted", "InProgress", "Pending"):
        return "failed"             # unfinished results are never counted as fine
    return "skipped"


def parse_trx(path):
    """{test name: (outcome, message, stack, class name)}. Raises RunIssue for an unreadable, oversized or hostile (DTD) file."""
    try:
        data = jsonio.read_bytes_capped(path, TRX_MAX_BYTES)
    except jsonio.JsonError:
        raise RunIssue("the trx file is not a regular file or is larger than %d MB; refused." % (TRX_MAX_BYTES // (1024 * 1024)))
    try:
        root = parse_xml_bytes(data)
    except XmlRefused:
        raise RunIssue("the trx file contains a DTD/entity declaration or an unaccepted encoding; refused.")
    except Exception:
        raise RunIssue("the trx file is not well-formed XML; refused.")
    classes = {}
    for el in root.iter():
        if _local(el.tag) == "UnitTest":
            uid = el.get("id")
            for tm in el:
                if _local(tm.tag) == "TestMethod" and uid:
                    classes[uid] = tm.get("className") or ""
    results = {}
    count = 0
    for el in root.iter():
        if _local(el.tag) != "UnitTestResult":
            continue
        count += 1
        if count > MAX_RESULTS:
            raise RunIssue("the trx file has more than %d results; refused." % MAX_RESULTS)
        name = el.get("testName") or ""
        outcome = el.get("outcome") or ""
        message = stack = ""
        for sub in el.iter():
            tag = _local(sub.tag)
            if tag == "Message" and not message:
                message = (sub.text or "")[:MAX_MESSAGE]
            elif tag == "StackTrace" and not stack:
                stack = (sub.text or "")[:MAX_STACK_LINES * MAX_STACK_LINE]
        prior = results.get(name)
        if prior is not None and _bucket(prior[0]) == "failed":
            continue                 # a duplicate name keeps the failure
        results[name] = (outcome, message, stack, classes.get(el.get("testId") or "", ""))
    return results


# ---------------------------------------------------------------- hints
_ASSERT_TOP_PREFIXES = ("Xunit.Assert", "Xunit.Sdk.", "NUnit.Framework.", "Microsoft.VisualStudio.TestTools.UnitTesting.Assert",
                        "Microsoft.VisualStudio.TestTools.UnitTesting.CollectionAssert", "Microsoft.VisualStudio.TestTools.UnitTesting.StringAssert",
                        "FluentAssertions.", "AwesomeAssertions.", "Shouldly.")
_ASSERT_MSG_RX = re.compile(r"Assert\.\w+\(\)\s*Failure|Assert\.\w+\s+failed|AssertionException|AssertFailedException|ShouldAssertException|"
                            r"^\s*Expected\s*:|^\s*Expected\s+\S[^\n]{0,200}\sto\s", re.MULTILINE)
_TEST_BUG_MSG_RX = re.compile(
    r"Unable to resolve service|No service for type|Unable to find a constructor|Cannot resolve parameter|matching fixture|"
    r"no such table|no such column|Invalid object name|Invalid column name|Cannot find the object|Unknown column|relation \"[^\"]{0,80}\" does not exist|"
    r"does not contain a definition|MissingMethod|MissingMember|MissingField|TypeLoadException|Method not found|Could not load file or assembly|"
    r"SqliteException|DbUpdateException|pending model changes|Cannot insert explicit value for identity|"
    r"Object of type '[^']{0,120}' cannot be converted|MemberData|ClassData", re.IGNORECASE)
_EXC_TYPE_RX = re.compile(r"^\s*([A-Za-z_][\w.`+]{0,100}?(?:Exception|Error))\b")
_EXC_TYPE_MS_RX = re.compile(r"threw exception:\s*([A-Za-z_][\w.`+]{0,100}?(?:Exception|Error))\b")
_FRAME_RX = re.compile(r"^\s*at\s+(?P<method>.+?)(?:\s+in\s+(?P<file>.+?):line\s+(?P<line>\d+))?\s*$")
_EXPECTED_RX = re.compile(r"^\s*Expected\s*:\s*(.*)$", re.MULTILINE)
_ACTUAL_RX = re.compile(r"^\s*(?:Actual|But was)\s*:\s*(.*)$", re.MULTILINE)
_MSTEST_RX = re.compile(r"Expected\s*:\s*<(.{0,300}?)>\.\s*(?:Actual|But was)\s*:\s*<(.{0,300}?)>", re.DOTALL)
_FLUENT_RX = re.compile(r"Expected\s.{0,200}?\sto\s(?:be|equal|contain|have|start with|end with|match)\s(.{0,300}?),?\s(?:but\s)?(?:found|was)\s(.{0,300}?)(?:\.\s|\.$|$)",
                        re.DOTALL)


def _frames(stack):
    out = []
    for raw in stack.splitlines()[:MAX_STACK_LINES]:
        m = _FRAME_RX.match(raw[:MAX_STACK_LINE])
        if m:
            out.append((m.group("method"), m.group("file"), m.group("line")))
    return out


def _exception_type(message):
    m = _EXC_TYPE_MS_RX.search(message[:600]) or _EXC_TYPE_RX.match(message[:600])
    if not m:
        return "Exception"
    return re.sub(r"[^A-Za-z0-9_.`+]", "", m.group(1))[:60] or "Exception"


def _expected_actual(message):
    m = _MSTEST_RX.search(message)
    if m:
        return m.group(1), m.group(2)
    exp = _EXPECTED_RX.search(message)
    act = _ACTUAL_RX.search(message)
    if exp or act:
        return (exp.group(1) if exp else None), (act.group(1) if act else None)
    m = _FLUENT_RX.search(message)
    if m:
        return m.group(1), m.group(2)
    return None, None


def hint_for(message, stack, class_name, root, test_dir):
    """Deterministic hint text for one failed test."""
    frames = _frames(stack)
    top_method = frames[0][0] if frames else ""
    if top_method.startswith(_ASSERT_TOP_PREFIXES) or _ASSERT_MSG_RX.search(message[:1500]):
        exp, act = _expected_actual(message)
        return "assertion-mismatch expected=%s actual=%s" % (clean(exp, VALUE_CAP) if exp is not None else "?",
                                                              clean(act, VALUE_CAP) if act is not None else "?")
    if _TEST_BUG_MSG_RX.search(message[:1500]):
        return "likely-test-bug"
    exc_type = _exception_type(message)
    short_class = class_name.split("`")[0] if class_name else ""
    for method, file, line in frames:
        rel = _rel_in_root(file, root) if file else None
        if rel is not None:
            if _under(os.path.join(root, rel), test_dir):
                return "likely-test-bug"
            return "prod-exception %s at %s%s" % (exc_type, clean(rel, 120), (":" + line) if line else "")
        if short_class and not file and method.startswith(short_class + "."):
            return "likely-test-bug"
    return "no-hint"


# ---------------------------------------------------------------- dotnet calls
def _redact(text):
    secrets = _ctx.get("secrets")
    return dk_mod.redact(text, secrets) if secrets else text


def _dotnet(dotnet, argv, root, timeout, cap, extra_env=None):
    try:
        return proc.run([dotnet] + argv, cwd=root, timeout=timeout, max_output=cap, extra_env=extra_env)
    except OSError:
        raise RunIssue("dotnet could not be started.")


def _scrub_trx(path):
    """A trx file that contains the connection string or password (a test message may echo it) is rewritten redacted; when that is
    impossible the file is deleted (it must never stay readable with a secret in it)."""
    try:
        data = jsonio.read_bytes_capped(path, TRX_MAX_BYTES)
        text = data.decode("utf-8", errors="replace")
        cleaned = dk_mod.redact(text, _ctx["secrets"])
        if cleaned != text:
            jsonio.write_atomic(path, cleaned)
    except (jsonio.JsonError, OSError):
        try:
            os.unlink(fs_path(path))
        except OSError:
            pass
        raise RunIssue("the trx file could not be cleaned of secrets; refused.")


def _drop_logs(trx_dir):
    """Console logs (*.log) next to the trx repeat test output that is not redacted and nothing reads them: removed."""
    try:
        for n in os.listdir(fs_path(trx_dir)):
            p = os.path.join(trx_dir, n)
            if n.lower().endswith(".log") and os.path.isfile(fs_path(p)) and not os.path.islink(fs_path(p)):
                os.unlink(fs_path(p))
    except OSError:
        pass


def _mtp_issue(code, number):
    """A Microsoft.Testing.Platform exit code that is not a plain test verdict. Only fixed sentences; the code is printed as a number."""
    if code == 8:
        return RunIssue("no tests ran (Microsoft.Testing.Platform exit code 8): zero tests were discovered or selected (pass %d)." % number, 1)
    if code == 9:
        return RunIssue("fewer tests ran than the minimum expected (Microsoft.Testing.Platform exit code 9, pass %d)." % number, 1)
    if code == 5:
        return RunIssue("Microsoft.Testing.Platform rejected the command line (exit code 5, pass %d); the Microsoft.Testing.Extensions.TrxReport "
                        "package is probably missing in the test project." % number)
    return RunIssue("the test run ended with exit code %d and no usable result (pass %d); it is not trusted." % (code, number))


def _run_tests(dotnet, csproj, root, trx_dir, number):
    """One `dotnet test --no-build` pass; returns the parsed trx."""
    name = "pass-%d.trx" % number
    trx_path = os.path.join(trx_dir, name)
    try:
        if os.path.lexists(fs_path(trx_path)):
            os.unlink(fs_path(trx_path))
    except OSError:
        raise RunIssue("a previous trx file cannot be removed.")
    mode = _ctx.get("mode", MODE_VSTEST)
    if mode == MODE_MTP:
        argv = ["test", "--project", csproj, "--no-build", "--nologo", "--report-trx", "--report-trx-filename", name, "--results-directory", trx_dir]
    else:
        argv = ["test", csproj, "--no-build", "--nologo", "--logger", "trx;LogFileName=" + name, "--results-directory", trx_dir]
        if _ctx.get("db_env"):
            # the connection string is in dotnet's environment: no MSBuild worker node or build server may keep it after the run.
            # (Not in mode mtp: the SDK 10 MTP command line rejects both options, they would reach the test application and end it with exit
            # code 5; verified on 10.0.402, and no dotnet process was left behind after `dotnet test --project --no-build`.)
            argv.append("-nodeReuse:false")
            if _ctx.get("sdk_major", 0) >= 7:
                argv.append("--disable-build-servers")
    res = _dotnet(dotnet, argv,
                  root, TEST_TIMEOUT, TEST_OUTPUT_CAP, extra_env=_ctx.get("db_env"))      # the connection string reaches `dotnet test` only
    db = bool(_ctx.get("secrets"))
    if db:
        # on EVERY path (timeout, exit codes that are no verdict, refusals below) nothing with a secret may stay in the run folder
        _drop_logs(trx_dir)
        if os.path.lexists(fs_path(trx_path)):
            _scrub_trx(trx_path)
    if res.timed_out:
        raise RunIssue("dotnet test timed out after %d s (pass %d)." % (TEST_TIMEOUT, number))
    output = _redact((res.stdout or "") + "\n" + (res.stderr or "")) if db else ""
    if not os.path.isfile(fs_path(trx_path)):
        if db and DB_ENV_PATTERNS.search(output):
            raise DbEnvironment()         # no test result at all: only dotnet's own output can be looked at
        if mode != MODE_VSTEST:
            raise _mtp_issue(res.returncode, number)
        raise RunIssue("dotnet test produced no trx file (exit code %s, pass %d)." % (res.returncode, number))
    if mode == MODE_MTP and res.returncode not in (0, 2):
        raise _mtp_issue(res.returncode, number)             # 8, 9, 5, 3 ...: not a verdict about the tests, whatever the trx says
    results = parse_trx(trx_path)
    if res.returncode != 0 and results and not any(_bucket(v[0]) == "failed" for v in results.values()):
        raise RunIssue("the test run reported failure (exit code %d) but the trx shows no failed test; it is not trusted (pass %d)." % (res.returncode, number))
    if db:
        # per failed test, from its OWN message and stack only: a log line of a passing test (or of production code) must never turn
        # a real failure into an uncounted environment outcome, and ALL failed tests must match
        failed = [(n, v) for n, v in sorted(results.items()) if _bucket(v[0]) == "failed"]
        if failed and all(DB_ENV_PATTERNS.search("%s\n%s" % (v[1], v[2])) for _n, v in failed):
            raise DbEnvironment([n for n, _v in failed])
    return results


def _sdk_major(version_text):
    """Major number of `dotnet --version` output ('9.0.308', '10.0.100-preview.1'); 0 when it cannot be read."""
    m = re.match(r"\s*([0-9]{1,3})\.", version_text or "")
    return int(m.group(1)) if m else 0


def _counts(results):
    c = {"passed": 0, "failed": 0, "skipped": 0}
    for v in results.values():
        c[_bucket(v[0])] += 1
    return c


def _flaky(passes):
    names = set()
    for p in passes:
        names.update(p)
    out = []
    for n in sorted(names):
        seen = set(_bucket(p[n][0]) if n in p else "missing" for p in passes)
        if len(seen) > 1:
            out.append(n)
    return out


# ---------------------------------------------------------------- command
_ctx = {}


def _production_line():
    """Closing line, same comparison as `verify-integrity --gate`: are the target files of the run still as recorded?"""
    try:
        bad = verify_gate.changed_targets(_ctx["root"], _ctx["state"])
    except verify_gate.NoTargets:
        return "production: not checked (no target recorded)"
    except (PathError, OSError, ValueError, KeyError):
        return "production: not checked"
    return "production: unchanged" if bad == 0 else "production: CHANGED %d file(s)" % bad


def _finish(lines, code, notes=()):
    if _ctx.get("count") and _ctx.get("bump"):
        _ctx["count"] = False
        _ctx["bump"]()                 # a call that got as far as building counts as a round, except a database environment outcome
    if _ctx.get("secrets"):
        lines = [_redact(l) for l in lines]
    if notes and lines:
        lines = lines[:1] + list(notes) + lines[1:]
    if len(lines) > MAX_LINES - 1:
        lines = lines[:MAX_LINES - 2] + ["... %d more line(s) omitted" % (len(lines) - MAX_LINES + 2)]
    if "root" in _ctx:
        lines = lines + [_production_line()]
    emit("\n".join(l[:300] for l in lines) + "\n")
    return code


def run(args):
    _ctx.clear()
    if not st.is_run_id(args.run_id):
        raise CmdError("Invalid run id.")
    root = project_root(args)
    state_res = resolve(root, ".test-generator/runs/%s/state.json" % args.run_id)
    if not state_res.exists:
        raise CmdError("No state.json for run " + args.run_id)
    try:
        state = st.read_state(state_res.path)
    except jsonio.JsonError:
        raise CmdError("state.json is not usable.")
    if state.get("status") != "open":
        raise CmdError("The run is not open.")
    current = state.get("round", 0)
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise CmdError("state.json has an invalid round counter; the run cannot continue. Tell the user.", 2)
    new_round = current + 1
    if new_round > HARD_MAX_ROUND:
        raise CmdError("Round limit reached (%d)." % HARD_MAX_ROUND)
    if new_round > MAX_ROUNDS and not args.more:
        raise CmdError("Round %d would exceed the limit of %d rounds. Ask the user, then run again with --more." % (new_round, MAX_ROUNDS))
    env_outcomes = st.int_or(state.get("envOutcomes"), 0)
    if env_outcomes >= MAX_ENV_OUTCOMES and not args.more:
        raise CmdError("This run already had %d database environment outcomes. Tell the user what the database problem is and ask what to do; "
                       "run again with --more only after the user decided." % env_outcomes)
    _ctx.update(root=root, state=state, env=_read_env(root, args.run_id), bump=lambda: _bump_round(args, state_res))
    proj = _find_test_project(root, args, args.run_id)
    test_dir = os.path.dirname(proj.path)
    try:
        return _run(args, root, state_res, proj, test_dir, new_round)
    except DbEnvironment as exc:
        _ctx["count"] = False           # not a round: the test code did not get a fair run
        n = _bump_env(state_res)
        lines = ["run %s: %s" % (args.run_id, DB_ENV_MESSAGE)]
        lines += ["ENV-ERROR %s" % clean(name, NAME_CAP) for name in exc.names[:MAX_ENV_NAMES]]
        if len(exc.names) > MAX_ENV_NAMES:
            lines.append("+%d more failed test(s)" % (len(exc.names) - MAX_ENV_NAMES))
        lines.append("environment outcome %d of %d%s" % (n, MAX_ENV_OUTCOMES, "; the next run needs the user's decision (--more)" if n >= MAX_ENV_OUTCOMES else ""))
        return _finish(lines, 2)
    except RunIssue as exc:
        return _finish(["run %s: %s" % (args.run_id, clean(exc, 200))], exc.code)


def _recorded_test_files(root, test_dir, recorded):
    """Absolute paths of the recorded files that still exist as regular .cs files (no links) inside the test project folder."""
    out = []
    base = os.path.normcase(os.path.abspath(test_dir))
    for rel in recorded:
        try:
            res = resolve_inside(root, rel)
        except PathError:
            continue
        path = os.path.abspath(res.path)
        norm = os.path.normcase(path)
        if not res.exists or not rel.lower().endswith(".cs") or not norm.startswith(base + os.sep):
            continue
        try:
            if not os.path.isfile(fs_path(path)) or os.path.islink(fs_path(path)):
                continue
        except OSError:
            continue
        out.append(path)
    return sorted(set(out))


def _bump_env(state_res):
    """Counts one database environment outcome (its own counter, not a round). Returns the new count."""
    box = []

    def bump(s):
        n = st.int_or(s.get("envOutcomes"), 0) + 1
        s["envOutcomes"] = n
        s["updatedUtc"] = st.utc_now_iso()
        box.append(n)
    try:
        st.update_state_file(state_res.path, bump)
    except (jsonio.JsonError, OSError):
        return MAX_ENV_OUTCOMES
    return box[0]


def _bump_round(args, state_res):
    """Counts this call as a round (state.json); the 4th needs --more."""
    def bump(s):
        n = st.int_or(s.get("round"), 0) + 1
        if n > MAX_ROUNDS and not args.more:
            raise CmdError("Round %d would exceed the limit of %d rounds. Ask the user, then run again with --more." % (n, MAX_ROUNDS))
        s["round"] = n
        s["updatedUtc"] = st.utc_now_iso()
    st.update_state_file(state_res.path, bump)


def _run(args, root, state_res, proj, test_dir, new_round):
    lines = []
    notes = []

    _ctx["is_mtp"], ntfm = _project_facts(root, proj.path)
    if ntfm > 1:
        raise RunIssue(MULTI_TFM)                 # before lint and before any round is counted
    trx_probe = resolve(root, ".test-generator/runs/%s/trx" % args.run_id).path
    if ";" in proj.path or ";" in trx_probe:
        raise RunIssue(BAD_PATH)

    recorded = writtenfiles.load(os.path.dirname(state_res.path))
    files = _recorded_test_files(root, test_dir, recorded)
    if not recorded:
        notes.append("lint: no files recorded, skipped")
    elif not files:
        notes.append("lint: no recorded file exists, skipped")
    else:
        try:
            _scanned, _tests, findings = lint_tests.lint_files(root, files)
        except (CmdError, PathError, OSError):
            raise RunIssue("the lint scan could not run.", 2)
        blocking = [f for f in findings if f["severity"] == "blocking"]
        if blocking:
            _bump_round(args, state_res)             # a lint loop counts too
            lines.append("lint failed: %d blocking finding(s); nothing was built" % len(blocking))
            for f in blocking[:MAX_LINT_LINES - 2]:
                where = ("%s:%s " % (clean(f["file"], 100), f["line"])) if f["file"] else ""
                lines.append("%s%s %s %s" % (where, clean(f["code"], 30), clean(f["method"], 60), clean(f["message"], 100)))
            return _finish(lines[:MAX_LINT_LINES - 1], 1)

    run_dir = os.path.dirname(state_res.path)
    if not os.path.lexists(os.path.join(run_dir, dk_mod.DB_JSON)) and _ctx["state"].get("dbUsed") is True:
        raise RunIssue(DB_NOT_RUNNING)       # a database was used in this run but its db.json is gone: never run the tests without it
    if os.path.lexists(os.path.join(run_dir, dk_mod.DB_JSON)):
        # a database container belongs to this run: it must be alive before anything else is started
        try:
            data = dk_mod.read_db_json(run_dir)
            if data is None or not dk_mod.is_alive(data["name"], root=root):
                raise RunIssue(DB_NOT_RUNNING)
            _ctx["db_env"], _ctx["secrets"] = dk_mod.connection_with_secrets(run_dir, project_root=root)
        except dk_mod.DockerError:
            raise RunIssue(DB_NOT_RUNNING)

    dotnet = proc.resolve_dotnet(exclude_dirs=[root])
    if dotnet is None:
        raise RunIssue("dotnet was not found (PATH or DOTNET_ROOT); install the .NET SDK.")
    pre = _dotnet(dotnet, ["--version"], root, PREFLIGHT_TIMEOUT, 16 * 1024)
    if pre.timed_out or pre.returncode != 0:
        raise RunIssue("`dotnet --version` failed (%s); the SDK is not usable here." % ("timeout" if pre.timed_out else "exit " + str(pre.returncode)))

    _ctx["sdk_major"] = _sdk_major(pre.stdout)
    _ctx["mode"], refusal = decide_mode(_ctx["sdk_major"], _global_json_mtp(_ctx.get("env")), _ctx["is_mtp"])
    if refusal:
        raise RunIssue(refusal)           # before the round is counted and before anything is built
    _ctx["count"] = True                 # counted when the run ends (see _finish), except a database environment outcome
    csproj = proj.path
    # No MSBuild worker node and no build server may outlive the run (a timeout kills only dotnet itself): node reuse is off, and the
    # SDK 7+ option --disable-build-servers is passed only when `dotnet --version` says 7 or newer (older SDKs would refuse it).
    build_argv = ["build", csproj, "--nologo", "-v", "q", "-nodeReuse:false"]
    if _sdk_major(pre.stdout) >= 7:
        build_argv.append("--disable-build-servers")
    b = _dotnet(dotnet, build_argv, root, BUILD_TIMEOUT, BUILD_OUTPUT_CAP)
    if b.timed_out:
        raise RunIssue("dotnet build timed out after %d s." % BUILD_TIMEOUT)
    label = "round %d/%d" % (new_round, MAX_ROUNDS)
    if b.returncode != 0:
        cs, other, in_test = parse_build_errors(_redact(b.stdout + "\n" + b.stderr), root, test_dir)
        total = len(cs) if cs else len(other)
        head = "run %s %s: build FAILED, %d error(s)" % (args.run_id, label, total)
        if in_test:
            head += ", %d in test project files => likely-test-bug" % in_test
        lines.append(head)
        for rel, line, code, msg, _t in cs[:MAX_BUILD_ERRORS]:
            lines.append("%s:%d %s %s" % (clean(rel, 150), line, code, msg))
        if not cs:
            for code, msg in other[:MAX_BUILD_ERRORS]:
                lines.append("%s %s" % (code, msg))
            if not other:
                lines.append("no CSxxxx error could be read from the build output (exit code %s)" % b.returncode)
        if len(cs) > MAX_BUILD_ERRORS:
            lines.append("+%d more error(s)" % (len(cs) - MAX_BUILD_ERRORS))
        return _finish(lines, 1, notes)

    trx_dir = resolve(root, ".test-generator/runs/%s/trx" % args.run_id).path
    try:
        os.makedirs(fs_path(trx_dir), exist_ok=True)
    except OSError:
        raise RunIssue("the trx folder cannot be created.")
    first = _run_tests(dotnet, csproj, root, trx_dir, 1)
    counts = _counts(first)
    flaky = []
    if counts["failed"] == 0 and counts["passed"] > 0:
        passes = [first, _run_tests(dotnet, csproj, root, trx_dir, 2), _run_tests(dotnet, csproj, root, trx_dir, 3)]
        flaky = _flaky(passes)
    stable_passed = counts["passed"] - len([n for n in flaky if n in first and _bucket(first[n][0]) == "passed"])

    lines.append("run %s %s: passed=%d failed=%d flaky=%d skipped=%d%s" % (
        args.run_id, label, stable_passed, counts["failed"], len(flaky), counts["skipped"],
        "" if sum(counts.values()) else " (no tests ran)"))
    for name, (outcome, message, stack, cls) in sorted(first.items()):
        if _bucket(outcome) == "failed":
            lines.append("FAIL %s | %s" % (clean(name, NAME_CAP), hint_for(message, stack, cls, root, test_dir)))
    for name in flaky:
        lines.append("FLAKY %s" % clean(name, NAME_CAP))
    if counts["passed"] == 0 and sum(counts.values()) > 0:
        lines.append("no test passed (all %d were skipped or not run)" % sum(counts.values()))
    return _finish(lines, notes=notes, code=0 if counts["failed"] == 0 and not flaky and counts["passed"] > 0 else 1)
