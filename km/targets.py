"""Per-target values: ``<project>/<directories.targets>/<target>.yaml``.

A few secrets exist per secret-manager instance and can never be shared
between targets (an Infisical project id, the token the services read
with, km's own publishing token). They live in one file per target, in
the credentials repo, git-crypted like the manifests (F-54 D4).

File shape (F-54 Decision Q1)::

    # targets/local.yaml
    infisical:                      # manifest slug
      secret_manager_token: eyJ…    # <secret>: <value>
      secret_manager_project: dd26…
    _publishers:                    # publisher-held state, by publisher name
      infisical:
        project_id: dd26…
        token: eyJ…

- A top-level ``<manifest slug>: {<secret>: <value>}`` mapping is
  overlaid onto that manifest's ``secrets:`` before publish and
  validate. The secret is still declared in its manifest (name, empty
  value, ``static`` producer); minting skips overlaid names. The slug is
  the manifest's file stem (``manifests/infisical.yaml`` → ``infisical``).
- ``_publishers.<publisher>`` holds what a publisher needs to reach the
  target's instance (km's own credentials). Manifest slugs never start
  with ``_``, so the key cannot collide with one.

A missing file is an empty set of values. Publishers' ``ensure``
(S-958) records what it created with :func:`update`, which writes only
the keys it is given, under a lock on the targets directory, so two
``km publish`` runs on one target never erase each other's values.
``km publish`` runs each ensure inside :func:`ensuring`, which holds
that lock, so two runs never both create one resource.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PUBLISHERS_KEY = "_publishers"


@dataclass
class TargetValues:
    """The parsed contents of one ``targets/<target>.yaml``."""

    name: str
    path: Path
    manifests: dict[str, dict[str, str]] = field(default_factory=dict)
    publishers: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.path.parent.name}/{self.path.name}"

    def overlay(self, slug: str, secrets: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
        """Return ``secrets`` with this target's values for ``slug`` laid
        over it, and the set of overlaid names."""
        values = self.manifests.get(slug) or {}
        merged = dict(secrets)
        merged.update(values)
        return merged, set(values)

    def publisher_state(self, publisher: str) -> dict[str, Any]:
        return dict(self.publishers.get(publisher) or {})


def target_path(project_dir: Path, targets_dir: str, name: str) -> Path:
    return (project_dir / targets_dir / f"{name}.yaml").resolve()


def manifest_slug(manifest_path: Path, manifests_dir: Path) -> str:
    """``manifests/apple-user.yaml`` → ``apple-user`` (F-56 D1).

    Raises :class:`ValueError` for a path not directly under
    ``manifests_dir``: a nested file is not a manifest and has no slug.
    """
    if manifest_path.resolve().parent != manifests_dir.resolve():
        raise ValueError(f"{manifest_path}: not a manifest directly under {manifests_dir}")
    return manifest_path.stem


def load(project_dir: Path, targets_dir: str, name: str) -> TargetValues:
    """Read ``targets/<name>.yaml``; a missing file is an empty value set.

    A malformed file is a hard error (exit 2) naming the file.
    """
    path = target_path(project_dir, targets_dir, name)
    out = TargetValues(name=name, path=path)
    if not path.is_file():
        return out
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        sys.stderr.write(f"error: {path}: {e}\n")
        raise SystemExit(2) from e
    if raw is None:
        return out
    if not isinstance(raw, dict):
        _fail(path, "top-level must be a mapping")
    for key, body in raw.items():
        if not isinstance(key, str) or not key:
            _fail(path, f"key must be a non-empty string (got {key!r})")
        if not isinstance(body, dict):
            _fail(path, f"{key}: must be a mapping")
        if key == PUBLISHERS_KEY:
            for pub, state in body.items():
                if not isinstance(pub, str) or not isinstance(state, dict):
                    _fail(path, f"{PUBLISHERS_KEY}.{pub}: must be a mapping")
                out.publishers[pub] = dict(state)
            continue
        if key.startswith("_"):
            _fail(path, f"unknown reserved key {key!r} (only {PUBLISHERS_KEY!r})")
        values: dict[str, str] = {}
        for secret, value in body.items():
            if not isinstance(secret, str) or not secret:
                _fail(path, f"{key}: secret name must be a non-empty string")
            if value is not None and not isinstance(value, (str, int, float, bool)):
                _fail(path, f"{key}.{secret}: value must be a scalar")
            values[secret] = "" if value is None else str(value)
        out.manifests[key] = values
    return out


def update(
    values: TargetValues,
    *,
    manifests: Mapping[str, Mapping[str, str]] | None = None,
    publishers: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Set the given ``<slug>.<secret>`` and ``_publishers.<pub>.<key>``
    values in ``values`` and in its file.

    The file is re-read under the lock and only these keys change, so a
    value another run wrote since ``values`` was loaded is kept.
    ``values`` changes only once the file is written: when the write
    fails, it still describes the file, so no caller goes on with a
    credential that was never recorded.
    """
    with _locked(values.path.parent):
        current = load(values.path.parent, ".", values.path.stem)
        for slug, body in (manifests or {}).items():
            current.manifests.setdefault(slug, {}).update(body)
        for pub, body in (publishers or {}).items():
            current.publishers.setdefault(pub, {}).update(body)
        _replace(current)
    for slug, body in (manifests or {}).items():
        values.manifests.setdefault(slug, {}).update(body)
    for pub, body in (publishers or {}).items():
        values.publishers.setdefault(pub, {}).update(body)


@contextlib.contextmanager
def ensuring(values: TargetValues) -> Iterator[set[tuple[str, str]]]:
    """Hold the targets directory's lock around one destination's ensure.

    Two runs on one target then never both find a resource missing and
    both create it: the second waits, and ``values`` is re-read under
    the lock, so it sees what the first recorded. Yields the
    ``(slug, secret)`` values that re-read changed.
    """
    with _locked(values.path.parent):
        current = load(values.path.parent, ".", values.path.stem)
        changed = {
            (slug, secret)
            for slug, body in current.manifests.items()
            for secret, value in body.items()
            if (values.manifests.get(slug) or {}).get(secret) != value
        }
        for slug, body in current.manifests.items():
            values.manifests.setdefault(slug, {}).update(body)
        for pub, body in current.publishers.items():
            values.publishers.setdefault(pub, {}).update(body)
        yield changed


_held = threading.local()


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``directory`` (the targets directory).

    Re-entrant within a thread, so :func:`update` can run inside
    :func:`ensuring`."""
    held: dict[Path, int] = _held.__dict__.setdefault("dirs", {})
    if held.get(directory):
        held[directory] += 1
        try:
            yield
        finally:
            held[directory] -= 1
        return
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        held[directory] = 1
        try:
            yield
        finally:
            held[directory] = 0
    finally:
        os.close(fd)


def _replace(values: TargetValues) -> None:
    """Write ``values`` to its file atomically (manifest slugs first, then
    ``_publishers``); the caller holds the lock."""
    data: dict[str, Any] = {slug: dict(v) for slug, v in values.manifests.items()}
    if values.publishers:
        data[PUBLISHERS_KEY] = {p: dict(s) for p, s in values.publishers.items()}
    fd, tmp_name = tempfile.mkstemp(
        dir=values.path.parent, prefix=f".{values.path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=4096))
        os.replace(tmp, values.path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fail(path: Path, msg: str) -> None:
    sys.stderr.write(f"error: {path}: {msg}\n")
    raise SystemExit(2)


__all__ = [
    "PUBLISHERS_KEY",
    "TargetValues",
    "ensuring",
    "load",
    "manifest_slug",
    "target_path",
    "update",
]
