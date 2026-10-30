"""Manifest loader.

A "manifest" is a yaml file describing one credential (or a related group
of credentials). Manifests live anywhere under a project directory (a dir
containing ``.infisical.json``); km globs for them recursively and treats
them uniformly.

The ``publishers:`` block holds one section per backing store. The only
publisher in this repo is ``infisical`` — its section has env-parent
shape: each top-level key is an Infisical environment slug, the value
is a mapping of local secret name → full Infisical path. Every secret
you want to push must be listed under every env it should exist in.
There is no scalar form, no per-env-name dict form, no implicit
"applies everywhere" semantics. Explicitness is the contract.

Underscore-prefixed meta keys inside a publisher section
(e.g. ``_owned``) declare publisher-specific metadata; the manifest
loader skips them and the publisher implementation interprets them.

Example::

    service: google
    secrets:
      client_id: REAL_VALUE
      client_secret: REAL_VALUE
    publishers:
      infisical:
        dev:
          client_id:     /oauth_client/google/client_id
          client_secret: /oauth_client/google/client_secret
        test:
          client_id:     /oauth_client/google/client_id
          client_secret: /oauth_client/google/client_secret
    rotation:
      runbook: ...
      groups: ...
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

TEMPLATE_FILENAMES = frozenset({"_template.yaml"})


@dataclass(frozen=True)
class ManifestEntry:
    path: str  # Infisical folder, e.g. `/jwt/account`
    name: str  # Infisical secret name, e.g. `private_key`
    format: str | None  # generator format (e.g. `ed25519`); None for vendor entries
    max_age_days: int  # rotation cadence; 0 means never stale
    group: str  # group key (yaml name or `<path>:<name>`); used by audit
    producer: str  # producer name from rotation.groups.<g>.producer; "" when not set (rotation will refuse, but audit/push tolerate it)
    upstream: str  # filename stem (e.g. "google" from "google.yaml")
    source_file: str  # path of the yaml relative to its project dir
    project: str  # project dir name
    env: str  # specific Infisical env (always set; loader rejects scalar shape)


def _split_infisical_path(full_path: str) -> tuple[str, str]:
    if not isinstance(full_path, str) or not full_path.startswith("/"):
        raise TypeError(f"infisical path must start with /, got: {full_path!r}")
    idx = full_path.rfind("/")
    if idx <= 0:
        return ("/", full_path[1:])
    return (full_path[:idx], full_path[idx + 1 :])


def _infisical_section(parsed: dict) -> dict:
    """Return the ``publishers.infisical`` block, or an empty dict.

    Treats a missing/empty ``publishers:`` or ``publishers.infisical:`` as
    "no Infisical addresses declared" — most commands tolerate that.
    Validation of the legacy ``infisical:`` top-level key happens in
    :mod:`km.commands._validate`, not here.
    """
    publishers = parsed.get("publishers") or {}
    if not isinstance(publishers, dict):
        return {}
    block = publishers.get("infisical") or {}
    if not isinstance(block, dict):
        return {}
    return block


def enumerate_secret_addresses(
    parsed: dict,
    *,
    file_label: str = "<manifest>",
) -> list[tuple[str, str, str]]:
    """Return ``[(local_name, infisical_path, env)]`` for every push target.

    The Infisical section must be env-parent shape::

        publishers:
          infisical:
            <env>:
              <local_name>: <infisical_path>
            _owned: [...]   # optional meta key, skipped here

    Other shapes (scalar, name-parent per-env dict) are rejected with a
    clear error — there is one supported shape and explicitness is the
    contract.
    """
    infisical = _infisical_section(parsed)
    if not infisical:
        return []

    out: list[tuple[str, str, str]] = []
    for env_name, env_map in infisical.items():
        if isinstance(env_name, str) and env_name.startswith("_"):
            # Reserved meta key (e.g. `_owned`); not an env.
            continue
        if not isinstance(env_name, str) or not env_name:
            raise ValueError(
                f"{file_label}: publishers.infisical key must be a non-empty env name "
                f"(got {env_name!r})"
            )
        if not isinstance(env_map, dict):
            raise ValueError(
                f"{file_label}: publishers.infisical[{env_name!r}] must be a mapping of "
                f"local_name → infisical_path (got {type(env_map).__name__}). "
                "The only supported shape is env-parent — list every "
                "secret under every env it should exist in, repeating as needed."
            )
        for local_name, ipath in env_map.items():
            if not isinstance(local_name, str) or not local_name:
                raise ValueError(
                    f"{file_label}: publishers.infisical[{env_name!r}] key must be a "
                    f"non-empty local secret name (got {local_name!r})"
                )
            if not isinstance(ipath, str) or not ipath.startswith("/"):
                raise ValueError(
                    f"{file_label}: publishers.infisical[{env_name!r}][{local_name!r}]: "
                    f"path must be a string starting with '/' (got {ipath!r})"
                )
            out.append((local_name, ipath, env_name))
    return out


def _read_manifest_file(file_path: Path) -> dict | None:
    raw = file_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    if parsed is None or not isinstance(parsed, dict):
        return None
    return parsed


def _iter_yaml_files(project_dir: Path) -> Iterable[Path]:
    for f in sorted(project_dir.rglob("*.yaml")):
        if f.is_file() and f.name not in TEMPLATE_FILENAMES:
            yield f


def _load_project(project_dir: Path, project_name: str) -> list[ManifestEntry]:
    if not project_dir.exists():
        raise FileNotFoundError(f"project directory does not exist: {project_dir}")
    if not project_dir.is_dir():
        raise NotADirectoryError(f"project path is not a directory: {project_dir}")

    out: list[ManifestEntry] = []
    for file_path in _iter_yaml_files(project_dir):
        try:
            parsed = _read_manifest_file(file_path)
        except yaml.YAMLError as e:
            raise ValueError(f"failed to parse {file_path}: {e}") from e
        if parsed is None:
            continue

        upstream = file_path.stem
        rel_source = file_path.relative_to(project_dir).as_posix()

        publishers_block = parsed.get("publishers")
        if publishers_block is not None and not isinstance(publishers_block, dict):
            raise ValueError(f"{file_path}: `publishers:` must be a mapping")
        infisical_section = (publishers_block or {}).get("infisical")
        if infisical_section is not None and not isinstance(infisical_section, dict):
            raise ValueError(f"{file_path}: `publishers.infisical:` must be a mapping")

        # The Infisical section is the source of truth for what this
        # manifest claims in Infisical. Every address declared there
        # becomes a ManifestEntry — rotation metadata (if any) just
        # enriches matching entries below.
        addresses_by_name: dict[str, list[tuple[str, str, str]]] = {}
        for local_name, ipath, env in enumerate_secret_addresses(parsed, file_label=str(file_path)):
            path, name = _split_infisical_path(ipath)
            addresses_by_name.setdefault(local_name, []).append((path, name, env))

        # (path, name, env) → enrichment from the rotation group that
        # claims it; addresses left out of any group fall through with
        # default rotation metadata.
        enrichment: dict[
            tuple[str, str, str],
            tuple[int, str, str | None, str],  # (max_age_days, producer, fmt, group_key)
        ] = {}

        rotation = parsed.get("rotation")
        groups = (rotation or {}).get("groups") or {}
        for group_name, group_data in groups.items():
            if not group_data or not isinstance(group_data, dict):
                raise ValueError(f"{file_path}: group {group_name!r} is not an object")

            max_age_days = group_data.get("max_age_days")
            producer_raw = group_data.get("producer")
            secrets = group_data.get("secrets")

            if not isinstance(secrets, list):
                raise ValueError(f"{file_path}: group {group_name!r} `secrets` must be an array")
            if len(secrets) == 0:
                continue
            if (
                not isinstance(max_age_days, int)
                or isinstance(max_age_days, bool)
                or max_age_days < 0
            ):
                raise ValueError(
                    f"{file_path}: group {group_name!r} `max_age_days` must be a "
                    "non-negative integer (0 = never stale)"
                )
            # Producer is optional at load time — required only by callers
            # that actually rotate (`km rotate`). Audit/push/validate
            # don't care.
            if producer_raw is None:
                producer = ""
            elif isinstance(producer_raw, str):
                producer = producer_raw
            else:
                raise ValueError(
                    f"{file_path}: group {group_name!r} `producer` must be a string if set"
                )

            resolved: list[tuple[str, str, str | None, str]] = []
            for secret in secrets:
                if not isinstance(secret, dict) or not isinstance(secret.get("name"), str):
                    raise ValueError(
                        f"{file_path}: group {group_name!r} secrets entries must be "
                        "{{ name, format? }} objects"
                    )
                local_name = secret["name"]
                fmt = secret.get("format")
                addresses = addresses_by_name.get(local_name, [])
                if not addresses:
                    continue  # local-only secret (no infisical mapping)
                for path, name, env in addresses:
                    resolved.append((path, name, fmt, env))

            if len(resolved) == 0:
                continue

            distinct_addrs = {(p, n) for p, n, _, _ in resolved}
            if len(distinct_addrs) == 1:
                path, name = next(iter(distinct_addrs))
                group_key = f"{path}:{name}"
            else:
                group_key = group_name

            for path, name, fmt, env in resolved:
                enrichment[(path, name, env)] = (
                    max_age_days,
                    producer,
                    fmt,
                    group_key,
                )

        # Emit one entry per declared address. Rotation-claimed
        # addresses get the group's metadata; the rest get defaults
        # (max_age=0, producer="", group="<path>:<name>").
        for addresses in addresses_by_name.values():
            for path, name, env in addresses:
                key = (path, name, env)
                if key in enrichment:
                    max_age_days, producer, fmt, group_key = enrichment[key]
                else:
                    max_age_days, producer, fmt = 0, "", None
                    group_key = f"{path}:{name}"
                out.append(
                    ManifestEntry(
                        path=path,
                        name=name,
                        format=fmt,
                        max_age_days=max_age_days,
                        group=group_key,
                        producer=producer,
                        upstream=upstream,
                        source_file=rel_source,
                        project=project_name,
                        env=env,
                    )
                )

    return out


def _validate_entries(entries: list[ManifestEntry]) -> None:
    by_addr: dict[tuple[str, str, str], ManifestEntry] = {}
    for e in entries:
        key = (e.path, e.name, e.env)
        if key in by_addr:
            prev = by_addr[key]
            raise ValueError(
                f"duplicate manifest entry for {e.path}/{e.name} (env={e.env}): "
                f'appears in "{prev.source_file}" and "{e.source_file}"'
            )
        by_addr[key] = e

    by_group: dict[str, ManifestEntry] = {}
    for e in entries:
        if e.group not in by_group:
            by_group[e.group] = e
            continue
        prev = by_group[e.group]
        if prev.producer != e.producer:
            raise ValueError(
                f'group "{e.group}" has mixed producers: "{prev.producer}" '
                f'({prev.source_file}) and "{e.producer}" ({e.source_file}) — '
                "group keys must be unique across files or share a single producer"
            )


def load_manifests(
    project_dirs: list[tuple[Path, str]] | list[Path],
) -> list[ManifestEntry]:
    """Load manifests from one or more project directories.

    ``project_dirs`` accepts bare ``Path`` items (project name = dir basename)
    or ``(Path, name)`` tuples to override the label. Each project's tree is
    recursively scanned; duplicate Infisical addresses (per-env) and
    producer conflicts are caught across the merged set.
    """
    all_entries: list[ManifestEntry] = []
    for item in project_dirs:
        if isinstance(item, tuple):
            project_dir, project_name = item
        else:
            project_dir = item
            project_name = Path(item).name
        all_entries.extend(_load_project(Path(project_dir).resolve(), project_name))
    _validate_entries(all_entries)
    return all_entries


def load_project_manifests(
    project_dir: Path, project_name: str | None = None
) -> list[ManifestEntry]:
    """Convenience: load entries from a single project."""
    name = project_name or project_dir.name
    entries = _load_project(Path(project_dir).resolve(), name)
    _validate_entries(entries)
    return entries
