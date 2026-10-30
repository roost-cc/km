"""Manifest yaml IO.

The read side (:func:`load_manifest`) parses a manifest, env-expands
``$VAR`` / ``${VAR}`` references in every string value *except* those
under ``secrets:``, ``login:`` and ``notes:`` (operator-controlled cells
that might legitimately contain a literal ``$``), and returns a plain
dict.

:func:`effective_login` resolves a manifest's ``login:`` (inline, or
``from: <slug>`` naming another manifest's) into the mapping ``km login``,
``km mfa`` and the interactive producer read (F-56 D2);
:func:`login_errors` is its schema check.

The write side (:func:`write_secrets`) round-trips through ruamel so
comments and formatting survive, and only mutates the ``secrets:``
block plus ``rotation.groups[…].last_rotated`` — the rest of the file
(including any unexpanded ``$VAR`` references) is preserved as-is.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

from km._envexpand import expand as _expand_env_vars
from km.manifest import TEMPLATE_FILENAMES

# Top-level manifest keys whose sub-trees are NOT env-expanded. These
# hold operator-controlled values where a literal ``$`` could be
# part of the secret/credential and reinterpreting it would corrupt
# the value.
_NO_EXPAND_KEYS = frozenset({"secrets", "login", "notes"})

# The keys an inline ``login:`` may hold: what the login verb uses.
LOGIN_KEYS = frozenset({"url", "id", "mfa"})


_LAST_ROTATED_DEFAULT = object()


def load_manifest(path: Path) -> dict | None:
    """Read + parse a manifest yaml, env-expanding non-secret cells.

    Returns ``None`` for empty files or non-mapping top levels. Yaml
    parse errors propagate as :class:`yaml.YAMLError`. Undefined env
    vars exit with status 2 (consistent with the config loader).
    """
    raw = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    if parsed is None or not isinstance(parsed, dict):
        return None
    return _expand_env_vars(parsed, f"manifest {path}", skip_keys=_NO_EXPAND_KEYS)


def read_raw(path: Path) -> dict | None:
    """The manifest's yaml mapping as written (no env expansion), or None."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def slug_path(manifests_dir: Path, ref: Any) -> Path | None:
    """``<manifests_dir>/<ref>.yaml`` when ``ref`` is a slug naming a
    manifest; None otherwise. A name with ``/``, ``\\`` or a leading
    ``.`` never resolves, so it cannot reach outside the directory, and
    the manifest template is not a manifest."""
    if not isinstance(ref, str) or not ref or "/" in ref or "\\" in ref or ref.startswith("."):
        return None
    path = manifests_dir / f"{ref}.yaml"
    return path if path.name not in TEMPLATE_FILENAMES and path.is_file() else None


def _inline_login_errors(login: Mapping) -> list[str]:
    """Shape errors of an inline ``login:`` (no ``from``)."""
    if not login:
        return ["`login: {}` is empty; give it `url`, `id` or `mfa`, or `from: <slug>`"]
    errors: list[str] = []
    unknown = sorted(str(k) for k in login if k not in LOGIN_KEYS)
    if unknown:
        errors.append(
            f"`login:` has unknown key(s) {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(LOGIN_KEYS))}; other fields go in `notes:`)"
        )
    for key in sorted(LOGIN_KEYS & set(login)):
        if not isinstance(login[key], str):
            errors.append(f"`login.{key}` must be a string")
    return errors


