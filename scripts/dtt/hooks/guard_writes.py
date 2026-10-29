"""PreToolUse hook (Write|Edit|MultiEdit|NotebookEdit): enforces where this plugin's subagents may write.

Plugin subagents cannot carry their own hooks, so the limits live here. The hook only acts when the calling agent is one of
this plugin's agents (agent_type in the hook input); the main conversation and all other agents are never touched, except
that nobody may write the files the plugin's own scripts own (state.json, 00-environment.json, snapshot/, written-files.txt of a run).

    test-writer       -> files inside the test project directory of an open run (the folder of strategy.existingTestProject or of a
                         test project that references the target, from that run's 00-environment.json, or the scaffold folder
                         tests/<Name>.Tests): .cs/.json/.txt/.csv, never project, SDK, NuGet or launch configuration such as
                         global.json or launchSettings.json; a file that already exists only when it is listed in the run's
                         written-files.txt (the writer's own file); and the notes 03-*.md (not 03-round-*.md) in
                         .test-generator/runs/<id>/ (nothing else there). No known test directory = only the notes.
    scenario-analyst  -> exactly .test-generator/runs/<id>/01-scenarios.json, and only while that run is open and in analyze/approval
    every other agent of this plugin, and any unknown name below the prefix dotnet-test-team: -> no writes at all
Tests are code: building or running them executes them with the user's rights. These rules limit WHERE files go,
not what the code in them does.
Exit 0 = allow, 2 = block (reason on stderr)."""
from __future__ import annotations

import os
import re
import time
import unicodedata

from ..core import jsonio, paths, state as st, writtenfiles
from ..core.paths import PathError, resolve_inside, fs_path
from ..core.textsafe import safe_ascii
from ._common import block, our_agent_name, tool_input

PREFIX = "dotnet-test-team write guard"
TG = ".test-generator"
EXT_ALLOWED = (".cs", ".json", ".txt", ".csv")
_FORBIDDEN_DIRS = (".git", ".claude", "bin", "obj")
_WRITER_NOTE_RX = re.compile(r"03-.*\.md", re.IGNORECASE | re.DOTALL)
# names a test-writer may never create inside a test folder although the extension is allowed (xunit.runner.json is plain data: allowed)
_DENIED_NAMES = frozenset(("global.json", "nuget.config", "dotnet-tools.json", "launchsettings.json", "directory.packages.props"))
_DENIED_SUFFIXES = (".csproj", ".vbproj", ".fsproj", ".props", ".targets", ".rsp", ".runsettings", ".testsettings", ".config")
_TEST_FRAMEWORK_RX = re.compile(r"(?:Microsoft\.NET\.Test\.Sdk|xunit|NUnit|MSTest|TUnit)", re.IGNORECASE)
_ATTR_VALUE_RX = re.compile(r"""\b(?:Include|Update|Sdk)\s*=\s*(?:"([^"\r\n]{1,200})"|'([^'\r\n]{1,200})')""", re.IGNORECASE)
CSPROJ_READ_CAP = 256 * 1024


def _fold(s: str) -> str:
    return s.casefold()


def _nfold(s: str) -> str:
    """NFC first (macOS lists names decomposed); case-folded on Windows only (elsewhere two spellings are two different files, and an
    existing file of the user must never be taken for one the writer made): for comparing folder and file names with the run files."""
    s = unicodedata.normalize("NFC", s)
    return s.casefold() if os.name == "nt" else s


def _is_script_owned(parts) -> bool:
    """.test-generator/runs/<anything>/(state.json* | 00-environment.json* | db.json* | snapshot[/...])."""
    if len(parts) < 4 or _fold(parts[0]) != TG or _fold(parts[1]) != "runs":
        return False
    p3 = _fold(parts[3])
    return p3.startswith("state.json") or p3.startswith("00-environment.json") or p3 == "snapshot" or p3.startswith("written-files.txt") or p3.startswith("db.json")


# Admin share of some host (UNC host, then a drive letter and a dollar sign). The host is NOT checked: every spelling of this machine
# (localhost, 127.x.x.x, ::1, its own names and addresses) has to count; for a remote host the answer "this is a run file" is harmless.
_ADMIN_SHARE_RX = re.compile(r"^[\\/]{2}[^\\/]+[\\/]([A-Za-z])\$(?:[\\/](.*))?$", re.DOTALL)
_EXTENDED_PREFIXES = ("\\\\?\\", "//?/", "\\\\.\\", "//./")


