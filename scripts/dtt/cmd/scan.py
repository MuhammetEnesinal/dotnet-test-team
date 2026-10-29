"""scan: the one read-only first step of a run. Creates <project>/.test-generator/runs/<run-id>/ (state.json, snapshot/ of every target,
00-environment.json) and prints a summary of at most 25 lines.

It never starts a process (no dotnet), never uses the network, never builds, restores or evaluates MSBuild. Project files are parsed
as plain XML (DTDs refused, size capped), NuGet.Config files are read only to LIST the package sources. Existing test files are never opened (the test stack comes from
package references only). The database block reads docker-compose image lines and the production projects' .cs files as data (core/dbdetect.py, bounded). Everything that comes from the project is data: it is sanitised before it is
printed and only appears in structured fields or in cleaned, length-capped summary lines, never in the fixed sentences of blockers and
warnings. Exit code: 0 run created, 1 run created but the target is blocked (see the BLOCKER lines), 1 with a message on stderr when
nothing was created."""
from __future__ import annotations

import fnmatch
import os
import re
import time
import unicodedata
from datetime import datetime

from ..core import dbdetect, dbengines, jsonio, nugetcfg, state as st
from ..core.hashing import file_sha256
from ..core.paths import fs_path, is_link_stat, is_session_root, resolve_inside
from ..core.textsafe import clean_text
from ._common import CmdError, emit, project_root, resolve, target_missing_message

MAX_XML_BYTES = 5 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 128 * 1024 * 1024        # all project files together
MAX_SECONDS = 30.0
MAX_OUTPUT_LINES = 25
MAX_LINE_CHARS = 220
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PACKAGES_JSON = os.path.join(PLUGIN_ROOT, "data", "packages.json")
SKIP_NAMES = ("bin", "obj", "node_modules", ".git", ".test-generator", ".vs", ".idea")
UNTRUSTED_FIELDS = ["projects[].path", "testProjects[].path", "testProjects[].exactPath", "features.solutions", "features.directoryBuildFiles",
                    "nuget.sources", "strategy.existingTestProject", "buildTimeCode"]
MEMORY_TEMPLATE = (
    "# dotnet-test-team project memory\n"
    "\n"
    "Facts about this project that make later runs faster (facts only, never commands). Edited by the test-generator skill; safe to edit by hand.\n"
    "\n"
    "- Test framework and packages in use:\n"
    "- Test project path:\n"
    "- Target framework of the test project:\n"
    "- Known pitfalls (culture, time zone, slow tests, flaky areas):\n"
    "- Decisions you made in earlier runs (kept so they are not asked again):\n"
)
# Package id (lower case, fnmatch) -> hint name from this fixed table. The printed hint is never text from the project.
DB_HINTS = (("npgsql*", "npgsql"), ("microsoft.data.sqlclient", "sqlserver-client"), ("system.data.sqlclient", "sqlserver-client"),
            ("microsoft.data.sqlite*", "sqlite-client"), ("system.data.sqlite*", "sqlite-client"), ("mysqlconnector", "mysql-client"),
            ("mysql.data", "mysql-client"), ("oracle.manageddataaccess*", "oracle-client"), ("dapper*", "dapper"), ("mongodb.driver", "mongodb"),
            ("stackexchange.redis", "redis"), ("microsoft.azure.cosmos", "cosmos"), ("testcontainers*", "testcontainers"))
EF_PROVIDER_PRIORITY = ("npgsql", "mysql", "oracle", "sqlserver", "sqlite")      # first one present wins, whatever the package order
BUILD_FILES = ("Directory.Build.props", "Directory.Build.targets", "Directory.Build.rsp", "Directory.Solution.props", "Directory.Solution.targets")



def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--target", required=True, help='one or more project-relative .cs paths joined with ";"')
    p.add_argument("--mode", choices=st.MODES, default="standard", help="standard or deep (deep runs the analyst on a stronger model)")
    p.add_argument("--run-id", default=None, help="run id to use (default: generated)")


# ---- helpers ----------------------------------------------------------------------------------------
def slug(name):
    """Run-id base from a file name: NFKD, ASCII letters/digits only, '-' between runs, at most 40 characters."""
    base = unicodedata.normalize("NFKD", name).lower()
    base = base.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    if len(base) > 40:
        base = base[:40].strip("-")
    if not base:
        base = "target-" + os.urandom(4).hex()
    return base


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


class _Budget(object):
    """Total bytes and seconds the XML reading of one scan may use (a hostile tree must not make it slow)."""

    def __init__(self):
        self.bytes = 0
        self.start = time.monotonic()
        self.exhausted = False

    def take(self, size):
        self.bytes += size
        if self.bytes > MAX_TOTAL_XML_BYTES or time.monotonic() - self.start > MAX_SECONDS:
            self.exhausted = True
        return not self.exhausted


def read_xml(path, budget=None):
    """Parsed root element. Only UTF-8/ASCII, no DTD/entities (core.xmlsafe), size capped, nothing external is resolved."""
    from ..core import xmlsafe
    size = os.stat(fs_path(path)).st_size
    if size > MAX_XML_BYTES:
        raise ValueError("Project file is larger than 5 MB.")
    if budget is not None and not budget.take(size):
        raise ValueError("Read budget used up.")
    return xmlsafe.parse_xml_bytes(jsonio.read_bytes_capped(path, MAX_XML_BYTES))


def find_all(doc, *names):
    if doc is None:
        return []
    return [el for el in doc.iter() if _local(el.tag) in names]


def get_prop(doc, name):
    if doc is None:
        return None
    for el in doc.iter():
        if _local(el.tag) == name:
            return "".join(el.itertext()).strip()
    return None


def is_true(value):
    return isinstance(value, str) and value.lower() == "true"


def get_flag(value, allowed):
    """enable/disable/true/false values only; anything else is reported as 'other' so that raw text never leaves this module."""
    if value is None:
        return None
    return value if value in allowed else "other"


def get_package_refs(doc):
    out = []
    for n in find_all(doc, "PackageReference", "PackageVersion", "GlobalPackageReference"):
        pid = n.get("Include") or n.get("Update")
        if not pid:
            continue
        out.append({"id": pid, "version": n.get("Version") or n.get("VersionOverride") or ""})
    return out


