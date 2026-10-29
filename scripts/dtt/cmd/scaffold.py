"""scaffold-test-project: creates a new test project (.csproj only) from a profile in data/packages.json and optionally adds it to the solution.

Package IDs and versions come exclusively from data/packages.json (never from a model). The new project is isolated from repository-wide
settings that would break it (central package management, warnings-as-errors, locked restore, inherited analyzers) by overriding them
inside its own .csproj; no repository file is edited. It does not restore or build anything (only `dotnet sln add` unless --no-solution).
Prints {projectPath, profile, testTfm, solutionUpdated, solution}."""
from __future__ import annotations

import os
import re
import unicodedata

from ..core import jsonio
from ..core.paths import fs_path, is_link_stat
from ..core.textsafe import clean_text, safe_ascii
from ._common import CmdError, emit_json, project_root, resolve

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PACKAGES_JSON = os.path.join(PLUGIN_ROOT, "data", "packages.json")
BANNED_DIRS = (".git", ".github", ".vs", ".idea", "bin", "obj", "node_modules", ".test-generator", ".claude")
_PROFILE_RX = re.compile(r"[a-z0-9-]{1,40}")
_TFM_RX = re.compile(r"net(8|9|1[0-9])\.0(-windows[0-9.]*)?")
_SEG_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}")
_EF_VER_RX = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_OVERRIDE_RX = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})=([0-9]+(\.[0-9]+){1,3})")
_PKG_ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_PKG_VER_RX = re.compile(r"[0-9]+(\.[0-9]+){1,3}")
_USING_RX = re.compile(r"[A-Za-z0-9_.]{1,100}")
_BAD_REF_CHARS = re.compile("[\x00-\x1f\x7f\"<>|]")


def add_arguments(p):
    p.add_argument("--project-root", default=None, help="project root (default: CLAUDE_PROJECT_DIR, else the current directory)")
    p.add_argument("--target-project", required=True)
    p.add_argument("--profile", default="vstest-xunitv3")
    p.add_argument("--test-tfm", default=None)
    p.add_argument("--tests-dir", default="tests")
    p.add_argument("--version-overrides", default="", help="'Id=1.2.3;Id2=4.5.6' (scan strategy.versionOverrides)")
    p.add_argument("--ef-sqlite-version", default="")
    p.add_argument("--with-mocks", action="store_true")
    p.add_argument("--no-solution", action="store_true")


def read_target_framework(csproj_path):
    """First TargetFrameworks/TargetFramework value of a project file, or None. DTDs are refused; nothing is resolved."""
    try:
        data = jsonio.read_bytes_capped(csproj_path, 2 * 1024 * 1024)
        from ..core import xmlsafe
        root = xmlsafe.parse_xml_bytes(data)
        for el in root.iter():
            local = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
            if local in ("TargetFrameworks", "TargetFramework"):
                return (el.text or "").split(";")[0].strip()
    except (OSError, jsonio.JsonError, ValueError, Exception):
        return None
    return None


def test_project_name(base):
    """Name part of the new test project (<base>.Tests): spaces, hyphens and other characters become '_' (also used by guard-writes)."""
    return "".join(c if (c in "_." or unicodedata.category(c)[0] in "LMN") else "_" for c in base)


