#!/usr/bin/env python3
"""Python 3.9 compatibility guard for the claude-toolkit.

Imports every top-level toolkit module and every ``fetchers.*`` submodule,
printing ``OK``/``BROKEN`` per module and exiting non-zero if any import
fails. This is the foolproof check that nothing in the tree relies on
syntax/runtime features unavailable on Python 3.9 — most notably PEP 604
union annotations (``X | None``), which raise ``TypeError`` at runtime on 3.9
when the annotation is evaluated.

Run it with the 3.9 venv interpreter from the repo root::

    .venv/bin/python scripts/check_imports.py
"""
import importlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# claude_proxy.py rebinds sys.stdout/sys.stderr to a log file at import time
# when CLAUDE_PROXY_LOG_FILE is unset (it defaults to proxy.log). Setting it to
# an empty string makes that branch falsy, so importing the proxy (directly, or
# transitively via analyze_probe_dump) leaves our stdout intact for OK/BROKEN
# reporting. This only affects import-time logging config, not behavior.
os.environ["CLAUDE_PROXY_LOG_FILE"] = ""

# Modules to skip: the quiz_* suite is the user's separately-tracked work, and a
# couple of them (and hook_turn_start) execute disruptive side effects at import
# (MongoDB access, file writes, stdin reads) that have nothing to do with the
# 3.9-compat check.
SKIP_TOP_LEVEL = {
    "quiz_check",
    "quiz_data",
    "quiz_dismiss",
    "quiz_grade",
    "quiz_save",
    "hook_turn_start",  # writes ~/.claude/turn-start at import time
}


def _discover_modules():
    """Return the ordered list of dotted module names to import."""
    names = []
    for path in sorted(REPO_ROOT.glob("*.py")):
        stem = path.stem
        if stem in SKIP_TOP_LEVEL:
            continue
        names.append(stem)

    fetchers_dir = REPO_ROOT / "fetchers"
    if fetchers_dir.is_dir():
        for path in sorted(fetchers_dir.glob("*.py")):
            if path.stem == "__init__":
                names.append("fetchers")
            else:
                names.append(f"fetchers.{path.stem}")
    return names


def main():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    real_stdout = sys.stdout
    real_stderr = sys.stderr

    modules = _discover_modules()
    failures = []
    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - report anything, incl. SystemExit
            # A module may have hijacked stdout/stderr during a partial import.
            sys.stdout = real_stdout
            sys.stderr = real_stderr
            failures.append(name)
            print(f"BROKEN  {name}: {type(exc).__name__}: {exc}")
        else:
            sys.stdout = real_stdout
            sys.stderr = real_stderr
            print(f"OK      {name}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(modules)} module(s) broken: "
              f"{', '.join(failures)}")
        return 1
    print(f"All {len(modules)} module(s) import clean on "
          f"Python {sys.version_info.major}.{sys.version_info.minor}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
