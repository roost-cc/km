"""What `km publish --only <slug>[:<name>]` selects (F-54 Decision S2).

A selector is ``<slug>`` (every secret of that manifest) or
``<slug>:<name>``, where ``<name>`` is a rotation group of the manifest
(its secrets) or else one of the manifest's secrets. Groups are looked
up with :func:`rotation_groups`, the same lookup ``km rotate --force``
uses. The resolved selection maps a manifest slug to the secret names
selected in it; ``None`` (``--all``) selects everything.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

Selection = dict[str, set[str]]


class SelectionError(ValueError):
    """A selector that is malformed or names nothing (a usage error)."""


def rotation_groups(parsed: Mapping[str, Any]) -> dict[str, dict]:
    """The manifest's ``rotation.groups`` mapping (``{}`` when absent)."""
    rotation = parsed.get("rotation") or {}
    groups = rotation.get("groups") if isinstance(rotation, Mapping) else None
    if not isinstance(groups, Mapping):
        return {}
    return {k: v for k, v in groups.items() if isinstance(k, str) and isinstance(v, dict)}


def group_secret_names(gdata: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for s in gdata.get("secrets") or []:
        name = s.get("name") if isinstance(s, Mapping) else s
        if isinstance(name, str):
            out.append(name)
    return out


def parse_selector(raw: str) -> tuple[str, str | None]:
    """``"synapse:roost_bot"`` → ``("synapse", "roost_bot")``; ``"synapse"`` →
    ``("synapse", None)``."""
    slug, sep, name = raw.partition(":")
    if not slug or (sep and not name) or ":" in name:
        raise SelectionError(f"--only {raw!r}: expected <slug> or <slug>:<group-or-secret>")
    return slug, (name if sep else None)


def resolve(
    selectors: Iterable[str],
    manifests: Mapping[str, tuple[Mapping[str, Any], Mapping[str, str]]],
    *,
    target: str | None = None,
    unlisted: Collection[str] = (),
) -> Selection:
    """Resolve ``selectors`` against ``manifests`` (slug → (parsed, secrets)),
    the manifests ``target`` lists.

    Raises :class:`SelectionError` for a slug in ``unlisted`` (a manifest
    the target does not publish), an unknown slug, or a name that is
    neither a rotation group nor a secret of the manifest; the message
    lists what the target or manifest offers.
    """
    out: Selection = {}
    for raw in selectors:
        slug, name = parse_selector(raw)
        if slug in unlisted:
            raise SelectionError(
                f"--only {raw!r}: target {target!r} does not publish manifest {slug!r} "
                f"(its `manifests:` lists: {', '.join(sorted(manifests)) or '-'})"
            )
        if slug not in manifests:
            raise SelectionError(
                f"--only {raw!r}: no manifest {slug!r} (have: {', '.join(sorted(manifests))})"
            )
        parsed, secrets = manifests[slug]
        groups = rotation_groups(parsed)
        if name is None:
            names = set(secrets)
            for gdata in groups.values():
                names.update(group_secret_names(gdata))
        elif name in groups:
            names = set(group_secret_names(groups[name]))
        elif name in secrets:
            names = {name}
        else:
            raise SelectionError(
                f"--only {raw!r}: {name!r} is neither a rotation group nor a secret of "
                f"{slug!r} (groups: {', '.join(sorted(groups)) or '-'}; "
                f"secrets: {', '.join(sorted(secrets)) or '-'})"
            )
        out.setdefault(slug, set()).update(names)
    return out


__all__ = [
    "Selection",
    "SelectionError",
    "group_secret_names",
    "parse_selector",
    "resolve",
    "rotation_groups",
]
