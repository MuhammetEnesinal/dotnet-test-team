"""Strict loader for data/db-engines.json (the table of database engines the plugin can start in Docker).

Everything in the table ends up in a docker command line or in a container, so it is validated key by key: unknown keys are rejected,
every string must match a strict pattern, the file is size capped. A tampered table raises DbTableError (fixed message)."""
from __future__ import annotations

import os
import re

from . import jsonio

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TABLE_PATH = os.path.join(PLUGIN_ROOT, "data", "db-engines.json")
MAX_TABLE_BYTES = 64 * 1024

RECIPE_IDS = ("sqlserver", "postgres", "mysql", "mariadb", "oracle")
NO_RECIPE_IDS = ("firebird", "db2", "cosmos")
ENGINE_IDS = RECIPE_IDS + NO_RECIPE_IDS

TAG_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
REPO_RX = re.compile(r"[a-z0-9][a-z0-9._/-]{0,99}")
_PATTERN_RX = re.compile(r"[a-z0-9][a-z0-9._-]{2,99}")
_ENV_NAME_RX = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ENV_VALUE_RX = re.compile(r"[A-Za-z0-9_.:/-]{1,64}")
_URL_RX = re.compile(r"https://[A-Za-z0-9./?=&_-]{1,200}")
_EXEC_ARG_RX = re.compile(r"[\x20-\x7e\n]{1,600}")
_ADMIN_DB_RX = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,30}")
_PLACEHOLDER_RX = re.compile(r"\{[^{}]*\}")
_MESSAGE_RX = re.compile(r"[A-Za-z0-9 .,;:()'/-]{1,200}")
_TOP_KEYS = {"schemaVersion", "description", "engines"}
_COMMON_KEYS = {"id", "recipe", "packagePatterns"}
_RECIPE_KEYS = _COMMON_KEYS | {"image", "composeRepos", "defaultTag", "port", "memoryMb", "needsEula", "eulaUrl", "archs",
                               "pullSizeHintMb", "passwordEnv", "env", "exec", "readyTimeoutS", "connection", "adminDatabase"}
_NO_RECIPE_KEYS = _COMMON_KEYS | {"message"}
_ARCHS = ("amd64", "arm64")
PASSWORD_PLACEHOLDER = "{password}"


class DbTableError(Exception):
    """The engine table is unusable. The message is a fixed sentence."""


def _fail(what):
    raise DbTableError("data/db-engines.json is invalid (%s)." % what)


def _int(value, low, high, what):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(what)
    return value


def _str_list(value, rx, what, max_items):
    if not isinstance(value, list) or len(value) > max_items:
        _fail(what)
    for item in value:
        if not isinstance(item, str) or rx.fullmatch(item) is None:
            _fail(what)
    return list(value)


_HEREDOC_RX = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n\2(?:\n|$)", re.DOTALL)


def password_expansion_ok(arg, pw_env) -> bool:
    """False when a shell script (an exec argument) expands the password variable where it could end up in a process argument.
    Allowed: a here-doc body (the shell writes it to a pipe or file, never to argv) and the assignment form `NAME="$PW" command`
    (an environment variable of one command). Everything else - `-P "$PW"`, `user/$PW@host`, `printf ... "$PW"` - is refused."""
    expansion = re.compile(r"\$\{?" + re.escape(pw_env) + r"(?![A-Za-z0-9_])\}?")
    rest = _HEREDOC_RX.sub(" ", arg)
    rest = re.sub(r"(?:(?<=^)|(?<=[\s;&|(]))[A-Za-z_][A-Za-z0-9_]*=\"?\$\{?" + re.escape(pw_env) + r"\}?\"?(?=\s|$)", " ", rest)
    return expansion.search(rest) is None


def valid_tag(tag) -> bool:
    return isinstance(tag, str) and TAG_RX.fullmatch(tag) is not None


