"""Read-only database detection for `dtt scan` (no process, no network, nothing is evaluated).

- engine: from the package ids of the PRODUCTION projects (never the test project), by the prefixes of data/db-engines.json. The result
  does not depend on the order of the packages.
- image tag: a project's docker-compose*.yml / compose.y*ml is read as DATA only, with a tolerant line reader (no YAML library): only
  `image:` lines, only a repository listed in the engine's composeRepos, only a tag that matches the strict tag pattern. Variables,
  digests, ports, environment, passwords and volumes are never read. At most 5 files of at most 64 KB; files below a test/bin/obj
  folder are ignored.
- migrations / features: a bounded text search of the production projects' .cs files (counts of five EF configuration method names)."""
from __future__ import annotations

import os
import re
import time

from . import dbengines, jsonio
from .paths import PathError, fs_path, is_link_stat, resolve_inside

COMPOSE_MAX_BYTES = 64 * 1024
COMPOSE_MAX_FILES = 5
COMPOSE_MAX_LINES = 2000
COMPOSE_LINE_CAP = 300
MAX_DIR_ENTRIES = 2000
FEATURE_NAMES = ("IsRowVersion", "HasFilter", "HasCheckConstraint", "HasComputedColumnSql", "HasPrecision")
MAX_CS_FILES = 400
MAX_CS_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DIRS = 2000
MAX_SECONDS = 5.0
SKIP_DIRS = ("bin", "obj", "node_modules", ".git", ".test-generator", ".vs", ".idea")
# first match wins; the same priority as the EF provider choice in scan (postgres, mysql, oracle before sqlserver)
ENGINE_PRIORITY = ("postgres", "mysql", "oracle", "sqlserver", "firebird", "db2", "cosmos")

_COMPOSE_NAME_RX = re.compile(r"(docker-compose(?:[.-][a-z0-9._-]{0,40})?|compose)\.ya?ml")
_IMAGE_LINE_RX = re.compile(r"\s*(?:-\s+)?image\s*:\s*(\S.*)")
_BLOCK_SCALAR_RX = re.compile(r"(?:^|[\s:-])[|>][+-]?[0-9]?\s*(?:#.*)?$")
_KEY_ONLY_RX = re.compile(r"\s*(?:-\s+)?([A-Za-z0-9_.-]{1,60})\s*:\s*(?:#.*)?")
_DB_SERVICE_RX = re.compile(r"(?i)(db|database|sql|postgres|pg|mysql|maria|oracle|mssql|data)")
_FEATURE_RX ={name: re.compile(r"\b%s\s*[(<]" % name) for name in FEATURE_NAMES}


def _test_like(name):
    return "test" in name.casefold()


def detect_engine(package_ids, table):
    """Engine id (a key of `table`) or None. package_ids: lower-case package ids of the production projects."""
    ids = [i for i in package_ids if isinstance(i, str)]
    for eid in ENGINE_PRIORITY:
        entry = table.get(eid)
        if entry and any(i.startswith(p) for p in entry["packagePatterns"] for i in ids):
            return eid
    return None


# ---------------------------------------------------------------- compose
def parse_compose_text(text, repos):
    """[(repo, tag)] from the `image:` lines of one compose file's text. repos: the accepted repository names."""
    out = []
    preferred = []
    block_indent = None          # inside a block scalar (`key: |` / `>`): its lines are text, never `image:` lines
    stack = []                   # (indent, key) of `key:` lines without a value: the enclosing service of an image line
    for raw_line in text.splitlines()[:COMPOSE_MAX_LINES]:
        line = raw_line[:COMPOSE_LINE_CAP]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        if block_indent is not None:
            if not stripped or indent > block_indent:
                continue
            block_indent = None
        if stripped and not stripped.startswith("#"):
            if _BLOCK_SCALAR_RX.search(line):
                block_indent = indent
            key = _KEY_ONLY_RX.fullmatch(line)
            if key:
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, key.group(1)))
        m = _IMAGE_LINE_RX.fullmatch(line)
        if not m:
            continue
        service = next((k for i, k in reversed(stack) if i < indent), "")
        value = m.group(1).strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end < 0:
                continue
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if not value or re.search(r"[\s$@{}]", value) or ":" not in value:
            continue
        repo, _sep, tag = value.rpartition(":")
        if "/" in tag or repo not in repos or not dbengines.valid_tag(tag):
            continue
        (preferred if _DB_SERVICE_RX.search(service) else out).append((repo, tag))
    return preferred + out                     # services named like a database first, otherwise file order


