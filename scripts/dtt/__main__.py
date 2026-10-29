"""Entry point: python -I -X utf8 scripts/dtt/__main__.py <command> ...

Under -I the script directory is not on sys.path, so the package directory is inserted here.
"""
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from dtt.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