def _plain_windows_form(fp):
    """Windows only: the extended-length (question-mark) and device (dot) prefixes, the UNC form of them and the admin share of any host
    name the same file as the plain drive path; return that plain form (anything else unchanged)."""
    if os.name != "nt":
        return fp
    if fp.startswith(_EXTENDED_PREFIXES):
        rest = fp[4:]
        if rest[:4].upper() in ("UNC\\", "UNC/"):
            fp = "\\\\" + rest[4:]
        else:
            return rest
    m = _ADMIN_SHARE_RX.match(fp)
    if m:
        return m.group(1) + ":\\" + (m.group(2) or "")
    return fp


def _unc_names_run_file(fp) -> bool:
    """Windows only: a UNC path that is not an admin share cannot be mapped to this project; if it spells the script-owned run
    files (.test-generator/runs/<id>/state.json ...) anywhere in its segments it is refused."""
    if os.name != "nt" or fp[:2] not in ("\\\\", "//", "\\/", "/\\"):
        return False
    parts = [p for p in re.split(r"[\\/]", fp) if p]
    return any(_is_script_owned(parts[i:]) for i in range(len(parts)))


def _path_value(payload: dict):
    """The target path of the tool call: the key that belongs to the tool decides (NotebookEdit: notebook_path, the others:
    file_path), the other known keys are only a fallback. Returns None when no key is present, "" for a non-string value."""
    ti = tool_input(payload)
    tool = payload.get("tool_name")
    keys = ("notebook_path", "file_path", "path") if tool == "NotebookEdit" else ("file_path", "notebook_path", "path")
    for key in keys:
        if key in ti:
            v = ti[key]
            return v if isinstance(v, str) else ""
    return None


_SCRIPT_OWNED_MESSAGE = ("state.json, 00-environment.json, db.json and snapshot/ and written-files.txt of a run are written only by "
                         "the plugin scripts (dtt scan, dtt update-state, dtt run). Call the script instead of writing the file.")


def _main_conversation_check(payload: dict) -> int:
    """Not one of our agents: the only thing guarded are the files that the plugin's scripts own. Never fails closed."""
    try:
        fp = _path_value(payload)
        if not fp or not fp.strip():
            return 0
        fp = _plain_windows_form(fp)
        if _unc_names_run_file(fp):
            return block(PREFIX, _SCRIPT_OWNED_MESSAGE)
        root = paths.get_project_root(None)
        # also classify what the path really points at (a link into a run folder must not be a way around the rule)
        try:
            absolute = fp if os.path.isabs(fp) else os.path.join(root, fp)
            real = os.path.realpath(absolute)
            real_root = os.path.realpath(root)
            for base, cand in ((root, fp), (real_root, real)):
                try:
                    res = resolve_inside(base, cand, allow_links=True)
                except PathError:
                    continue
                if _is_script_owned(res.parts):
                    return block(PREFIX, _SCRIPT_OWNED_MESSAGE)
        except OSError:
            pass
        return 0
    except Exception:
        return 0


def net_extension(name: str) -> str:
    """.NET Path.GetExtension: from the last '.', also for a leading dot; empty if the dot is the last character."""
    i = name.rfind(".")
    if i < 0 or i == len(name) - 1:
        return ""
    return name[i:]


