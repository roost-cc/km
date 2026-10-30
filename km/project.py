"""Project discovery via ``config.yaml``.

A "project" is any directory containing a ``config.yaml``. The directory
and all of its descendants belong to that project.

Two discovery patterns:

- :func:`find_project_for_cwd` — walk up from a starting directory until
  ``config.yaml`` is found. Used by every command that operates on a
  single project.
- :func:`discover_projects` — walk down from a root and return every
  directory containing a ``config.yaml``. Used by aggregate workflows.

This module does not load the config itself (see :mod:`km.config`); it
only locates project directories. Publisher- and producer-specific
configuration (e.g. Infisical workspace id, AWS profile) lives inside
the loaded config — the ``Project`` itself carries no publisher state.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from km.config import CONFIG_FILENAME


@dataclass(frozen=True)
class Project:
    """A directory containing ``config.yaml``."""

    dir: Path

    @property
    def name(self) -> str:
        return self.dir.name


def load_project(dir_path: Path) -> Project:
    """Bind a Project to a directory known to contain ``config.yaml``."""
    if not (dir_path / CONFIG_FILENAME).is_file():
        sys.stderr.write(f"{dir_path}: missing {CONFIG_FILENAME}\n")
        raise SystemExit(2)
    return Project(dir=dir_path.resolve())


def discover_projects(root: Path) -> list[Project]:
    """Walk ``root`` and return every directory containing ``config.yaml``.

    Sorted by directory name for stable output.
    """
    if not root.is_dir():
        sys.stderr.write(f"discovery root is not a directory: {root}\n")
        raise SystemExit(2)
    projects: list[Project] = []
    for cfg in sorted(root.rglob(CONFIG_FILENAME)):
        if cfg.is_file():
            projects.append(load_project(cfg.parent))
    return projects


def find_project_for_cwd(start: Path | None = None) -> Project:
    """Walk up from ``start`` until a directory has ``config.yaml``.

    ``start`` defaults to :func:`km.location.default_start` (``-C/--dir``, then
    ``$KM_PROJECT_DIR``, then the cwd).
    """
    from km.location import default_start

    origin = (start or default_start()).resolve()
    cur = origin
    while True:
        if (cur / CONFIG_FILENAME).is_file():
            return load_project(cur)
        if cur.parent == cur:
            sys.stderr.write(f"no {CONFIG_FILENAME} found walking up from {origin}\n")
            raise SystemExit(2)
        cur = cur.parent


def resolve_project(*, cwd: Path | None = None) -> Project:
    """Pick the active project by walking up from ``cwd`` (default: :func:`km.location.default_start`)."""
    return find_project_for_cwd(cwd)