def xml_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def run(args):
    root = project_root(args)
    target = resolve(root, args.target_project)
    if not target.parts or not target.parts[-1].lower().endswith(".csproj"):
        raise CmdError("--target-project must be a .csproj file.")
    if not (target.exists and os.path.isfile(fs_path(target.path))):
        raise CmdError("The target project does not exist.")

    profile = args.profile
    if not _PROFILE_RX.fullmatch(profile):
        raise CmdError("Invalid profile name.")
    data = jsonio.read_json_file(PACKAGES_JSON, 1024 * 1024)
    profiles = data.get("profiles", {})
    if profile not in profiles:
        raise CmdError("Unknown profile: " + safe_ascii(profile, 40))
    prof = profiles[profile]

    tfm = args.test_tfm
    if tfm is None or not tfm.strip():
        tfm = read_target_framework(target.path)
    if tfm is None or not _TFM_RX.fullmatch(tfm):
        raise CmdError("The test target framework '%s' is not supported (net8.0 or newer, optionally with -windows). Pass --test-tfm explicitly."
                       % safe_ascii(tfm, 40))

    base = os.path.splitext(target.parts[-1])[0]
    # Spaces, hyphens and other characters in the target name become '_' in the name of the NEW test project.
    test_base = test_project_name(base)
    if not (1 <= len(test_base) <= 100) or re.fullmatch(r"[._]+", test_base):
        raise CmdError("The target project name cannot be turned into a usable test project name.")
    test_name = test_base + ".Tests"
    segs = [s for s in re.split(r"[\\/]", args.tests_dir) if s != ""]
    if not segs or len(segs) > 4:
        raise CmdError("--tests-dir must be a short relative folder path.")
    for seg in segs:
        if not _SEG_RX.fullmatch(seg) or seg.lower() in BANNED_DIRS:
            raise CmdError("--tests-dir contains a folder name that is not allowed: " + safe_ascii(seg, 40))
    dir_res = resolve(root, "/".join(segs + [test_name]))
    directory = dir_res.path
    probe = os.path.dirname(directory)
    root_norm = os.path.normcase(root.rstrip("\\/"))
    while len(os.path.normcase(probe)) >= len(root_norm):
        if os.path.isdir(fs_path(probe)) and any(n.lower().endswith(".csproj") for n in os.listdir(fs_path(probe))):
            raise CmdError("A project file sits in the folder that would contain the tests (for example a single-project repository with the "
                           ".csproj at its root); default globbing would pull the test files into that project. Not scaffolding. Create the "
                           "test project yourself outside that folder, or point the plugin at an existing test project.")
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    csproj_path = os.path.join(directory, test_name + ".csproj")
    if os.path.exists(fs_path(csproj_path)):
        raise CmdError("Test project already exists: " + safe_ascii(test_name, 100))

    # --- packages from the allowlist
    pkgs = [dict(p) for p in prof.get("packages", [])]
    if args.with_mocks:
        pkgs.append(dict(data["optional"]["mocking"]))
    if args.ef_sqlite_version and args.ef_sqlite_version.strip():
        if not _EF_VER_RX.fullmatch(args.ef_sqlite_version):
            raise CmdError("--ef-sqlite-version must look like 9.0.12.")
        pkgs.append({"id": data["optional"]["efCoreTestDatabase"]["id"], "version": args.ef_sqlite_version})
    override = {}
    for o in [x for x in args.version_overrides.split(";") if x]:
        m = _OVERRIDE_RX.fullmatch(o)
        if not m:
            raise CmdError("A version override must look like Id=1.2.3.")
        if not any(p["id"] == m.group(1) for p in pkgs):
            raise CmdError("A version override names a package that is not part of the profile: " + safe_ascii(m.group(1), 60))
        override[m.group(1)] = m.group(2)
    refs = []
    for p in pkgs:
        version = override.get(p["id"], p["version"])
        if not _PKG_ID_RX.fullmatch(p["id"]) or not _PKG_VER_RX.fullmatch(version):
            raise CmdError("Bad package entry in packages.json: " + safe_ascii(p["id"], 100))
        # '[x.y.z]' is an exact version in NuGet; a bare '4.0.2' would mean "4.0.2 or higher".
        if p.get("privateAssets") == "all":
            refs += ['    <PackageReference Include="%s" Version="[%s]">' % (p["id"], version),
                     "      <PrivateAssets>all</PrivateAssets>",
                     "      <IncludeAssets>runtime; build; native; contentfiles; analyzers</IncludeAssets>",
                     "    </PackageReference>"]
        else:
            refs.append('    <PackageReference Include="%s" Version="[%s]" />' % (p["id"], version))
    usings = []
    for u in prof.get("usings", []):
        if not _USING_RX.fullmatch(u):
            raise CmdError("Bad using in packages.json.")
        usings.append('    <Using Include="%s" />' % u)

    rel_ref = os.path.relpath(target.path, directory).replace(os.sep, "/")
    # The path lands in an XML attribute read by MSBuild. Control characters are refused; characters that MSBuild treats specially
    # (% @ $ ( ) ; ' * ?) are written as %xx escapes and the XML-special ones are entity-escaped.
    if not (1 <= len(rel_ref) <= 300) or _BAD_REF_CHARS.search(rel_ref):
        raise CmdError("The path from the test project to the target project is too long or contains characters that cannot be used in a project reference.")
    rel_ref = re.sub(r"[%@$();'*?]", lambda m: "%%%02X" % ord(m.group(0)), rel_ref)
    rel_ref = xml_escape(rel_ref)

    output_type = "    <OutputType>Exe</OutputType>\n" if prof.get("outputType") == "Exe" else ""
    content = (
        '<Project Sdk="Microsoft.NET.Sdk">\n\n'
        "  <PropertyGroup>\n"
        "    <TargetFramework>" + tfm + "</TargetFramework>\n" + output_type +
        "    <IsPackable>false</IsPackable>\n"
        "    <IsTestProject>true</IsTestProject>\n"
        "    <Nullable>enable</Nullable>\n"
        "    <ImplicitUsings>enable</ImplicitUsings>\n"
        "  </PropertyGroup>\n\n"
        "  <!-- Isolation from repository-wide settings, so no repository file has to change. -->\n"
        "  <PropertyGroup>\n"
        "    <ManagePackageVersionsCentrally>false</ManagePackageVersionsCentrally>\n"
        "    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>\n"
        "    <WarningsAsErrors />\n"
        "    <RestorePackagesWithLockFile>false</RestorePackagesWithLockFile>\n"
        "    <RestoreLockedMode>false</RestoreLockedMode>\n"
        "    <EnforceCodeStyleInBuild>false</EnforceCodeStyleInBuild>\n"
        "    <NuGetAudit>true</NuGetAudit>\n"
        "  </PropertyGroup>\n\n"
        "  <ItemGroup>\n" + "\n".join(refs) + "\n  </ItemGroup>\n\n"
        "  <ItemGroup>\n" + "\n".join(usings) + "\n  </ItemGroup>\n\n"
        "  <ItemGroup>\n"
        '    <ProjectReference Include="' + rel_ref + '" />\n'
        "  </ItemGroup>\n\n"
        "</Project>\n"
    )
    os.makedirs(fs_path(directory), exist_ok=True)
    jsonio.write_atomic(csproj_path, content)

    solution_updated = False
    solution_name = None
    if not args.no_solution:
        slns = []
        with os.scandir(fs_path(root)) as it:
            for e in sorted(it, key=lambda e: e.name):
                if e.is_file(follow_symlinks=False) and e.name.lower().endswith((".sln", ".slnx")) and not is_link_stat(e.stat(follow_symlinks=False)):
                    slns.append(e.name)
        if len(slns) == 1:
            solution_name = clean_text(slns[0], 100)
            from ..core import proc
            dotnet = proc.resolve_dotnet(exclude_dirs=[root])
            if dotnet:
                try:
                    r = proc.run([dotnet, "sln", os.path.join(root, slns[0]), "add", csproj_path], cwd=root, timeout=120, max_output=64 * 1024)
                    solution_updated = (r.returncode == 0)
                except (OSError, ValueError):
                    solution_updated = False
    emit_json({"projectPath": csproj_path, "profile": profile, "testTfm": tfm, "solutionUpdated": solution_updated, "solution": solution_name})
    return 0