def _strip_xml_comments(text: str) -> str:
    """Linear (str.find only): an unclosed comment swallows the rest, so repeated openers cannot cost quadratic time."""
    out = []
    i = 0
    while True:
        j = text.find("<!--", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        k = text.find("-->", j + 4)
        if k < 0:
            break
        i = k + 3
    return "".join(out)


def _is_test_project(root, parts) -> bool:
    """True when parts name an existing regular .csproj inside the project (no links) whose first 256 KB reference a test framework
    package (Microsoft.NET.Test.Sdk, xunit*, NUnit*, MSTest*, TUnit) in a PackageReference or in the Sdk attribute. Production
    projects never qualify: a run folder that comes with a cloned repository cannot turn production code into a test folder."""
    try:
        res = resolve_inside(root, "/".join(parts))
        if not res.exists:
            return False
        data = jsonio.read_bytes_capped(res.path, CSPROJ_READ_CAP)          # regular file only; larger files are not trusted
        text = _strip_xml_comments(data.decode("utf-8-sig", errors="replace"))
        for m in _ATTR_VALUE_RX.finditer(text):
            value = (m.group(1) or m.group(2) or "").strip()
            if _TEST_FRAMEWORK_RX.match(value):
                return True
    except Exception:
        return False
    return False


def _test_project_base(production_csproj: str):
    """Folder name of the scaffolded test project for a production project path (same naming as `dtt scaffold-test-project`)."""
    from ..cmd import scaffold
    name = [p for p in re.split(r"[\\/]", production_csproj) if p][-1]
    base = scaffold.test_project_name(os.path.splitext(name)[0])
    return base


def _run_test_dirs(root, run, deadline):
    """Test project directories (tuples of path parts below the root) that the test-writer may use in this run, from the run's
    00-environment.json (script-written: no agent and not the main conversation may write it): strategy.existingTestProject, the
    test projects that reference the target (the user may pick one of several), or, for a scaffold strategy, tests/<Name>.Tests of the
    production project. Each must be an existing test project (a .csproj that references a test framework)."""
    found = []
    try:
        env_path = resolve_inside(root, ".test-generator/runs/%s/00-environment.json" % run.run_id)
        if not env_path.exists:
            return found
        from ..core import runs
        env = jsonio.read_json_file(env_path.path, runs.ENV_FILE_CAP_BYTES)     # bigger files are skipped, never trusted
        if not isinstance(env, dict):
            return found
        candidates = []
        strategy = env.get("strategy")
        if isinstance(strategy, dict):
            if isinstance(strategy.get("existingTestProject"), str):
                candidates.append(strategy["existingTestProject"])
            if strategy.get("mode") == "scaffold":
                projects = env.get("projects")
                first = projects[0] if isinstance(projects, list) and projects else None
                if isinstance(first, dict) and isinstance(first.get("path"), str) and first["path"].lower().endswith(".csproj"):
                    base = _test_project_base(first["path"])
                    if base:
                        candidates.append("tests/%s.Tests/%s.Tests.csproj" % (base, base))
        tps = env.get("testProjects")
        for tp in (tps if isinstance(tps, list) else []):
            if isinstance(tp, dict) and tp.get("referencesTarget") is True:
                cand = tp.get("exactPath") if isinstance(tp.get("exactPath"), str) else tp.get("path")
                if isinstance(cand, str):
                    candidates.append(cand)
        for cand in candidates[:20]:
            parts = [p for p in re.split(r"[\\/]", cand) if p not in ("", ".")]
            if not parts or ".." in parts or not parts[-1].lower().endswith(".csproj"):
                continue
            directory = tuple(parts[:-1])
            if not directory or _fold(directory[0]) == TG:
                continue            # a project at the repository root would make the whole repository a test folder
            if time.monotonic() > deadline:
                break
            if directory in found or not _is_test_project(root, parts):
                continue
            found.append(directory)
    except Exception:
        pass
    return found


def _writer_runs(root, segments, exists):
    """Run ids of the open runs that allow the test-writer to write this path: it lies inside one of the run's test project
    directories and, when the file already exists, the run's written-files.txt lists it (the writer's own file; a file of the user
    is never overwritten). Empty = not allowed."""
    from ..core import runs
    out = []
    try:
        deadline = runs.new_deadline()
        folded = [_nfold(s) for s in segments[:-1]]
        rel_key = _nfold("/".join(segments))
        for run in runs.get_open_runs_bounded(root, deadline=deadline):
            if time.monotonic() > deadline:
                break           # budget used up: nothing more is allowed
            inside = any(len(folded) >= len(d) and folded[:len(d)] == [_nfold(x) for x in d]
                         for d in _run_test_dirs(root, run, deadline))
            if not inside:
                continue
            if exists and rel_key not in {_nfold(e) for e in writtenfiles.load(run.run_path)}:
                continue
            out.append(run.run_id)
    except Exception:
        return []
    return out


def _run_accepts_scenarios(root, run_id) -> bool:
    """scenario-analyst writes 01-scenarios.json only while the run is open and its step is analyze or approval."""
    try:
        res = resolve_inside(root, ".test-generator/runs/%s/state.json" % run_id)
        if not res.exists:
            return False
        obj = jsonio.read_json_file(res.path, jsonio.MAX_STATE_BYTES)
        return (isinstance(obj, dict) and obj.get("status") == "open" and isinstance(obj.get("step"), str)
                and obj["step"] in ("analyze", "approval"))
    except Exception:
        return False


def _denied_test_file(name: str) -> bool:
    low = _fold(name)
    return low in _DENIED_NAMES or low.startswith("directory.build.") or low.endswith(_DENIED_SUFFIXES)


def _record_written(root, segments, run_ids) -> None:
    """The write is allowed: remember the test file for `dtt run` in the runs that allowed it. Never changes the verdict."""
    try:
        rel = "/".join(segments)
        for run_id in run_ids:
            writtenfiles.record(root, run_id, rel)
    except Exception:
        pass


def _oversize_decision() -> int:
    """Hook input over the size limit whose agent could not be identified (it may or may not be one of this plugin's agents).
    Deny when this project has a .test-generator/runs folder (a plugin run may be open: no 16 MiB test file exists), else allow."""
    try:
        root = paths.get_project_root(None)
        has_runs = os.path.lexists(fs_path(os.path.join(root, ".test-generator", "runs")))
    except Exception:
        return 0
    if has_runs:
        return block(PREFIX, "the hook input is larger than the size limit and cannot be attributed to an agent, so the call is refused "
                             "while a test-generation run folder exists.")
    return 0


def main() -> int:
    payload = None
    try:
        payload, flag = jsonio.read_hook_payload()
        if flag == "unparsed-ours":
            return block(PREFIX, "the hook input could not be read, so the call is refused.")
        if flag == "oversize":
            return _oversize_decision()
        if payload is None:
            return 0
        name = our_agent_name(payload)
        if flag == "hostile" and name is not None:
            return block(PREFIX, "the hook input is nested too deeply, so the call is refused.")
        if name is None:
            return _main_conversation_check(payload)

        if name not in ("test-writer", "scenario-analyst"):
            return block(PREFIX, "agent '%s' is not allowed to write files." % (name if name != st.RESTRICTED_AGENT else "unknown plugin agent"))

        file_path = _path_value(payload)
        if file_path is None or not file_path.strip():
            return block(PREFIX, "the tool call has no file path.")

        root = paths.get_project_root(None)
        try:
            res = resolve_inside(root, file_path, new_name_rules=True)
        except PathError as exc:
            return block(PREFIX, str(exc))
        segments = list(res.parts)
        if not segments:
            return block(PREFIX, "the project root itself is not a file.")

        if res.exists and os.path.isfile(fs_path(res.path)):
            try:
                links = os.stat(fs_path(res.path)).st_nlink
            except OSError:
                links = 1
            if links > 1:
                return block(PREFIX, "the file has more than one hard link, so writing it could change another file.")

        # Alternate data streams, device names, trailing dot/space and 8.3 names were already refused by resolve_inside
        # (Windows: every segment; elsewhere: names that do not exist yet).
        if any(_fold(seg) in _FORBIDDEN_DIRS for seg in segments):
            return block(PREFIX, "writing into .git, .claude, bin or obj is not allowed.")

        in_run = (len(segments) >= 4 and _fold(segments[0]) == TG and _fold(segments[1]) == "runs"
                  and st.is_run_id(segments[2]))

        if name == "scenario-analyst":
            if in_run and len(segments) == 4 and segments[3] == "01-scenarios.json":
                if _run_accepts_scenarios(root, segments[2]):
                    return 0
                return block(PREFIX, "scenario-analyst may write 01-scenarios.json only while the run is open and in the analyze or approval step.")
            return block(PREFIX, "scenario-analyst may only write .test-generator/runs/<id>/01-scenarios.json.")

        # test-writer
        if in_run:
            p3 = _fold(segments[3])
            if p3 in ("snapshot", "backups") or p3.startswith("state.json") or p3.startswith("00-environment.json") or p3.startswith("written-files.txt") or p3.startswith("db.json"):
                return block(PREFIX, "state.json, 00-environment.json, db.json, snapshot of a run are managed by scripts only.")
            if len(segments) == 4 and _WRITER_NOTE_RX.fullmatch(segments[3]):
                if p3.startswith("03-round-"):
                    return block(PREFIX, "03-round-*.md are the orchestrator's round records; test-writer may not write them.")
                return 0
            return block(PREFIX, "test-writer may only write its notes 03-*.md directly in the run folder.")
        if _fold(segments[0]) == TG:
            return block(PREFIX, "writes into .test-generator are restricted to run folders.")

        ext = net_extension(segments[-1]).lower()
        if ext not in EXT_ALLOWED:
            return block(PREFIX, "file type '%s' is not allowed for test-writer (.cs, .json, .txt, .csv only)." % safe_ascii(ext, 20))
        if _denied_test_file(segments[-1]):
            return block(PREFIX, "project, SDK, NuGet and launch configuration files (global.json, launchSettings.json, Directory.Build.*, "
                                 "*.props, *.targets, *.rsp ...) are not test files.")
        existing = res.exists
        run_ids = _writer_runs(root, segments, existing)         # last, bounded: reads files from the (untrusted) repository
        if not run_ids:
            if existing:
                return block(PREFIX, "test-writer may not change a file that exists and was not written by it in this run; "
                                     "new tests go into new files in the test project directory of this run.")
            return block(PREFIX, "test-writer may only write inside the test project directory of this run (see 00-environment.json).")
        _record_written(root, segments, run_ids)
        return 0
    except Exception as exc:
        # Fail closed for this plugin's own write-restricted agents: an unexpected error must not open a hole.
        if our_agent_name(payload) is None:
            return 0
        return block(PREFIX, "could not evaluate the request: " + safe_ascii(str(exc), 200))