def find_up_file(start_dir, names, stop_dir):
    cur = start_dir
    stop = os.path.normcase(stop_dir.rstrip("\\/"))
    while True:
        for n in names:
            p = os.path.join(cur, n)
            try:
                if os.path.isfile(fs_path(p)) and not is_link_stat(os.lstat(fs_path(p))):
                    return p
            except OSError:
                pass
        if os.path.normcase(cur.rstrip("\\/")) == stop:
            break
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return None


def find_files_up(start_dir, names, root):
    """Every existing regular (non-link) file called one of `names` in start_dir and each parent up to and including root."""
    found = []
    cur = start_dir
    root_norm = os.path.normcase(root.rstrip("\\/"))
    while True:
        for n in names:
            p = os.path.join(cur, n)
            try:
                if os.path.isfile(fs_path(p)) and not is_link_stat(os.lstat(fs_path(p))):
                    found.append(p)
            except OSError:
                pass
        if os.path.normcase(cur.rstrip("\\/")) == root_norm:
            break
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return found


def get_tfm_info(tfm):
    unknown = {"tfm": "unrecognized", "kind": "unknown", "major": 0, "suffix": ""}
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,32}", tfm or ""):
        return unknown
    # the platform suffix must be one of the known platform names (plus a version): free text after the dash is refused
    m = re.fullmatch(r"net(?P<maj>[0-9]{1,2})\.(?P<min>[0-9])(?P<suffix>-(?:windows|android|ios|maccatalyst|macos|tvos|browser|linux|tizen)[0-9.]{0,16})?", tfm)
    if m:
        return {"tfm": tfm, "kind": "net", "major": int(m.group("maj")), "suffix": m.group("suffix") or ""}
    if re.fullmatch(r"net(4[0-9]{1,2})", tfm, re.I):
        return {"tfm": tfm, "kind": "netfx", "major": 4, "suffix": ""}
    if re.fullmatch(r"netstandard[0-9.]{0,4}", tfm, re.I):
        return {"tfm": tfm, "kind": "netstandard", "major": 0, "suffix": ""}
    if re.fullmatch(r"netcoreapp[0-9.]{0,4}", tfm, re.I):
        return {"tfm": tfm, "kind": "netcoreapp", "major": 0, "suffix": ""}
    return unknown


def get_tfms(doc, project_file, root, budget=None):
    tfm = get_prop(doc, "TargetFrameworks") or get_prop(doc, "TargetFramework")
    source = "csproj"
    if not tfm:
        dbp = find_up_file(os.path.dirname(project_file), ["Directory.Build.props"], root)
        if dbp:
            try:
                d = read_xml(dbp, budget)
                tfm = get_prop(d, "TargetFrameworks") or get_prop(d, "TargetFramework")
                source = "Directory.Build.props"
            except Exception:
                pass
    if not tfm:
        return {"tfms": [], "source": "unknown"}
    items = [t.strip() for t in tfm.split(";")[:8]]
    return {"tfms": [t for t in items if t], "source": source}


def get_test_stack(refs, doc):
    ids = [r["id"].lower() for r in refs]

    def has(pattern):
        return any(fnmatch.fnmatchcase(i, pattern) for i in ids)

    framework, variant = "unknown", ""
    if has("xunit.v3.mtp-off*"):
        framework, variant = "xunit-v3", "mtp-off"
    elif has("xunit.v3.mtp-v1*"):
        framework, variant = "xunit-v3", "mtp-v1"
    elif has("xunit.v3.mtp-v2*"):
        framework, variant = "xunit-v3", "mtp-v2"
    elif has("xunit.v3*"):
        framework, variant = "xunit-v3", "mtp-v2"
    elif has("xunit") or has("xunit.core") or has("xunit.abstractions"):
        framework = "xunit-v2"
    elif has("nunit"):
        framework = "nunit"
    elif has("mstest") or has("mstest.testframework") or has("mstest.sdk"):
        framework = "mstest"
    elif has("tunit*"):
        framework = "tunit"
    runner = "vstest"
    if variant in ("mtp-v1", "mtp-v2"):
        runner = "mtp"
    if has("microsoft.testing.platform*") or has("mstest.sdk") or has("tunit*"):
        runner = "mtp"
    if any(is_true(get_prop(doc, p)) for p in ("UseMicrosoftTestingPlatformRunner", "EnableMSTestRunner", "EnableNUnitRunner", "TestingPlatformDotnetTestSupport")):
        runner = "mtp"
    # MSTest.Sdk is named in the Sdk attribute (no PackageReference); it runs on Microsoft.Testing.Platform unless UseVSTest is true and
    # brings the TRX report extension itself (unless switched off).
    trx_builtin = False
    sdk_attr = doc.get("Sdk") if doc is not None else None
    if isinstance(sdk_attr, str) and sdk_attr.lower().startswith("mstest.sdk"):
        framework = "mstest"
        if is_true(get_prop(doc, "UseVSTest")):
            runner = "vstest"
        else:
            runner = "mtp"
            trx_builtin = (get_prop(doc, "EnableMicrosoftTestingExtensionsTrxReport") or "").lower() != "false"
    assertion = "built-in"
    if has("fluentassertions"):
        assertion = "FluentAssertions"
    elif has("awesomeassertions"):
        assertion = "AwesomeAssertions"
    elif has("shouldly"):
        assertion = "Shouldly"
    mock = "none"
    if has("moq"):
        mock = "Moq"
    elif has("nsubstitute"):
        mock = "NSubstitute"
    elif has("fakeiteasy"):
        mock = "FakeItEasy"
    helpers = [h for h in ("autofixture", "bogus", "autofac.extras.moq", "verify.xunit", "respawn", "testcontainers") if has(h)]
    coverage = "none"
    if has("coverlet.collector"):
        coverage = "coverlet.collector"
    elif has("coverlet.msbuild"):
        coverage = "coverlet.msbuild"
    elif has("coverlet.mtp"):
        coverage = "coverlet.mtp"
    elif has("microsoft.testing.extensions.codecoverage"):
        coverage = "CodeCoverage"
    return {"framework": framework, "variant": variant, "runner": runner, "assertion": assertion, "mocking": mock,
            "helpers": helpers, "coverage": coverage, "trxReportExtension": has("microsoft.testing.extensions.trxreport") or trx_builtin}


