"""Platform detection: one immutable record that every other module uses instead of ad-hoc checks.

Kept light on purpose (no dataclasses/typing/tempfile at import time): every hook call imports this module."""
from __future__ import annotations

import os
import sys
import unicodedata

_FIELDS = (
    "os_family",          # windows | macos | linux
    "is_wsl",             # kernel says Microsoft/WSL. Also true inside Docker Desktop containers (WSL2 kernel): read together with is_container
    "is_container",       # docker | containerd | kubepods markers (/.dockerenv, /proc/1/cgroup); False elsewhere
    "is_msys",            # Git Bash / MSYS / Cygwin environment variables present
    "arch",
    "shell_kind",         # git-bash | bash | zsh | wsl-bash | unknown (heuristic, from the environment)
    "fs_case_sensitive",  # None = not probed
    "unicode_names",      # nfd-listing | preserved | unknown
    "python_version",
    "python_executable",
    "home",
    "temp",
    "nuget_cache",
)


class Platform(object):
    """Immutable record. Build with Platform(**fields); change with .replace(**changes)."""
    __slots__ = _FIELDS

    def __init__(self, **fields):
        if set(fields) != set(_FIELDS):
            raise TypeError("Platform needs exactly the fields: " + ", ".join(_FIELDS))
        for name in _FIELDS:
            object.__setattr__(self, name, fields[name])

    def __setattr__(self, name, value):
        raise AttributeError("Platform is immutable")

    def __delattr__(self, name):
        raise AttributeError("Platform is immutable")

    def __repr__(self):
        return "Platform(%s)" % ", ".join("%s=%r" % (n, getattr(self, n)) for n in _FIELDS)

    def replace(self, **changes):
        fields = self.to_dict()
        fields["python_version"] = tuple(fields["python_version"])
        fields.update(changes)
        return Platform(**fields)

    def to_dict(self):
        d = {n: getattr(self, n) for n in _FIELDS}
        d["python_version"] = list(self.python_version)
        return d

    @property
    def is_windows(self):
        return self.os_family == "windows"


_cached_family = None
_cached_platform = None


def os_family():
    """windows | macos | linux. Cheap: no record is built."""
    global _cached_family
    if _cached_family is None:
        if sys.platform == "win32":
            _cached_family = "windows"
        elif sys.platform == "darwin":
            _cached_family = "macos"
        else:
            _cached_family = "linux"
    return _cached_family


def _is_wsl():
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read(4096).lower()
    except OSError:
        return False


def _is_container():
    """True inside a Docker / containerd / Kubernetes container (Linux only). Informational: nothing depends on it."""
    if sys.platform != "linux":
        return False
    try:
        if os.path.exists("/.dockerenv"):
            return True
    except OSError:
        pass
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(8192).lower()
    except OSError:
        return False
    return any(m in text for m in ("docker", "containerd", "kubepods"))


def _shell_kind(wsl):
    env = os.environ
    if env.get("MSYSTEM"):
        return "git-bash"
    shell = os.path.basename(env.get("SHELL", "")).lower()
    if env.get("ZSH_VERSION") or shell == "zsh":
        return "zsh"
    if env.get("BASH_VERSION") or shell == "bash":
        return "wsl-bash" if wsl else "bash"
    return "unknown"


def _home():
    home = os.path.expanduser("~")
    if not home or home == "~":
        return "unknown"
    return home


def _nuget_cache(home):
    explicit = os.environ.get("NUGET_PACKAGES", "")
    if explicit:
        return explicit
    if home == "unknown":
        return "unknown"
    return os.path.join(home, ".nuget", "packages")


def _machine():
    import platform as _pf
    return _pf.machine() or "unknown"


def _temp_dir():
    import tempfile
    try:
        return tempfile.gettempdir()
    except Exception:  # pragma: no cover - extremely unusual
        return "unknown"


def detect():
    """The platform record (cached per process). Filesystem probes are NOT run here (see detect_probed)."""
    global _cached_platform
    if _cached_platform is not None:
        return _cached_platform
    family = os_family()
    wsl = _is_wsl()
    msys = bool(os.environ.get("MSYSTEM")) or os.environ.get("OSTYPE", "") in ("msys", "cygwin") or sys.platform == "cygwin"
    home = _home()
    arch = (os.environ.get("PROCESSOR_ARCHITECTURE") if family == "windows" else None) or _machine()
    _cached_platform = Platform(
        os_family=family,
        is_wsl=wsl,
        is_container=_is_container(),
        is_msys=msys,
        arch=arch,
        shell_kind=_shell_kind(wsl),
        fs_case_sensitive=None,
        unicode_names="unknown",
        python_version=tuple(sys.version_info[:3]),
        python_executable=sys.executable or "unknown",
        home=home,
        temp=_temp_dir(),
        nuget_cache=_nuget_cache(home),
    )
    return _cached_platform


def probe_case_sensitive(directory):
    """True if two names differing only in case are different files in `directory`. Creates and removes two temp files."""
    token = os.urandom(6).hex()
    low = os.path.join(directory, "dtt-case-" + token + "-a")
    up = os.path.join(directory, "DTT-CASE-" + token + "-A")
    try:
        fd = os.open(low, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return not os.path.exists(up)
    finally:
        for p in (low, up):
            try:
                os.unlink(p)
            except OSError:
                pass


def probe_unicode_names(directory):
    """'nfd-listing' when a name created in NFC form is listed decomposed (macOS HFS+/APFS-like), else 'preserved'."""
    nfc = "dtt-" + chr(0xE9) + "-" + os.urandom(4).hex()
    path = os.path.join(directory, nfc)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        for n in os.listdir(directory):
            if unicodedata.normalize("NFC", n) == nfc:
                return "preserved" if n == nfc else "nfd-listing"
        return "unknown"
    except OSError:
        return "unknown"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def detect_probed():
    """detect() plus the two filesystem probes, run in a fresh private directory below the temp folder."""
    import shutil
    import tempfile
    base = detect()
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="dtt-probe-")
        cs = probe_case_sensitive(tmp)
        un = probe_unicode_names(tmp)
    except OSError:
        cs, un = None, "unknown"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return base.replace(fs_case_sensitive=cs, unicode_names=un)


def public_dict(p):
    """The record for bug reports: the user's home folder name is replaced by '~'."""
    d = p.to_dict()
    home = p.home
    if home and home != "unknown" and len(home.rstrip("/" + chr(92))) > 0:       # a home of "/" would turn every absolute path into "~..."
        for key in ("temp", "nuget_cache", "python_executable"):
            val = d[key]
            if isinstance(val, str) and val.lower().startswith(home.lower()):
                d[key] = "~" + val[len(home):]
        d["home"] = "~"
    return d