def _compose_files(root, start_dir):
    """Relative paths (forward slashes) of up to COMPOSE_MAX_FILES compose files: start_dir first, then each parent up to root."""
    found = []
    cur = os.path.abspath(start_dir)
    root_abs = os.path.abspath(root)
    root_norm = os.path.normcase(root_abs).rstrip("\\/")
    while len(found) < COMPOSE_MAX_FILES:
        rel_dir = os.path.relpath(cur, root_abs).replace(os.sep, "/")
        parts = [] if rel_dir == "." else rel_dir.split("/")
        if not any(p.casefold() in ("bin", "obj") or _test_like(p) for p in parts):
            try:
                with os.scandir(fs_path(cur)) as it:
                    entries = sorted((e for _i, e in zip(range(MAX_DIR_ENTRIES), it)), key=lambda e: e.name)
            except OSError:
                entries = []
            for e in entries:
                try:
                    if _COMPOSE_NAME_RX.fullmatch(e.name.casefold()) and e.is_file(follow_symlinks=False) \
                            and not is_link_stat(e.stat(follow_symlinks=False)):
                        found.append("/".join(parts + [e.name]))
                        if len(found) >= COMPOSE_MAX_FILES:
                            break
                except OSError:
                    continue
        if os.path.normcase(cur).rstrip("\\/") == root_norm:
            break
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return found


def compose_images(root, start_dir, repos):
    """[(repo, tag)] over the compose files near the production project (file order: nearest first)."""
    out = []
    for rel in _compose_files(root, start_dir):
        try:
            res = resolve_inside(root, rel)
            if not res.exists:
                continue
            data = jsonio.read_bytes_capped(res.path, COMPOSE_MAX_BYTES, refuse_hardlinks=True)
        except (PathError, jsonio.JsonError, OSError):
            continue
        if b"\x00" in data:
            continue
        out.extend(parse_compose_text(data.decode("utf-8", errors="replace"), repos))
    return out


def choose_engine_and_tag(engine, table, images):
    """(engine, tag, tagFrom). A mysql-family project whose compose file runs only a mariadb image is mariadb; the tag comes from the
    first compose image of the engine's repositories, else the table default."""
    if engine is None:
        return None, None, None
    if not table[engine]["recipe"]:
        return engine, None, None
    if engine == "mysql":
        mysql_tags = [t for r, t in images if r in table["mysql"]["composeRepos"]]
        maria_tags = [t for r, t in images if r in table["mariadb"]["composeRepos"]]
        if maria_tags and not mysql_tags:
            engine = "mariadb"
    for repo, tag in images:
        if repo in table[engine]["composeRepos"]:
            return engine, tag, "compose"
    return engine, table[engine]["defaultTag"], "table"


# ---------------------------------------------------------------- sources
def scan_sources(project_dirs):
    """(migrations: bool, {feature name: count}) over the .cs files of the production project folders. Nested projects, test-like
    folders and files, bin/obj and Migrations folders are not read (a ModelSnapshot file is only looked for by name)."""
    counts = dict((n, 0) for n in FEATURE_NAMES)
    migrations = False
    files_read = 0
    total = 0
    dirs_seen = 0
    start = time.monotonic()
    done = set()
    for top in project_dirs:
        top_key = os.path.normcase(os.path.abspath(top))
        if top_key in done:
            continue
        done.add(top_key)
        stack = [(top, False)]
        while stack:
            if dirs_seen >= MAX_DIRS or time.monotonic() - start > MAX_SECONDS:
                return migrations, counts
            d, in_migrations = stack.pop()
            dirs_seen += 1
            try:
                with os.scandir(fs_path(d)) as it:
                    entries = sorted((e for _i, e in zip(range(MAX_DIR_ENTRIES), it)), key=lambda e: e.name)
            except OSError:
                continue
            if d != top and any(e.name.lower().endswith(".csproj") for e in entries):
                continue                                  # a nested project is its own project
            for e in entries:
                try:
                    info = e.stat(follow_symlinks=False)
                    if is_link_stat(info):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        low = e.name.casefold()
                        if low in SKIP_DIRS or _test_like(e.name):
                            continue
                        stack.append((os.path.join(d, e.name), in_migrations or low == "migrations"))
                        continue
                except OSError:
                    continue
                low = e.name.casefold()
                if not low.endswith(".cs"):
                    continue
                if low.endswith("modelsnapshot.cs"):
                    migrations = True
                if in_migrations or _test_like(e.name) or files_read >= MAX_CS_FILES or total >= MAX_TOTAL_BYTES:
                    continue
                if info.st_size > MAX_CS_BYTES:
                    continue
                try:
                    data = jsonio.read_bytes_capped(os.path.join(d, e.name), MAX_CS_BYTES, refuse_hardlinks=True)
                except (jsonio.JsonError, OSError):
                    continue
                files_read += 1
                total += len(data)
                text = data.decode("utf-8", errors="replace")
                for name, rx in _FEATURE_RX.items():
                    counts[name] += len(rx.findall(text))
    return migrations, counts


def features_list(counts):
    return [{"name": n, "count": counts[n]} for n in FEATURE_NAMES if counts.get(n)]