def login_errors(parsed: Mapping[str, Any], manifests_dir: Path) -> list[str]:
    """Schema errors of the manifest's ``account:``, ``login:`` and
    ``notes:`` blocks (F-56 D2). An empty list means the shapes are good.

    ``login:`` is either ``{from: <slug>}`` alone, naming a manifest with
    an inline ``login:``, or a mapping of some of ``url``, ``id``,
    ``mfa``, each a string. ``notes:`` is a mapping km never reads.
    Messages name keys, never values.
    """
    errors: list[str] = []
    if "account" in parsed:
        errors.append("`account:` was renamed to `login:` (other fields go in `notes:`)")
    notes = parsed.get("notes", {})
    if not isinstance(notes, Mapping):
        errors.append("`notes:` must be a mapping")
    if "login" not in parsed:
        return errors
    login = parsed["login"]
    if not isinstance(login, Mapping):
        return [*errors, "`login:` must be a mapping"]
    if "from" not in login:
        return [*errors, *_inline_login_errors(login)]
    if len(login) > 1:
        others = ", ".join(sorted(str(k) for k in login if k != "from"))
        return [*errors, f"`login.from` stands alone (also has: {others})"]
    ref = login["from"]
    ref_path = slug_path(manifests_dir, ref)
    if ref_path is None:
        return [*errors, f"`login.from` names no manifest ({ref!r})"]
    referenced = read_raw(ref_path)
    ref_login = None if referenced is None else referenced.get("login")
    if not isinstance(ref_login, Mapping):
        return [*errors, f"`login.from`: manifest {ref!r} has no `login:` mapping"]
    if "from" in ref_login:
        return [
            *errors,
            f"`login.from`: manifest {ref!r} has `login.from` itself (one hop only)",
        ]
    if _inline_login_errors(ref_login):
        return [*errors, f"`login.from`: manifest {ref!r} has a malformed `login:`"]
    return errors


def effective_login(parsed: Mapping[str, Any], manifests_dir: Path) -> dict | None:
    """The manifest's effective login: its inline ``login:`` (or, with
    ``from``, the named manifest's inline ``login:``) plus ``password``
    from that same manifest's ``secrets.password`` when it is a string.

    Returns ``None`` when the manifest has no ``login:``. Raises
    :class:`ValueError` (naming keys, never values) when the blocks are
    malformed. ``from`` stands alone: nothing merges over the
    referenced login. The referenced manifest is read as written, with
    no env expansion, like ``login:`` itself.
    """
    errors = login_errors(parsed, manifests_dir)
    if errors:
        raise ValueError("; ".join(errors))
    login = parsed.get("login")
    if login is None:
        return None
    source = parsed
    if "from" in login:
        source = read_raw(slug_path(manifests_dir, login["from"]))
        login = source["login"]
    out = dict(login)
    secrets = source.get("secrets")
    if isinstance(secrets, Mapping) and isinstance(secrets.get("password"), str):
        out["password"] = secrets["password"]
    return out


def _round_trip_yaml() -> YAML:
    rt = YAML()
    rt.preserve_quotes = True
    rt.indent(mapping=2, sequence=4, offset=2)
    # Avoid the default 80-char wrap — long secrets on a single line are
    # easier to diff than wrapped ones.
    rt.width = 4096
    return rt


def write_secrets(
    manifest_path: Path,
    group_name: str,
    name_to_value: dict[str, str],
    *,
    remove: Iterable[str] = (),
    last_rotated: Any = _LAST_ROTATED_DEFAULT,
) -> None:
    """Update the manifest's ``secrets:`` block and ``last_rotated``.

    - Each key in ``name_to_value`` lands at the top-level ``secrets:``
      mapping (creating the block if absent).
    - Each name in ``remove`` is deleted from the ``secrets:`` mapping
      (silently no-op if absent).
    - ``rotation.groups[group_name].last_rotated`` is set:
        - to the value supplied via the ``last_rotated`` keyword argument
          if one was provided (any shape — a date object, an iso string,
          a list of {kid, date} mappings, etc.), or
        - to today's date in ISO format if no override was passed.
    - The write is atomic (temp file + ``os.replace``).
    """
    yaml = _round_trip_yaml()
    with manifest_path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        raise ValueError(f"manifest {manifest_path} is empty")

    secrets = data.get("secrets")
    if secrets is None:
        from ruamel.yaml.comments import CommentedMap

        secrets = CommentedMap()
        data["secrets"] = secrets
    for name, value in name_to_value.items():
        secrets[name] = value
    for name in remove:
        if name in secrets:
            del secrets[name]

    rotation = data.get("rotation")
    if rotation is None:
        raise ValueError(
            f"manifest {manifest_path}: missing `rotation:` block; cannot record last_rotated"
        )
    groups = rotation.get("groups")
    if groups is None or group_name not in groups:
        raise ValueError(
            f"manifest {manifest_path}: rotation.groups[{group_name!r}] "
            "is missing; cannot record last_rotated"
        )
    if last_rotated is _LAST_ROTATED_DEFAULT:
        groups[group_name]["last_rotated"] = dt.date.today().isoformat()
    else:
        groups[group_name]["last_rotated"] = last_rotated

    tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f)
        os.replace(tmp_path, manifest_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
