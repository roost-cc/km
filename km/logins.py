"""Logins: the manifests with a ``login:`` block (F-56 D2).

Shared by ``km mfa`` and ``km login``. A login is named by its
manifest's slug; its fields are the manifest's effective login
(:func:`km.manifest_io.effective_login`). Nothing here prints or
returns a field's value in a message.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from km.manifest import TEMPLATE_FILENAMES
from km.manifest_io import effective_login, read_raw, slug_path


def manifests_dir(cfg, project) -> Path:
    """The project's resolved manifests directory (it may not exist)."""
    return (project.dir / cfg.directories.manifests).resolve()


def manifest_path(manifests_dir: Path, slug: str) -> Path | None:
    """The manifest for ``slug``, or None if the name is unsafe or unknown
    (:func:`km.manifest_io.slug_path`)."""
    return slug_path(manifests_dir, slug)


def load(manifests_dir: Path, slug: str) -> dict | None:
    """The effective login of manifest ``slug``.

    Returns None when the slug names no manifest, the manifest does not
    parse, or it has no ``login:``. Raises :class:`ValueError` (naming
    keys, never values) when its login blocks are malformed.
    """
    path = manifest_path(manifests_dir, slug)
    if path is None:
        return None
    data = read_raw(path)
    if data is None:
        return None
    return effective_login(data, manifests_dir)


def has_string(field: str) -> Callable[[dict], bool]:
    """Return a predicate: the login's ``field`` is a non-empty string."""

    def predicate(login: dict) -> bool:
        value = login.get(field)
        return isinstance(value, str) and bool(value.strip())

    return predicate


def login_slugs(manifests_dir: Path, predicate: Callable[[dict], bool] | None = None) -> list[str]:
    """The sorted slugs of manifests with a ``login:`` whose effective
    login satisfies ``predicate`` (any, when None). Manifests that do
    not parse or whose login blocks are malformed are skipped."""
    slugs = []
    if not manifests_dir.is_dir():
        return slugs
    for p in manifests_dir.glob("*.yaml"):
        if p.name in TEMPLATE_FILENAMES:
            continue
        try:
            login = load(manifests_dir, p.stem)
        except ValueError:
            continue
        if login is not None and (predicate is None or predicate(login)):
            slugs.append(p.stem)
    return sorted(slugs)


def unknown_message(
    slug: str, manifests_dir: Path, predicate: Callable[[dict], bool] | None, kind: str
) -> str:
    """Return ``no login '<slug>'; <kind>: a, b`` (or ``none``)."""
    names = login_slugs(manifests_dir, predicate)
    known = ", ".join(names) if names else "none"
    return f"no login {slug!r}; {kind}: {known}"
