"""Manifest discovery and per-target secrets, shared by the commands.

A manifest is ``<directories.manifests>/<slug>.yaml``; its slug is the
file stem, and every command names a manifest by it (F-56 D1).
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Any

from km.config import LoadedConfig
from km.manifest import TEMPLATE_FILENAMES
from km.project import Project
from km.publishers import ManifestRef
from km.targets import TargetValues, manifest_slug


def manifest_yaml_files(
    project: Project, cfg: LoadedConfig, slugs: Iterable[str] | None = None
) -> list[Path]:
    """The manifests directly under the manifests directory, sorted; with
    ``slugs``, only those (a target's list)."""
    manifests_dir = project.dir / cfg.directories.manifests
    if not manifests_dir.is_dir():
        return []
    wanted = None if slugs is None else set(slugs)
    return sorted(
        f
        for f in manifests_dir.glob("*.yaml")
        if f.is_file() and f.name not in TEMPLATE_FILENAMES and (wanted is None or f.stem in wanted)
    )


def nested_manifest_files(project: Project, cfg: LoadedConfig) -> list[Path]:
    """``*.yaml`` files below a subdirectory of the manifests directory:
    not manifests, since a manifest's slug is its stem (F-56 D1)."""
    manifests_dir = project.dir / cfg.directories.manifests
    if not manifests_dir.is_dir():
        return []
    return sorted(
        f
        for f in manifests_dir.rglob("*.yaml")
        if f.is_file() and f.parent != manifests_dir and f.name not in TEMPLATE_FILENAMES
    )


def resolve_slug(project: Project, cfg: LoadedConfig, slug: str) -> Path:
    """The manifest file for ``slug``; a usage error (exit 2) listing the
    slugs when none matches."""
    by_slug = {f.stem: f for f in manifest_yaml_files(project, cfg)}
    if slug not in by_slug:
        have = ", ".join(sorted(by_slug)) or "(none)"
        sys.stderr.write(f"error: no manifest {slug!r} (have: {have})\n")
        raise SystemExit(2)
    return by_slug[slug]


def slug_for(path: Path, project: Project, cfg: LoadedConfig) -> str:
    return manifest_slug(path, project.dir / cfg.directories.manifests)


def target_secrets(
    parsed: Mapping[str, Any],
    slug: str,
    target: TargetValues,
) -> tuple[dict[str, str], set[str]]:
    """The manifest's ``secrets:`` as strings with ``target``'s values for
    ``slug`` overlaid (F-54 Decision Q1), plus the overlaid names."""
    raw = parsed.get("secrets") or {}
    if not isinstance(raw, Mapping):
        raw = {}
    secrets = {k: ("" if v is None else str(v)) for k, v in raw.items() if isinstance(k, str)}
    return target.overlay(slug, secrets)


def declared_secrets(parsed: Mapping[str, Any]) -> set[str]:
    """The names the manifest's own ``secrets:`` declares."""
    raw = parsed.get("secrets") or {}
    return {k for k in raw if isinstance(k, str)} if isinstance(raw, Mapping) else set()


def undeclared_target_values(
    target: TargetValues,
    declared: Mapping[str, set[str] | None],
    unlisted: Collection[str] = (),
) -> list[str]:
    """Errors for target values that overlay nothing (F-54 Decision Q1: a
    per-target value is still declared in its manifest).

    ``declared`` maps every manifest slug the target lists to its
    declared names, or to ``None`` when the manifest did not parse
    (nothing can be said). ``unlisted`` are the slugs of the manifests
    the target does not list.
    """
    errors: list[str] = []
    for slug, values in sorted(target.manifests.items()):
        if slug in unlisted:
            errors.append(
                f"{target.label}: {slug!r} is a manifest target {target.name!r} does not "
                "list in its `manifests:`"
            )
            continue
        if slug not in declared:
            errors.append(f"{target.label}: {slug!r} names no manifest")
            continue
        names = declared[slug]
        if names is None:
            continue
        for name in sorted(set(values) - names):
            errors.append(
                f"{target.label}: {slug}.{name} is declared by no `secrets:` entry of "
                f"manifest {slug!r}; declare it there (empty value, `static` producer)"
            )
    return errors


def manifest_refs(
    publisher_name: str,
    manifests: list[tuple[Path, Mapping[str, Any], Mapping[str, str]]],
) -> list[ManifestRef]:
    """One :class:`ManifestRef` per manifest with a section for ``publisher_name``."""
    refs: list[ManifestRef] = []
    for path, parsed, secrets in manifests:
        block = parsed.get("publishers")
        if not isinstance(block, Mapping):
            continue
        section = block.get(publisher_name)
        if section is None:
            continue
        refs.append(ManifestRef(path=path, secrets=secrets, section=section))
    return refs


__all__ = [
    "declared_secrets",
    "manifest_refs",
    "manifest_yaml_files",
    "nested_manifest_files",
    "resolve_slug",
    "slug_for",
    "target_secrets",
    "undeclared_target_values",
]