def get_build_flags(doc):
    """Which build-time code mechanisms a project/props file contains (fixed names): custom Target, Exec task, UsingTask (inline task
    code), Pre/PostBuildEvent command, an SDK that is not a Microsoft.NET.Sdk one (resolved from NuGet)."""
    flags = set()
    sdk = doc.get("Sdk") if doc is not None else None
    if sdk and not sdk.startswith("Microsoft.NET.Sdk"):
        flags.add("custom-sdk")
    for el in doc.iter():
        name = _local(el.tag)
        if name == "Target":
            flags.add("Target")
        elif name == "Exec":
            flags.add("Exec")
        elif name == "UsingTask":
            flags.add("UsingTask")
        elif name in ("PreBuildEvent", "PostBuildEvent") and "".join(el.itertext()).strip():
            flags.add("BuildEvent")
        elif name == "Sdk" and el.get("Name") and not el.get("Name").startswith("Microsoft.NET.Sdk"):
            flags.add("custom-sdk")
    return sorted(flags)


def find_project_files(root, limit):
    """Directory walk that never follows links and never descends into excluded folders."""
    found = []
    truncated = False
    stack = [root]
    while stack and not truncated:
        d = stack.pop()
        try:
            with os.scandir(fs_path(d)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if is_link_stat(e.stat(follow_symlinks=False)):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name.casefold() not in SKIP_NAMES:
                        stack.append(os.path.join(d, e.name))
                    continue
            except OSError:
                continue
            if e.name.lower().endswith(".csproj"):
                if len(found) >= limit:
                    truncated = True
                    break
                found.append(os.path.join(d, e.name))
    return found, truncated


def wildcard_match(pattern, text):
    """Linear-time case-insensitive glob where '*' matches any run of characters (no regex, no backtracking blow-up)."""
    p, t = pattern.lower(), text.lower()
    pi = ti = 0
    star, mark = -1, 0
    while ti < len(t):
        if pi < len(p) and p[pi] == "*":
            star, mark = pi, ti
            pi += 1
        elif pi < len(p) and p[pi] == t[ti]:
            pi += 1
            ti += 1
        elif star >= 0:
            pi = star + 1
            mark += 1
            ti = mark
        else:
            return False
    while pi < len(p) and p[pi] == "*":
        pi += 1
    return pi == len(p)


def rel_to(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _project_ref_paths(doc, project_file):
    """Absolute paths of ProjectReference includes that contain no MSBuild property / wildcard syntax."""
    out = []
    for pr in find_all(doc, "ProjectReference"):
        inc = (pr.get("Include") or "").replace("\\", "/")
        if not inc or re.search(r"[$%@;*?]", inc):
            continue
        out.append(os.path.abspath(os.path.join(os.path.dirname(project_file), inc)))
    return out


def _find_csproj_for(root, start, warnings):
    """The project file that owns `start` (a file or folder): the first .csproj (alphabetical) in its folder or the nearest parent."""
    cur = start if os.path.isdir(fs_path(start)) else os.path.dirname(start)
    root_len = len(root)
    while len(cur) >= root_len:
        try:
            names = sorted((n for n in os.listdir(fs_path(cur)) if n.lower().endswith(".csproj")), key=str.lower)
        except OSError:
            names = []
        names = [n for n in names if os.path.isfile(fs_path(os.path.join(cur, n))) and not is_link_stat(os.lstat(fs_path(os.path.join(cur, n))))]
        if names:
            if len(names) > 1:
                warnings.append("MULTIPLE_PROJECTS_IN_FOLDER: several project files are in the target folder; the first one in alphabetical order was used. Pass the .csproj as target to choose.")
            return os.path.join(cur, names[0])
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return None


def strip_json_comments(text):
    """Text without // and /* */ comments (outside of strings); the dotnet SDK accepts them in global.json, the strict JSON reader does not."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def read_global_json(path):
    """global.json as a dict-or-whatever; comments are tolerated like the SDK does (size and depth limits stay those of jsonio)."""
    try:
        return jsonio.read_json_file(path, 1024 * 1024)
    except jsonio.JsonError:
        data = jsonio.read_bytes_capped(path, 1024 * 1024)
        return jsonio.loads_strict(strip_json_comments(data.decode("utf-8-sig")))


# ---- the environment findings ---------------------------------------------------------------------------
def analyse(root, targets):
    """All read-only findings for the first target's project. Returns the dict that is saved as 00-environment.json."""
    blockers, warnings = [], []
    budget = _Budget()
    target_full = targets[0].path

    # ---- target project file
    target_csproj = _find_csproj_for(root, target_full, warnings)
    if len(targets) > 1:
        owners = set()
        for tr in targets[1:]:
            other = _find_csproj_for(root, tr.path, [])
            owners.add(os.path.normcase(other) if other else None)
        if owners - {os.path.normcase(target_csproj) if target_csproj else None}:
            warnings.append("MULTIPLE_TARGET_PROJECTS: the target files belong to different projects; only the first target's project is examined.")
    if not target_csproj:
        blockers.append("NO_PROJECT: no .csproj was found for the target. Point the skill at a file inside a C# project.")

    # ---- global.json
    global_json = {"path": None, "version": None, "rollForward": None, "testRunner": None, "parseError": None}
    stop = os.path.splitdrive(root)[0] + os.sep if os.path.splitdrive(root)[0] else os.sep
    gj = find_up_file(root, ["global.json"], stop)
    gj_flags = []
    if gj:
        global_json["path"] = "global.json"
        try:
            g = read_global_json(gj)
            sdk = g.get("sdk") if isinstance(g, dict) else None
            if isinstance(sdk, dict) and "version" in sdk:
                v = str(sdk["version"])
                global_json["version"] = v if re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}(-[A-Za-z0-9.]{1,30})?", v) else "unrecognized"
            if isinstance(sdk, dict) and "rollForward" in sdk:
                global_json["rollForward"] = st.enum_or_unknown(str(sdk["rollForward"]), ("patch", "feature", "minor", "major", "latestPatch", "latestFeature", "latestMinor", "latestMajor", "disable"))
            if isinstance(sdk, dict) and "paths" in sdk:
                gj_flags.append("sdk-paths")
            if isinstance(g, dict) and "msbuild-sdks" in g:
                gj_flags.append("msbuild-sdks")
            test = g.get("test") if isinstance(g, dict) else None
            if isinstance(test, dict) and "runner" in test:
                rn = str(test["runner"])
                rn = rn.lower()           # the SDK compares case-insensitively (verified on 10.0.402)
                global_json["testRunner"] = "Microsoft.Testing.Platform" if rn == "microsoft.testing.platform" else ("VSTest" if rn == "vstest" else "other")
        except Exception:
            global_json["parseError"] = "global.json could not be parsed (comments or invalid JSON)."
            warnings.append("GLOBAL_JSON_UNPARSED: global.json exists but could not be parsed.")

    # ---- target project
    projects = []
    target_doc = None
    tfm_infos = []
    build_time = []
    if target_csproj:
        try:
            target_doc = read_xml(target_csproj, budget)
        except Exception:
            blockers.append("PROJECT_UNREADABLE: the target project file could not be parsed as plain XML (or is too large). Files with a DTD are refused on purpose.")
        if target_doc is not None:
            t = get_tfms(target_doc, target_csproj, root, budget)
            tfm_infos = [get_tfm_info(x) for x in t["tfms"]]
            if not tfm_infos:
                warnings.append("TFM_UNKNOWN: the target framework could not be determined from the project file or Directory.Build.props.")
            projects.append({
                "path": clean_text(rel_to(root, target_csproj), 200), "tfmSource": t["source"],
                "targetFrameworks": [{"tfm": ti["tfm"], "kind": ti["kind"], "major": ti["major"], "windows": ti["suffix"].lower().startswith("-windows")}
                                     for ti in tfm_infos],
                "nullable": get_flag(get_prop(target_doc, "Nullable"), ("enable", "disable", "warnings", "annotations")),
                "implicitUsings": get_flag(get_prop(target_doc, "ImplicitUsings"), ("enable", "disable", "true", "false")),
            })

    # ---- repository features
    all_csproj, truncated = find_project_files(root, 300)
    all_csproj.sort(key=lambda f: rel_to(root, f).casefold())          # a stable order: the first matching test project is reproducible
    if truncated:
        warnings.append("MANY_PROJECTS: more than 300 project files were found; only the first 300 were inspected, so an existing test project may have been missed. Pass a narrower project root.")
    sols = []
    with os.scandir(fs_path(root)) as it:
        for e in sorted(it, key=lambda e: e.name):
            if e.is_file(follow_symlinks=False) and e.name.lower().endswith((".sln", ".slnx")) and not is_link_stat(e.stat(follow_symlinks=False)):
                sols.append(clean_text(e.name, 100))
    cpm_file = find_up_file(root, ["Directory.Packages.props"], root)
    cpm = False
    cpm_versions = {}
    if cpm_file:
        try:
            d = read_xml(cpm_file, budget)
            cpm = is_true(get_prop(d, "ManagePackageVersionsCentrally"))
            for r in get_package_refs(d):
                if r["version"]:
                    cpm_versions[r["id"].lower()] = r["version"]
        except Exception:
            pass
    start_dir = os.path.dirname(target_csproj) if target_csproj else root
    bf_paths = find_files_up(start_dir, BUILD_FILES, root)
    dbp_files = [p for p in bf_paths if os.path.basename(p) in ("Directory.Build.props", "Directory.Build.targets")]
    twae = False
    lock_files = any(os.path.isfile(fs_path(os.path.join(os.path.dirname(f), "packages.lock.json"))) for f in all_csproj)
    for f in bf_paths:
        flags = []
        if not f.lower().endswith(".rsp"):
            try:
                d = read_xml(f, budget)
                flags = get_build_flags(d)
                if is_true(get_prop(d, "TreatWarningsAsErrors")):
                    twae = True
                if is_true(get_prop(d, "RestorePackagesWithLockFile")):
                    lock_files = True
            except Exception:
                flags = ["unreadable"]
        build_time.append({"path": clean_text(rel_to(root, f), 160), "kind": "msbuild-import", "flags": flags})
    if target_doc is not None and is_true(get_prop(target_doc, "TreatWarningsAsErrors")):
        twae = True
    nuget_cfg = find_up_file(root, ["nuget.config", "NuGet.Config", "NuGet.config"], root)
    mapping = False
    mapping_patterns = []
    if nuget_cfg:
        try:
            d = read_xml(nuget_cfg, budget)
            mapping = len(find_all(d, "packageSourceMapping")) > 0
            if mapping:
                for ps in find_all(d, "packageSource"):
                    for pk in ps:
                        pat = pk.get("pattern") if _local(pk.tag) == "package" else None
                        if pat and len(pat) <= 200 and len(mapping_patterns) < 200:
                            mapping_patterns.append(pat)
        except Exception:
            pass
    features = {
        "solutions": sols, "centralPackageManagement": cpm, "lockFiles": lock_files,
        "nugetConfig": bool(nuget_cfg), "packageSourceMapping": mapping,
        "directoryBuildFiles": [clean_text(os.path.basename(f), 40) for f in dbp_files],
        "treatWarningsAsErrors": twae,
    }
    if mapping:
        warnings.append("SOURCE_MAPPING: nuget.config uses packageSourceMapping; the generated test packages must be mapped to a source or restore fails (NU1100).")
    if lock_files:
        warnings.append("LOCK_FILES: the repository uses packages.lock.json; the generated test project opts out of locked restore.")
    if twae:
        warnings.append("WARNINGS_AS_ERRORS: warnings are errors in this repository; the generated test project overrides this for itself only.")
    if len(sols) > 1:
        warnings.append("MULTIPLE_SOLUTIONS: several solution files exist; the skill will pass the solution path explicitly and not guess.")

    # ---- NuGet sources (listed from the config files, never contacted) and the other build-time files
    entries, _problems = nugetcfg.effective_sources(root)
    nuget = {"sources": [{"name": clean_text(e["key"], 60), "url": clean_text(nugetcfg.redact_source(e["value"]), 160), "enabled": bool(e["enabled"])}
                         for e in entries[:20]], "truncated": len(entries) > 20}
    for cfg in nugetcfg.config_files(root):
        if cfg.startswith(root + os.sep):
            build_time.append({"path": clean_text(rel_to(root, cfg), 160), "kind": "nuget-config", "flags": []})
    if gj:
        inside = gj.startswith(root + os.sep)
        build_time.append({"path": "global.json" if inside else "global.json (parent folder)", "kind": "global-json", "flags": gj_flags})
    tools = os.path.join(root, ".config", "dotnet-tools.json")
    try:
        if os.path.isfile(fs_path(tools)) and not is_link_stat(os.lstat(fs_path(tools))):
            build_time.append({"path": ".config/dotnet-tools.json", "kind": "dotnet-tools", "flags": []})
    except OSError:
        pass

    # ---- EF Core and database hints in the target (and its direct project references): the production projects
    ef = {"detected": False, "version": None, "provider": None, "sqliteRisk": None}
    db_hints = set()
    providers = set()
    prod_ids = set()
    prod_dirs = []

    def add_ef_from(doc):
        for ref in get_package_refs(doc):
            rid = ref["id"].lower()
            ver = ref["version"]
            prod_ids.add(rid)
            for pattern, hint in DB_HINTS:
                if fnmatch.fnmatchcase(rid, pattern):
                    db_hints.add(hint)
            if not ver and rid in cpm_versions:
                ver = cpm_versions[rid]
            if fnmatch.fnmatchcase(rid, "microsoft.entityframeworkcore*") and not rid.endswith(".design") and not rid.endswith(".tools"):
                ef["detected"] = True
                m = re.fullmatch(r"\[?([0-9]+\.[0-9]+\.[0-9]+)\]?", ver or "")
                if ver and m and not ef["version"] and rid in ("microsoft.entityframeworkcore", "microsoft.entityframeworkcore.relational",
                                                                "microsoft.entityframeworkcore.sqlserver", "microsoft.entityframeworkcore.sqlite",
                                                                "microsoft.entityframeworkcore.inmemory"):
                    ef["version"] = m.group(1)
            # the provider does not depend on the order of the packages: see EF_PROVIDER_PRIORITY below
            if rid.startswith("npgsql.entityframeworkcore"):
                ef["detected"] = True
                providers.add("npgsql")
            elif rid.startswith("pomelo.entityframeworkcore") or rid.startswith("mysql.entityframeworkcore"):
                ef["detected"] = True
                providers.add("mysql")
            elif rid == "microsoft.entityframeworkcore.sqlserver":
                providers.add("sqlserver")
            elif rid == "microsoft.entityframeworkcore.sqlite":
                providers.add("sqlite")
            elif rid.startswith("oracle.entityframeworkcore"):
                ef["detected"] = True
                providers.add("oracle")

    if target_doc is not None:
        add_ef_from(target_doc)
        prod_dirs.append(os.path.dirname(target_csproj))
        for ref_path in _project_ref_paths(target_doc, target_csproj):
            try:
                res = resolve_inside(root, ref_path)
                if res.exists and os.path.isfile(fs_path(res.path)):
                    add_ef_from(read_xml(res.path, budget))
                    prod_dirs.append(os.path.dirname(res.path))
            except Exception:
                pass
        if ef["detected"]:
            ef["provider"] = next((p for p in EF_PROVIDER_PRIORITY if p in providers), "unknown")
            # SQLite can only imitate a provider so far: models with provider-specific column types, functions or arrays may not even create there.
            ef["sqliteRisk"] = "higher" if ef["provider"] in ("npgsql", "mysql", "oracle") else ("medium" if ef["provider"] in ("sqlserver", "unknown") else "low")
            if not ef["version"]:
                warnings.append("EF_CORE_VERSION_UNKNOWN: EF Core is used but its exact version could not be read from the project files; an EF test database package cannot be matched automatically. A real throw-away database in Docker (dtt db check) does not need the version.")
            else:
                warnings.append("EF_CORE_DETECTED: the target uses EF Core %s (provider: %s). Tests of code that runs EF queries need a decision: a real throw-away database in Docker (dtt db check), SQLite, a small hand-written helper or mocks only; see the skill (test database)." % (ef["version"], ef["provider"]))

    # ---- the database the tests could run against (static reading only)
    db = {"engine": None, "tag": None, "tagFrom": None, "recipe": False, "needsEula": False, "migrations": False, "features": []}
    if target_doc is not None:
        try:
            table = dbengines.load()
            engine = dbdetect.detect_engine(prod_ids, table)
            repos = sorted(set(r for e in table.values() if e["recipe"] for r in e["composeRepos"]))
            images = dbdetect.compose_images(root, os.path.dirname(target_csproj), repos) if engine is not None and table[engine]["recipe"] else []
            engine, tag, tag_from = dbdetect.choose_engine_and_tag(engine, table, images)
            if engine is not None:
                db.update(engine=engine, tag=tag, tagFrom=tag_from, recipe=bool(table[engine]["recipe"]), needsEula=bool(table[engine].get("needsEula")))
        except dbengines.DbTableError:
            warnings.append("DB_TABLE_INVALID: data/db-engines.json could not be read, so no database engine was detected.")
        migrations, counts = dbdetect.scan_sources(prod_dirs)
        db.update(migrations=migrations, features=dbdetect.features_list(counts))

    # ---- all project files: test projects and custom build-time code
    test_projects = []
    pkg_data = jsonio.read_json_file(PACKAGES_JSON, 1024 * 1024)
    known_ids = {}
    for prof in pkg_data["profiles"].values():
        for pp in prof["packages"]:
            known_ids[pp["id"].lower()] = pp["id"]
    known_ids[pkg_data["optional"]["mocking"]["id"].lower()] = pkg_data["optional"]["mocking"]["id"]
    version_votes = {}
    for f in all_csproj:
        if budget.exhausted:
            warnings.append("SCAN_BUDGET: the project files are too large or too slow to read completely; later project files were skipped.")
            break
        try:
            d = read_xml(f, budget)
            flags = get_build_flags(d)
            if flags:
                build_time.append({"path": clean_text(rel_to(root, f), 160), "kind": "csproj", "flags": flags})
            refs = get_package_refs(d)
            my_versions = []
            for r in refs:
                key = r["id"].lower()
                if key not in known_ids:
                    continue
                v = r["version"]
                if not v and key in cpm_versions:
                    v = cpm_versions[key]
                m = re.fullmatch(r"\[?([0-9]+(\.[0-9]+){1,3})\]?", v or "")
                if v and m:
                    my_versions.append({"id": known_ids[key], "version": m.group(1)})
            is_test_flag = is_true(get_prop(d, "IsTestProject"))
            stack = get_test_stack(refs, d)
            if stack["framework"] == "unknown" and not is_test_flag:
                continue
            for mv in my_versions:
                votes = version_votes.setdefault(mv["id"], {})
                votes[mv["version"]] = votes.get(mv["version"], 0) + 1
            t = get_tfms(d, f, root, budget)
            refs_target = False
            if target_csproj:
                target_norm = os.path.normcase(os.path.abspath(target_csproj))
                for full in _project_ref_paths(d, f):
                    if os.path.normcase(full) == target_norm:
                        refs_target = True
                        break
            test_projects.append({
                "path": clean_text(rel_to(root, f), 200),
                "exactPath": rel_to(root, f),
                "tfms": [get_tfm_info(x)["tfm"] for x in t["tfms"]], "framework": stack["framework"], "variant": stack["variant"], "runner": stack["runner"],
                "assertion": stack["assertion"], "mocking": stack["mocking"], "helpers": stack["helpers"], "coverage": stack["coverage"],
                "trxReportExtension": stack["trxReportExtension"],
                "referencesTarget": refs_target,
                "packageVersions": ["%s=%s" % (mv["id"], mv["version"]) for mv in my_versions],
            })
        except Exception:
            continue

    # ---- which runner `dotnet test` will use: only global.json can switch to Microsoft.Testing.Platform, and only the SDK 10+ reads that key
    # (the SDK is not asked here; `dtt run` asks `dotnet --version` and decides). A pinned global.json SDK below 10 that cannot roll
    # forward to a new major means the key is ignored.
    dotnet_test_mode = "mtp" if global_json["testRunner"] == "Microsoft.Testing.Platform" else "vstest"
    pin = re.match(r"([0-9]{1,3})\.", global_json["version"] or "")
    key_ignored = bool(pin) and int(pin.group(1)) < 10 and global_json["rollForward"] not in ("major", "latestMajor")
    pinned_new = bool(pin) and int(pin.group(1)) >= 10
    runner_summary = {"dotnetTestMode": dotnet_test_mode,
                      "reason": "global.json selects Microsoft.Testing.Platform (applies on SDK 10+)" if dotnet_test_mode == "mtp"
                      else "no global.json test runner: VSTest"}
    for idx, tp in enumerate(test_projects, 1):
        if tp["runner"] == "vstest" and dotnet_test_mode == "mtp" and not key_ignored:
            blockers.append("RUNNER_MISMATCH: global.json selects Microsoft.Testing.Platform but existing test project #%d (testProjects[%d]) uses VSTest; 'dotnet test' would refuse to run it." % (idx, idx - 1))
        elif tp["runner"] == "mtp" and key_ignored and tp["referencesTarget"]:
            blockers.append("RUNNER_MISMATCH: existing test project #%d (testProjects[%d]) runs on Microsoft.Testing.Platform but global.json pins an SDK older than 10; dtt run supports that only with SDK 10. Use SDK 10 or a VSTest test project." % (idx, idx - 1))
        elif tp["runner"] == "mtp" and dotnet_test_mode != "mtp" and pinned_new and tp["referencesTarget"]:
            blockers.append("RUNNER_MISMATCH: existing test project #%d (testProjects[%d]) runs on Microsoft.Testing.Platform but global.json (SDK 10+) does not select it; 'dotnet test' would refuse to run it." % (idx, idx - 1))

    # ---- strategy
    strategy = {"mode": None, "profile": None, "framework": None, "runner": None, "testTfm": None, "existingTestProject": None,
                "versionOverrides": [], "reason": None}
    reusable = [tp for tp in test_projects if tp["referencesTarget"]]
    if reusable:
        tp = reusable[0]
        strategy.update(mode="reuse-existing", existingTestProject=tp["exactPath"], framework=tp["framework"], runner=tp["runner"],
                        reason="A test project that references the target already exists (see existingTestProject); its packages are used and nothing is added.")
        if len(reusable) > 1:
            warnings.append("SEVERAL_TEST_PROJECTS: %d test projects reference the target; ask the user which to use. The first one is reported in strategy.existingTestProject." % len(reusable))
        if tp["framework"] not in ("xunit-v2", "xunit-v3", "nunit", "mstest"):
            blockers.append("UNSUPPORTED_FRAMEWORK: the existing test framework '%s' is not supported (supported: xUnit v2/v3, NUnit, MSTest)." % tp["framework"])
        elif tp["runner"] == "mtp" and not tp["trxReportExtension"] and tp["framework"] != "mstest":
            blockers.append("MTP_TRX_EXTENSION_MISSING: the existing test project runs on Microsoft.Testing.Platform but has no Microsoft.Testing.Extensions.TrxReport package, so dtt run cannot read its results. This plugin does not change an existing test project; ask the user to add the package or to use another test project.")
    else:
        strategy["mode"] = "scaffold"
        same_repo = [tp for tp in test_projects if tp["framework"] in ("xunit-v2", "nunit", "mstest", "xunit-v3")][:1]
        test_tfm = None

        def windows_only(ti):
            return ti["suffix"].lower().startswith("-windows")

        usable = sorted((t for t in tfm_infos if t["kind"] == "net" and t["major"] >= 8 and (os.name == "nt" or not windows_only(t))), key=lambda t: -t["major"])
        if usable:
            test_tfm = usable[0]["tfm"]
        elif any(t["kind"] == "netfx" for t in tfm_infos):
            blockers.append("NETFX_UNSUPPORTED: .NET Framework targets are not supported (not tested). Multi-target the library or add an existing test project.")
        elif any(t["kind"] == "net" and t["major"] < 8 for t in tfm_infos) or any(t["kind"] in ("netstandard", "netcoreapp") for t in tfm_infos):
            blockers.append("TFM_TOO_OLD: the target framework is older than net8.0 (or netstandard/netcoreapp). The generated test project needs net8.0+ and an installed runtime; this plugin does not generate tests for such targets.")
        elif any(t["kind"] == "net" and windows_only(t) for t in tfm_infos):
            blockers.append("WINDOWS_ONLY_TFM: the target framework is a -windows one and this computer is not Windows, so its tests cannot be built here.")
        if not test_tfm and not any(t["kind"] != "unknown" for t in tfm_infos) and target_csproj:
            blockers.append("TFM_UNKNOWN: the target framework of the target project could not be read from its project file or Directory.Build.props (an old-style project, a property such as $(TargetFramework), or a conditional setting). Add a plain <TargetFramework>net8.0</TargetFramework> (or newer) to the project, or use an existing test project; this plugin cannot create a matching test project otherwise.")
        if target_csproj and os.path.normcase(os.path.dirname(target_csproj).rstrip("\\/")) == os.path.normcase(root.rstrip("\\/")):
            blockers.append("ROOT_PROJECT: the target project file sits in the repository root, so a new test project below the root would be compiled into it. Add a test project outside the root folder yourself, or move the project into a subfolder; this plugin does not move files.")
        if test_tfm:
            strategy["testTfm"] = test_tfm
            if dotnet_test_mode == "mtp" and not key_ignored:
                strategy.update(profile="mtp-xunitv3", framework="xunit-v3", runner="mtp")
            elif len(same_repo) == 1 and same_repo[0]["runner"] == "vstest":
                mapping_ = {"xunit-v2": "vstest-xunitv2", "nunit": "vstest-nunit", "mstest": "vstest-mstest", "xunit-v3": "vstest-xunitv3"}
                strategy.update(profile=mapping_[same_repo[0]["framework"]], framework=same_repo[0]["framework"], runner="vstest")
                warnings.append("PREFERRED_FRAMEWORK: another test project in the repository uses %s; the new test project follows it. Ask the user to confirm." % same_repo[0]["framework"])
            else:
                strategy.update(profile="vstest-xunitv3", framework="xunit-v3", runner="vstest")
            strategy["reason"] = "No test project references the target; a new one is created from the allowlisted profile."
            # Use the versions the repository already uses for the profile's packages (same id), so the new test project does not pull a
            # different major version of a test library than its neighbours. A package whose versions disagree is left at the plugin's version.
            overrides = []
            profile_pkgs = (pkg_data["profiles"].get(strategy["profile"]) or {}).get("packages")
            if profile_pkgs:
                for pp in profile_pkgs:
                    votes = version_votes.get(pp["id"])
                    if not votes:
                        continue
                    if len(votes) == 1:
                        v = next(iter(votes))
                        if v != pp["version"]:
                            overrides.append("%s=%s" % (pp["id"], v))
                    else:
                        warnings.append("VERSION_CONFLICT: existing test projects use different versions of %s; the plugin's version %s is used for the new test project." % (pp["id"], pp["version"]))
            strategy["versionOverrides"] = overrides
            if overrides:
                warnings.append("ALIGNED_VERSIONS: the new test project uses the same versions as the repository's existing test projects for: %s." % ", ".join(o.split("=")[0] for o in overrides))
    # With packageSourceMapping, every package of the chosen profile must be mapped to some source or restore fails (NU1100).
    if mapping and strategy["mode"] == "scaffold" and strategy["profile"]:
        try:
            unmapped = [p["id"] for p in pkg_data["profiles"][strategy["profile"]]["packages"]
                        if not any(wildcard_match(pat, p["id"]) for pat in mapping_patterns)]
            if unmapped:
                blockers.append("SOURCE_MAPPING_BLOCKS: nuget.config packageSourceMapping has no pattern for: %s. Restore would fail (NU1100); the repository owner has to map them, this plugin does not edit nuget.config." % ", ".join(unmapped))
        except Exception:
            warnings.append("SOURCE_MAPPING_UNCHECKED: nuget.config could not be analysed.")
    if cpm:
        warnings.append("CENTRAL_PACKAGE_MANAGEMENT: the repository uses central package management; a generated test project opts out for itself (versions stay in its own .csproj).")

    return {
        "schemaVersion": 4,
        "root": ".",
        "globalJson": global_json,
        "projects": projects,
        "runner": runner_summary,
        "features": features,
        "nuget": nuget,
        "buildTimeCode": build_time[:60],
        "efCore": ef,
        "dbHints": sorted(db_hints),
        "db": db,
        "testProjects": test_projects,
        "strategy": strategy,
        "blockers": blockers,
        "warnings": warnings,
        "untrustedFields": UNTRUSTED_FIELDS,
    }


# ---- the printed summary ---------------------------------------------------------------------------------
def _line(text):
    return clean_text(text, MAX_LINE_CHARS)


def _joined(prefix, items, empty, sep="; ", limit=MAX_LINE_CHARS - 14):
    """prefix + as many items as fit in one line, then '(+N more)'; items are never cut in the middle."""
    if not items:
        return prefix + empty
    text = prefix
    shown = 0
    for item in items:
        add = (sep if shown else "") + item
        if shown and len(text) + len(add) > limit:
            break
        text += add
        shown += 1
    return text + (" (+%d more)" % (len(items) - shown) if shown < len(items) else "")


def summary_lines(run_id, mode, ntargets, env):
    """At most MAX_OUTPUT_LINES lines. Fixed sentences plus cleaned, length-capped project data."""
    lines = [_line("run: %s | mode: %s | targets: %d" % (run_id, mode, ntargets))]
    pr = env["projects"][0] if env["projects"] else None
    if pr:
        tfms = ", ".join(t["tfm"] for t in pr["targetFrameworks"]) or "unknown"
        lines.append(_line("production project: %s | tfm: %s | nullable: %s" % (pr["path"], tfms, pr["nullable"] or "unset")))
    else:
        lines.append("production project: none found")
    ef = env["efCore"]
    efs = "EF Core %s, provider %s, sqlite risk %s" % (ef["version"] or "version unknown", ef["provider"], ef["sqliteRisk"]) if ef["detected"] else "no EF Core"
    lines.append(_line("data: %s | db hints: %s" % (efs, ", ".join(env["dbHints"]) or "none")))
    db = env.get("db")
    if isinstance(db, dict):
        parts = []
        if db.get("engine"):
            parts.append("engine %s%s" % (db["engine"], " tag %s (%s)" % (db["tag"], db["tagFrom"]) if db.get("tag") else ""))
            parts.append("recipe: " + ("yes" if db.get("recipe") else "no"))
        else:
            parts.append("no known engine")
        parts.append("migrations: " + ("yes" if db.get("migrations") else "no"))
        feats = ["%s=%d" % (f["name"], f["count"]) for f in db.get("features", [])]
        parts.append("features: " + (", ".join(feats) or "none"))
        lines.append(_line("database: " + " | ".join(parts)))
    tps = env["testProjects"]
    lines.append("test projects: %d" % len(tps))
    for tp in tps[:3]:
        lines.append(_line("  %s | %s | %s/%s | assert: %s | mock: %s | coverage: %s | references target: %s" % (
            tp["path"], ",".join(tp["tfms"]) or "tfm unknown", tp["framework"], tp["runner"], tp["assertion"], tp["mocking"], tp["coverage"],
            "yes" if tp["referencesTarget"] else "no")))
    if len(tps) > 3:
        lines.append("  ... %d more test project(s) in 00-environment.json" % (len(tps) - 3))
    s = env["strategy"]
    if s["mode"] == "reuse-existing":
        lines.append(_line("strategy: reuse-existing %s" % clean_text(s["existingTestProject"], 160)))
    else:
        lines.append(_line("strategy: scaffold | profile: %s | test tfm: %s | version overrides: %s" % (
            s["profile"] or "none", s["testTfm"] or "none", ";".join(s["versionOverrides"]) or "none")))
    bt = ["%s%s" % (b["path"], "[" + ",".join(b["flags"]) + "]" if b["flags"] else "") for b in env["buildTimeCode"]]
    lines.append(_line(_joined("build-time code (runs when dotnet builds/restores): ", bt, "none found")))
    src = ["%s=%s%s" % (n["name"], n["url"], "" if n["enabled"] else " (disabled)") for n in env["nuget"]["sources"]]
    lines.append(_line(_joined("nuget sources (listed, not contacted): ", src, "none")))
    for b in env["blockers"][:5]:
        lines.append(_line("BLOCKER " + b))
    if len(env["blockers"]) > 5:
        lines.append("BLOCKER ... %d more in 00-environment.json" % (len(env["blockers"]) - 5))
    # warnings use whatever room is left
    room = MAX_OUTPUT_LINES - 1 - len(lines)
    warn = env["warnings"]
    if warn and room > 0:
        shown = warn[:room] if len(warn) <= room else warn[:room - 1]
        lines.extend(_line("warning " + w) for w in shown)
        if len(warn) > len(shown):
            lines.append("warning ... %d more in 00-environment.json" % (len(warn) - len(shown)))
    lines.append(_line("saved: .test-generator/runs/%s/00-environment.json" % run_id))
    if len(lines) > MAX_OUTPUT_LINES:               # last guard: the cap holds whatever the sections above added
        lines = lines[:MAX_OUTPUT_LINES - 1] + [lines[-1]]
    return lines


# ---- main --------------------------------------------------------------------------------------------
def run(args):
    import shutil
    root = project_root(args)
    if not is_session_root(root):
        raise CmdError("--project-root must be the folder Claude Code was started in (the project directory); a run is not created anywhere else.")
    targets = []
    for t in [x.strip() for x in args.target.split(";") if x.strip()]:
        res = resolve(root, t)
        if not (res.exists and os.path.isfile(fs_path(res.path))):
            if not res.exists:
                raise CmdError("Target file " + target_missing_message(res.root, res.parts, t), unicode_text=True)
            raise CmdError("Target file does not exist: " + clean_text(t, 100), unicode_text=True)
        if not res.parts[-1].lower().endswith(".cs"):
            raise CmdError("Target must be a .cs file.")
        if any(p.casefold() in ("bin", "obj", ".test-generator") for p in res.parts[:-1]):
            raise CmdError("Target must not be inside bin, obj or .test-generator.")
        targets.append(res)
    if not targets:
        raise CmdError("At least one target is required.")
    if len(targets) > 20:
        raise CmdError("At most 20 target files per run.")

    run_id = args.run_id
    if run_id is None or not run_id.strip():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.splitext(targets[0].parts[-1])[0]
        run_id = "%s-%s" % (stamp, slug(base))
    if not st.is_run_id(run_id):
        raise CmdError("Run id must match ^[a-z0-9][a-z0-9-]{0,63}$.")

    env = analyse(root, targets)          # read-only; nothing is created when it fails

    tg_dir = resolve(root, ".test-generator").path
    if not os.path.exists(fs_path(tg_dir)):
        os.mkdir(fs_path(tg_dir))
    ignore = resolve(root, ".test-generator/.gitignore").path
    if not os.path.exists(fs_path(ignore)):
        jsonio.write_atomic(ignore, "*\n")
    memory = resolve(root, ".test-generator/memory.md").path
    if not os.path.exists(fs_path(memory)):
        jsonio.write_atomic(memory, MEMORY_TEMPLATE)
    runs_root = resolve(root, ".test-generator/runs").path
    if not os.path.exists(fs_path(runs_root)):
        os.mkdir(fs_path(runs_root))
    run_res = resolve(root, ".test-generator/runs/" + run_id)
    if os.path.exists(fs_path(run_res.path)):
        raise CmdError("Run already exists: " + run_id)
    os.mkdir(fs_path(run_res.path))
    snap_dir = os.path.join(run_res.path, "snapshot")
    os.mkdir(fs_path(snap_dir))

    entries = []
    for i, res in enumerate(targets, 1):
        copy_name = "%02d-%s" % (i, res.parts[-1])
        copy_path = os.path.join(snap_dir, copy_name)
        shutil.copyfile(fs_path(res.path), fs_path(copy_path))
        # Hash the copy, so the recorded hash always describes exactly the bytes that were saved.
        entries.append({"path": res.rel, "sha256": file_sha256(copy_path), "snapshot": "snapshot/" + copy_name})

    jsonio.write_atomic(os.path.join(run_res.path, "00-environment.json"), jsonio.dumps(env) + "\n")
    jsonio.write_json_atomic(os.path.join(run_res.path, "state.json"), st.new_state(run_id, args.mode, entries))
    emit("\n".join(summary_lines(run_id, args.mode, len(targets), env)) + "\n")
    return 1 if env["blockers"] else 0
