"""NuGet.Config chain, read as data only, to LIST the configured package sources (names and URLs). Nothing here touches the network.

Configs are read from the file system root down to the project folder (nearer files win), after the user-level config. <clear/> in a
nearer file removes what farther files added; disabledPackageSources is honoured. Environment variables or MSBuild properties inside a
source are not expanded. Machine-wide config is not read. Files are parsed with the safe XML reader (no DTD, size capped)."""
from __future__ import annotations

import os
import re

from . import jsonio
from .paths import fs_path, is_link_stat

CONFIG_NAMES = ("NuGet.Config", "nuget.config", "NuGet.config")
DEFAULT_SOURCE_KEY = "nuget.org"
DEFAULT_SOURCE_URL = "https://api.nuget.org/v3/index.json"
MAX_CONFIG_BYTES = 1024 * 1024


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_config_text(data):
    """Returns a list of operations: ('clear_sources',), ('add_source', key, value), ('clear_disabled',), ('disable', key), ('enable', key)."""
    from . import xmlsafe
    root = xmlsafe.parse_xml_bytes(data)
    ops = []
    for section in root:
        name = _local(section.tag)
        if name == "packageSources":
            for el in section:
                kind = _local(el.tag)
                if kind == "clear":
                    ops.append(("clear_sources",))
                elif kind == "add" and el.get("key") is not None and el.get("value") is not None:
                    ops.append(("add_source", el.get("key"), el.get("value").strip()))
        elif name == "disabledPackageSources":
            for el in section:
                kind = _local(el.tag)
                if kind == "clear":
                    ops.append(("clear_disabled",))
                elif kind == "add" and el.get("key") is not None:
                    ops.append(("disable" if (el.get("value", "") or "").strip().lower() == "true" else "enable", el.get("key")))
    return ops


def user_config_path(env=None):
    env = os.environ if env is None else env
    if os.name == "nt":
        appdata = env.get("APPDATA", "")
        return os.path.join(appdata, "NuGet", "NuGet.Config") if appdata else None
    home = env.get("HOME", "")
    return os.path.join(home, ".nuget", "NuGet", "NuGet.Config") if home else None


def config_files(start_dir, env=None):
    """Existing config files, farthest first (user config, then the ancestors of start_dir from the root down to start_dir)."""
    files = []
    user = user_config_path(env)
    if user and os.path.isfile(fs_path(user)):
        files.append(user)
    chain = []
    cur = os.path.abspath(start_dir)
    while True:
        chain.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(chain):
        try:
            names = set(os.listdir(fs_path(d)))
        except OSError:
            continue
        for n in CONFIG_NAMES:
            if n in names:
                p = os.path.join(d, n)
                try:
                    if os.path.isfile(fs_path(p)) and not is_link_stat(os.lstat(fs_path(p))):
                        files.append(p)
                except OSError:
                    pass
                break
    return files


def effective_sources(start_dir, env=None):
    """Returns (sources, problems): sources = list of {"key", "value", "enabled"} in precedence order; problems = list of fixed strings."""
    sources = {}
    disabled = set()
    problems = []
    for path in config_files(start_dir, env):
        try:
            ops = parse_config_text(jsonio.read_bytes_capped(path, MAX_CONFIG_BYTES))
        except Exception:
            problems.append("unreadable-config")
            continue
        for op in ops:
            if op[0] == "clear_sources":
                sources = {}
            elif op[0] == "add_source":
                sources[op[1]] = op[2]
            elif op[0] == "clear_disabled":
                disabled = set()
            elif op[0] == "disable":
                disabled.add(op[1])
            elif op[0] == "enable":
                disabled.discard(op[1])
    if not sources and not problems:
        sources = {DEFAULT_SOURCE_KEY: DEFAULT_SOURCE_URL}      # NuGet's built-in default when nothing is configured
    return [{"key": k, "value": v, "enabled": k not in disabled} for k, v in sources.items()], problems


LOCAL_PATH_TEXT = "<local path>"
UNRECOGNISED_SOURCE_TEXT = "<unrecognised source>"
_URL_RX = re.compile(r"^(https?):[\\/]{2}([^\\/?#]*)", re.I)
_HOST_RX = re.compile(r"(?:[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,250}[A-Za-z0-9_])?|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?")


def redact_source(value):
    """A source location safe to show: an http(s) URL is reduced to scheme, host and port - user info (everything up to the LAST
    '@' of the authority), path (API keys live in paths), query and fragment are dropped; a folder or any other form is shown as
    '<local path>'. A host that does not look like a host name or address is '<unrecognised source>'."""
    m = _URL_RX.match(value.strip() if isinstance(value, str) else "")
    if not m:
        return LOCAL_PATH_TEXT
    authority = m.group(2)
    host = authority[authority.rfind("@") + 1:]
    if not _HOST_RX.fullmatch(host):
        return UNRECOGNISED_SOURCE_TEXT
    return m.group(1).lower() + "://" + host.lower()
