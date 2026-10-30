"""Where `km` starts looking for a project's ``config.yaml``.

Commands that are given no explicit path walk up from a starting directory to find the project
(``config.yaml``). That starting directory is, in order of precedence:

1. ``-C/--dir`` on the command line;
2. the ``KM_PROJECT_DIR`` environment variable, when set and non-empty;
3. the current working directory (the original behaviour).

An unset or empty variable falls through to the cwd. A value that is not a directory is an error
naming its source, so a typo never silently falls back. The walk-up still applies from whichever
start wins, so a subdirectory of the project works too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_VAR = "KM_PROJECT_DIR"

_cli_dir: str | None = None


def set_cli_dir(path: str | None) -> None:
    """Record ``-C/--dir`` from the command line (``None`` clears it)."""
    global _cli_dir
    _cli_dir = path or None


def default_start() -> Path:
    """The directory to walk up from when a command is given no explicit path."""
    if _cli_dir:
        return _checked(Path(_cli_dir).expanduser(), "--dir")
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return _checked(Path(env).expanduser(), f"${ENV_VAR}")
    return Path.cwd()


def _checked(path: Path, source: str) -> Path:
    if not path.is_dir():
        sys.stderr.write(f"{source}={path}: not a directory\n")
        raise SystemExit(2)
    return path