def _check_recipe(entry):
    out = {"id": entry["id"], "recipe": True}
    out["packagePatterns"] = _str_list(entry["packagePatterns"], _PATTERN_RX, "packagePatterns", 8)
    image = entry["image"]
    if not isinstance(image, str) or REPO_RX.fullmatch(image) is None or ".." in image or "//" in image:
        _fail("image")
    out["image"] = image
    out["composeRepos"] = _str_list(entry["composeRepos"], REPO_RX, "composeRepos", 6)
    if not valid_tag(entry["defaultTag"]):
        _fail("defaultTag")
    out["defaultTag"] = entry["defaultTag"]
    out["port"] = _int(entry["port"], 1, 65535, "port")
    out["memoryMb"] = _int(entry["memoryMb"], 256, 16384, "memoryMb")
    if not isinstance(entry["needsEula"], bool):
        _fail("needsEula")
    out["needsEula"] = entry["needsEula"]
    if entry["needsEula"]:
        url = entry.get("eulaUrl")
        if not isinstance(url, str) or _URL_RX.fullmatch(url) is None:
            _fail("eulaUrl")
        out["eulaUrl"] = url
    elif "eulaUrl" in entry:
        _fail("eulaUrl without needsEula")
    archs = _str_list(entry["archs"], re.compile("|".join(_ARCHS)), "archs", 2)
    if not archs:
        _fail("archs")
    out["archs"] = archs
    out["pullSizeHintMb"] = _int(entry["pullSizeHintMb"], 1, 20000, "pullSizeHintMb")
    pw_env = entry["passwordEnv"]
    if not isinstance(pw_env, str) or _ENV_NAME_RX.fullmatch(pw_env) is None:
        _fail("passwordEnv")
    out["passwordEnv"] = pw_env
    env = entry["env"]
    if not isinstance(env, list) or not env or len(env) > 8:
        _fail("env")
    names, password_names = [], []
    for item in env:
        if not isinstance(item, str) or "=" not in item:
            _fail("env")
        name, value = item.split("=", 1)
        if _ENV_NAME_RX.fullmatch(name) is None or name in names:
            _fail("env")
        if value == PASSWORD_PLACEHOLDER:
            password_names.append(name)
        elif _ENV_VALUE_RX.fullmatch(value) is None:
            _fail("env")
        names.append(name)
    if password_names != [pw_env]:
        _fail("env must pass the password through passwordEnv only")
    out["env"] = list(env)
    argv = entry["exec"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= 6:
        _fail("exec")
    for index, arg in enumerate(argv):
        if not isinstance(arg, str) or _EXEC_ARG_RX.fullmatch(arg) is None or (index == 0 and arg.startswith("-")) or "{password}" in arg:
            _fail("exec")
    if any(not password_expansion_ok(arg, pw_env) for arg in argv):
        _fail("exec must not put the password in a program argument")
    out["exec"] = list(argv)
    out["readyTimeoutS"] = _int(entry["readyTimeoutS"], 10, 600, "readyTimeoutS")
    conn = entry["connection"]
    if not isinstance(conn, str) or _EXEC_ARG_RX.fullmatch(conn) is None:
        _fail("connection")
    if sorted(_PLACEHOLDER_RX.findall(conn)) != ["{password}", "{port}"] or conn.count("{") != 2 or conn.count("}") != 2:
        _fail("connection placeholders")
    out["connection"] = conn
    db = entry["adminDatabase"]
    if not isinstance(db, str) or _ADMIN_DB_RX.fullmatch(db) is None:
        _fail("adminDatabase")
    out["adminDatabase"] = db
    return out


def validate(doc) -> dict:
    """Validated table: {engine id: entry}. Raises DbTableError."""
    if not isinstance(doc, dict) or set(doc) - _TOP_KEYS or "schemaVersion" not in doc or "engines" not in doc:
        _fail("top level")
    if doc["schemaVersion"] != 1 or isinstance(doc["schemaVersion"], bool):
        _fail("schemaVersion")
    engines = doc["engines"]
    if not isinstance(engines, list) or len(engines) > 32:
        _fail("engines")
    table = {}
    for entry in engines:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] not in ENGINE_IDS or entry["id"] in table:
            _fail("engine id")
        eid = entry["id"]
        if entry.get("recipe") is True and eid in RECIPE_IDS:
            if set(entry) - _RECIPE_KEYS or (_RECIPE_KEYS - {"eulaUrl"}) - set(entry):
                _fail("keys of " + eid)
            table[eid] = _check_recipe(entry)
        elif entry.get("recipe") is False and eid in NO_RECIPE_IDS:
            if set(entry) != _NO_RECIPE_KEYS:
                _fail("keys of " + eid)
            message = entry["message"]
            if not isinstance(message, str) or _MESSAGE_RX.fullmatch(message) is None:
                _fail("message")
            table[eid] = {"id": eid, "recipe": False, "message": message,
                          "packagePatterns": _str_list(entry["packagePatterns"], _PATTERN_RX, "packagePatterns", 8)}
        else:
            _fail("recipe flag of " + eid)
    if set(table) != set(ENGINE_IDS):
        _fail("engine list")
    return table


def load(path=None) -> dict:
    try:
        doc = jsonio.read_json_file(path or TABLE_PATH, MAX_TABLE_BYTES, 8)
    except jsonio.JsonError:
        raise DbTableError("data/db-engines.json cannot be read.")
    return validate(doc)
