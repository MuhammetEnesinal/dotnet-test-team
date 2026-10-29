"""Command line dispatcher: `dtt hook <name>`, `dtt --version`, `dtt --platform`.

Exit codes of the CLI: 0 ok, 1 finding / validation failure, 2 environment blocker, 3 launcher or usage error.
Hooks exit 0, or 2 only for a deliberate block; a usage problem is exit 3 (non-blocking). The launcher bin/dtt itself may exit 2 for a hook when no
Python is found and a .test-generator/runs folder exists.
argparse (allow_abbrev=False) handles everything except the plain `hook <name>` form, which skips it to save start-up time."""
from __future__ import annotations

import importlib
import os
import sys

HOOKS = {
    "session-resume": "session_resume",
    "guard-writes": "guard_writes",
}
# CLI commands: name -> module in dtt.cmd. Each module has add_arguments(parser) and run(args) -> exit code.
COMMANDS = {
    "scan": "scan",
    "update-state": "update_state",
    "validate-scenarios": "validate_scenarios",
    "render-scenarios": "render_scenarios",
    "scaffold-test-project": "scaffold",
    "lint-tests": "lint_tests",
    "run": "run",
    "verify-integrity": "verify_gate",
    "status": "status",
    "db": "db",
}
HELP = {
    "scan": "read-only first step: create the run folder and print the project, test stack, build-time code and blockers (max 25 lines)",
    "update-state": "change the step, status, fix path or note of a run",
    "validate-scenarios": "validate 01-scenarios.json of a run",
    "render-scenarios": "render the scenario table for the approval step",
    "scaffold-test-project": "create a new test project from the package allowlist",
    "lint-tests": "check test files for vacuous or weak tests",
    "run": "lint, build and run the generated tests; short result with hints and a round counter",
    "verify-integrity": "check that production files still match the run snapshot (--gate)",
    "status": "list unfinished runs (step, round, scenarios still without a trace comment)",
    "db": "throw-away test database in Docker: check | up | down | sweep",
}
EXIT_USAGE = 3


def _plugin_version():
    import json
    here = os.path.dirname(os.path.abspath(__file__))
    manifest = os.path.join(os.path.dirname(os.path.dirname(here)), ".claude-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8-sig") as fh:
            return str(json.load(fh).get("version", "unknown"))
    except (OSError, ValueError, AttributeError):
        return "unknown"


def _build_parser():
    import argparse

    class _Parser(argparse.ArgumentParser):
        def error(self, message):  # argparse would exit 2, which must never reach a hook
            self.print_usage(sys.stderr)
            sys.stderr.write("dtt: error: %s\n" % message.encode("ascii", "replace").decode("ascii"))
            raise SystemExit(EXIT_USAGE)

    parser = _Parser(prog="dtt", description="dotnet-test-team command line.",
                     allow_abbrev=False)
    parser.add_argument("--version", action="store_true", help="print the plugin version")
    parser.add_argument("--platform", action="store_true", help="print the detected platform record (for bug reports)")
    sub = parser.add_subparsers(dest="command", metavar="command")
    hook = sub.add_parser("hook", help="run a hook (reads the hook JSON on stdin)", allow_abbrev=False)
    hook.add_argument("name", choices=sorted(HOOKS))
    for name in COMMANDS:
        p = sub.add_parser(name, help=HELP[name], allow_abbrev=False, add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def _command_parser(name):
    import argparse

    class _P(argparse.ArgumentParser):
        def error(self, message):
            self.print_usage(sys.stderr)
            sys.stderr.write("dtt %s: error: %s%s" % (name, message.encode("ascii", "replace").decode("ascii"), chr(10)))
            raise SystemExit(EXIT_USAGE)

    parser = _P(prog="dtt " + name, allow_abbrev=False)
    module = importlib.import_module("dtt.cmd." + COMMANDS[name])
    module.add_arguments(parser)
    return parser, module


def _run_command(name, argv):
    parser, module = _command_parser(name)
    args = parser.parse_args(argv)
    from .cmd._common import run_command
    return run_command(name, module.run, args)


def _write(text):
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _run_hook(name):
    module = importlib.import_module("dtt.hooks." + HOOKS[name])
    try:
        code = module.main()
    except SystemExit:
        raise
    except BaseException as exc:  # last resort: the hooks handle their own errors; this never blocks
        sys.stderr.write("dotnet-test-team hook %s: internal error (%s)\n" % (name, type(exc).__name__))
        return 1
    return int(code) if isinstance(code, int) else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "hook" and argv[1] in HOOKS:
        return _run_hook(argv[1])
    if argv and argv[0] in COMMANDS:
        return _run_command(argv[0], argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        _write("dotnet-test-team " + _plugin_version())
        return 0
    if args.platform:
        import json
        from .core import platform as plat
        _write(json.dumps(plat.public_dict(plat.detect_probed()), indent=2, sort_keys=True, ensure_ascii=True))
        return 0
    if args.command == "hook":
        return _run_hook(args.name)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE
