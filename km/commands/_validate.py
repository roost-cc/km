"""`km validate <target> [<env>...]` — is the target in a valid state?

Only the manifests the target lists are checked, except that every
manifest's ``login:`` and ``notes:`` are (a login-only manifest belongs
to no target). Three gates, run in order:

1. **Schema**: no ``*.yaml`` sits below a subdirectory of the manifests
   directory (a manifest is ``<slug>.yaml`` directly in it, F-56 D1);
   every listed manifest parses; required keys present;
   producer/publisher names resolve via the loader; per-publisher
   ``validate()`` checks pass; every per-target value overlays a secret
   its manifest declares.
2. **Freshness**: every rotation group's ``last_rotated`` is
   non-null and within ``max_age_days``.
3. **Publication**: read back through every destination of the target
   (the same publishers ``km publish`` writes through), for the given
   envs (default: every env the target accepts), with the target's
   per-target values overlaid. Reported in three buckets:
   ``missing`` (the destination doesn't have the secret at all),
   ``differs`` (it has it but the value diverges from the manifest —
   typically what you see right after ``km rotate`` and before
   ``km publish``), and ``store_only`` (the destination holds a secret
   no manifest declares — the mirror of ``missing``).

The system is **valid** iff all three pass; a ``store_only`` row fails
it like a ``missing`` one, since nothing is created in a store by hand
(F-54 D6). ``template_file`` has no store-only (the rendered file is the
store). Schema errors short-circuit the file (freshness/publication
checks skip files that didn't parse); freshness and publication run
independently.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from km import targets as targets_mod
from km.commands._manifests import (
    declared_secrets,
    manifest_refs,
    manifest_yaml_files,
    nested_manifest_files,
    slug_for,
    target_secrets,
    undeclared_target_values,
)
from km.config import LoadedConfig, resolve_config_path
from km.config import load as load_config
from km.loader import producer_class, schema_publisher
from km.manifest import enumerate_secret_addresses
from km.manifest_io import load_manifest, login_errors
from km.project import Project, load_project
from km.publishers import Publisher
from km.publishers._discovery import (
    LoadedDestination,
    build_destinations,
    close_all,
)


def _warn(msg: str) -> None:
    sys.stderr.write(f"warn: {msg}\n")


# ---------- result rows ----------


@dataclass
class StaleRow:
    manifest_path: str  # project-relative
    group_name: str
    last_rotated: Any  # raw value from yaml (may be None / scalar / list)
    max_age_days: int
    age_days: int | None  # None when never rotated


@dataclass
class MissingRow:
    """Publisher has no entry at the address the manifest declares."""

    manifest_path: str  # project-relative
    local_name: str | None
    env: str
    publisher: str
    store_address: str


@dataclass
class DiffersRow:
    """Publisher has an entry but the value diverges from the manifest."""

    manifest_path: str
    local_name: str | None
    env: str
    publisher: str
    store_address: str
    manifest_value: str | None
    store_value: str | None


@dataclass
class StoreOnlyRow:
    """Destination holds a secret no manifest declares."""

    env: str
    publisher: str
    store_address: str


@dataclass
class ValidateResult:
    project_name: str
    project_dir: Path
    manifests_dir: str  # project-relative
    files_checked: int
    target: str = ""
    envs: list[str] = field(default_factory=list)
    schema_errors: dict[str, list[str]] = field(default_factory=dict)  # rel_path -> [error]
    stale: list[StaleRow] = field(default_factory=list)
    missing: list[MissingRow] = field(default_factory=list)
    differs: list[DiffersRow] = field(default_factory=list)
    store_only: list[StoreOnlyRow] = field(default_factory=list)
    publication_errors: list[str] = field(default_factory=list)  # publisher reach failures

    @property
    def valid(self) -> bool:
        return (
            not self.schema_errors
            and not self.stale
            and not self.missing
            and not self.differs
            and not self.store_only
            and not self.publication_errors
        )


# ---------- schema gate ----------


def _schema_publisher(
    name: str,
    project: Project,
    cfg: LoadedConfig,
    publishers: Mapping[str, Publisher] | None,
) -> Publisher | None:
    """The target's instance for ``name`` if it has one, else a
    configuration-less instance (None if the class needs configuration).
    Raises when the name does not resolve."""
    if publishers and name in publishers:
        return publishers[name]
    return schema_publisher(name, project, cfg)


def _validate_file(
    path: Path,
    project: Project,
    cfg: LoadedConfig,
    publishers: Mapping[str, Publisher] | None = None,
) -> tuple[list[str], dict | None]:
    """Schema-check one manifest. Returns (errors, parsed_or_None).

    ``parsed`` is None when the file failed to parse as a yaml mapping;
    callers skip freshness/publication for those files. ``publishers``
    (by name) are the target's instances used for section checks; a
    publisher the target lacks is checked when it constructs without
    configuration, and otherwise only for resolving.
    """
    errors: list[str] = []
    try:
        data = load_manifest(path)
    except yaml.YAMLError as e:
        return [f"{path.name}: YAML parse error: {e}"], None
    except (FileNotFoundError, ValueError) as e:
        return [f"{path.name}: {e}"], None
    if data is None:
        return [f"{path.name}: top-level must be a mapping"], None

    loc = path.relative_to(project.dir).as_posix()

    manifests_dir = (project.dir / cfg.directories.manifests).resolve()
    errors.extend(f"{loc}: {e}" for e in login_errors(data, manifests_dir))

    secrets = data.get("secrets") or {}
    if not isinstance(secrets, dict):
        errors.append(f"{loc}: `secrets:` must be a mapping")
        secrets = {}

    publishers_block = data.get("publishers")
    if publishers_block is not None and not isinstance(publishers_block, dict):
        errors.append(f"{loc}: `publishers:` must be a mapping")
        publishers_block = {}
    publishers_block = publishers_block or {}

    for k, v in secrets.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            errors.append(f"{loc}: secrets['{k}']: value must be a scalar (got {type(v).__name__})")

    for pub_name, section in publishers_block.items():
        if not isinstance(pub_name, str) or not pub_name:
            errors.append(f"{loc}: publishers key must be a non-empty string (got {pub_name!r})")
            continue
        try:
            publisher = _schema_publisher(pub_name, project, cfg, publishers)
        except Exception as err:  # noqa: BLE001
            errors.append(f"{loc}: publisher {pub_name!r}: {err}")
            continue
        if publisher is None:
            continue
        errors.extend(publisher.validate(manifest_path=path, section=section, secrets=secrets))

    try:
        addresses = enumerate_secret_addresses(data, file_label=loc)
    except ValueError as e:
        errors.append(str(e))
        addresses = []

    if secrets:
        for local_name, _ipath, _env in addresses:
            if local_name not in secrets:
                errors.append(f"{loc}: infisical entry for '{local_name}' has no matching secret")

    rotation = data.get("rotation")
    if rotation is None:
        if data.get("secrets") or data.get("publishers"):
            errors.append(
                f"{loc}: login-only manifests must omit `secrets:` and "
                "`publishers:` (no `rotation:` block declared)"
            )
        return errors, data
    if not isinstance(rotation, dict):
        errors.append(f"{loc}: `rotation:` must be a mapping")
        return errors, data

    if not data.get("publishers") and not data.get("secrets"):
        errors.append(
            f"{loc}: `rotation:` declared but neither `secrets:` nor "
            "`publishers:` — interactive rotation has nothing to rotate"
        )

    runbook = rotation.get("runbook")
    if not isinstance(runbook, str) or not runbook:
        errors.append(f"{loc}: `rotation.runbook` is required (non-empty string)")
    # Existence of the referenced runbook file is intentionally not
    # checked here — runbook directory resolution is the next refactor
    # round. Rotation surfaces a clear message at use-time if the file
    # is missing.

    groups_raw = rotation.get("groups")
    if groups_raw is None or (isinstance(groups_raw, dict) and not groups_raw):
        errors.append(f"{loc}: `rotation.groups` must be a non-empty mapping")
        groups = {}
    elif not isinstance(groups_raw, dict):
        errors.append(f"{loc}: `rotation.groups` must be a non-empty mapping")
        groups = {}
    else:
        groups = groups_raw

    grouped_names: set[str] = set()
    declared_in_publisher: set[str] = set()
    for section in (publishers_block or {}).values():
        # template_file shape: list of {template, output, envs} specs.
        if isinstance(section, list):
            for spec in section:
                if not isinstance(spec, dict):
                    continue
                spec_envs = spec.get("envs")
                if not isinstance(spec_envs, dict):
                    continue
                for body in spec_envs.values():
                    if not isinstance(body, dict):
                        continue
                    for name in body.keys():
                        if isinstance(name, str):
                            declared_in_publisher.add(name)
            continue
        if not isinstance(section, dict):
            continue
        for sub_key, env_map in section.items():
            if isinstance(sub_key, str) and sub_key.startswith("_"):
                continue
            if not isinstance(env_map, dict):
                continue
            for name in env_map.keys():
                if isinstance(name, str):
                    declared_in_publisher.add(name)

    for gname, g in groups.items():
        if not isinstance(g, dict) or not g:
            errors.append(f"{loc}: group '{gname}' is not a non-empty mapping")
            continue
        producer = g.get("producer")
        if producer is None:
            errors.append(f"{loc}: group {gname!r}: `producer:` is required")
        elif not isinstance(producer, str):
            errors.append(f"{loc}: group {gname!r}: `producer:` must be a string")
        else:
            try:
                producer_class(producer, project, cfg)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{loc}: group {gname!r}: producer {producer!r}: {err}")
        max_age = g.get("max_age_days")
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
            errors.append(f"{loc}: group '{gname}': max_age_days must be a non-negative int")
        elif max_age == 0 and isinstance(producer, str) and producer != "static":
            errors.append(
                f"{loc}: group {gname!r}: max_age_days=0 is only valid with "
                "'producer: static' (the never-rotated convention)."
            )
        last_rotated = g.get("last_rotated")
        if last_rotated is not None:
            from km.commands._rotate import last_rotated_date

            if isinstance(last_rotated, (str, dt.date)):
                if last_rotated_date(last_rotated) is None:
                    errors.append(f"{loc}: group '{gname}': last_rotated invalid")
            else:
                errors.append(
                    f"{loc}: group '{gname}': last_rotated invalid: "
                    f"unexpected type {type(last_rotated).__name__}"
                )

        gsecrets = g.get("secrets") or []
        if not isinstance(gsecrets, list):
            errors.append(f"{loc}: group '{gname}': secrets must be a list")
            continue
        if not gsecrets:
            errors.append(f"{loc}: group '{gname}': secrets list must not be empty")
            continue
        for s in gsecrets:
            sname = s.get("name") if isinstance(s, dict) else s
            if not isinstance(sname, str) or not sname:
                errors.append(f"{loc}: group '{gname}': bad secret entry {s!r}")
                continue
            if sname in grouped_names:
                errors.append(f"{loc}: secret '{sname}' appears in multiple groups")
            grouped_names.add(sname)
            if secrets and sname not in secrets and sname not in declared_in_publisher:
                errors.append(
                    f"{loc}: group '{gname}' references '{sname}' which is in neither "
                    "secrets nor publishers"
                )

    return errors, data


# ---------- freshness gate ----------


def _check_freshness(parsed: dict, manifest_path: Path, project_dir: Path) -> list[StaleRow]:
    """Return one StaleRow per rotation group whose last_rotated is null
    or older than max_age_days. Schema-broken groups are skipped."""
    from km.commands._rotate import last_rotated_date

    rotation = parsed.get("rotation")
    if not isinstance(rotation, dict):
        return []
    groups = rotation.get("groups") or {}
    if not isinstance(groups, dict):
        return []
    today = dt.date.today()
    rel = manifest_path.relative_to(project_dir).as_posix()
    out: list[StaleRow] = []
    for gname, g in groups.items():
        if not isinstance(g, dict):
            continue
        max_age = g.get("max_age_days") or 0
        if not isinstance(max_age, int) or isinstance(max_age, bool):
            continue
        if max_age == 0:
            continue  # never-auto-rotated group
        last = g.get("last_rotated")
        last_date = last_rotated_date(last)
        if last_date is None:
            out.append(
                StaleRow(
                    manifest_path=rel,
                    group_name=gname,
                    last_rotated=last,
                    max_age_days=max_age,
                    age_days=None,
                )
            )
            continue
        age = (today - last_date).days
        if age >= max_age:
            out.append(
                StaleRow(
                    manifest_path=rel,
                    group_name=gname,
                    last_rotated=last,
                    max_age_days=max_age,
                    age_days=age,
                )
            )
    return out


# ---------- publication gate ----------


def _check_publication(
    parsed_per_file: dict[Path, dict],
    project: Project,
    cfg: LoadedConfig,
    loaded: list[LoadedDestination],
    target: targets_mod.TargetValues,
    envs: list[str],
) -> tuple[list[MissingRow], list[DiffersRow], list[StoreOnlyRow], list[str]]:
    """Run Publisher.collect() per (destination, env) and split the
    four-state rows into the user-facing buckets: ``missing``,
    ``differs`` and ``store_only``.

    Returns (missing, differs, store_only, reach_errors). Reach errors
    fire when a destination's publisher fails to load or raises during
    collect — they invalidate the system on their own (we couldn't
    confirm publication state).
    """
    missing: list[MissingRow] = []
    differs: list[DiffersRow] = []
    store_only: list[StoreOnlyRow] = []
    reach_errors: list[str] = []

    triples = []
    for fp, parsed in parsed_per_file.items():
        secrets, _overlaid = target_secrets(parsed, slug_for(fp, project, cfg), target)
        triples.append((fp, parsed, secrets))

    for ld in loaded:
        dest, publisher = ld.destination, ld.publisher
        if publisher is None:
            reach_errors.append(
                f"destination {dest.label}: publisher {dest.publisher!r} failed to load — "
                f"{ld.error}"
            )
            continue
        refs = manifest_refs(dest.publisher, triples)
        # Read every accepted env back, claimed by a manifest or not: a
        # store can hold strays in an env no manifest publishes to.
        dest_envs = [e for e in envs if e in dest.envs]
        if not publisher.env_partitioned:
            dest_envs = dest_envs[:1]
        for env in dest_envs:
            try:
                result = publisher.collect(env=env, manifests=refs)
            except Exception as err:  # noqa: BLE001
                reach_errors.append(f"destination {dest.label} env={env!r}: collect failed — {err}")
                continue
            for row in result.entries:
                if row.state == "same":
                    continue
                if row.state == "store_only":
                    store_only.append(
                        StoreOnlyRow(
                            env=env, publisher=dest.publisher, store_address=row.store_address
                        )
                    )
                    continue
                rel = ""
                if row.manifest_path is not None:
                    try:
                        rel = row.manifest_path.relative_to(project.dir).as_posix()
                    except ValueError:
                        rel = str(row.manifest_path)
                if row.state == "differs":
                    differs.append(
                        DiffersRow(
                            manifest_path=rel,
                            local_name=row.local_name,
                            env=env,
                            publisher=dest.publisher,
                            store_address=row.store_address,
                            manifest_value=row.manifest_value,
                            store_value=row.value,
                        )
                    )
                else:
                    missing.append(
                        MissingRow(
                            manifest_path=rel,
                            local_name=row.local_name,
                            env=env,
                            publisher=dest.publisher,
                            store_address=row.store_address,
                        )
                    )

    return missing, differs, store_only, reach_errors


# ---------- rendering ----------


def _render_human(result: ValidateResult, *, verbose: bool) -> str:
    lines: list[str] = []
    lines.append(
        f"project: {result.project_name} (manifests dir: {result.manifests_dir}/)  "
        f"target: {result.target}  envs: {' '.join(result.envs) or '(none)'}"
    )
    lines.append("")

    # schema
    if result.schema_errors:
        n_errs = sum(len(v) for v in result.schema_errors.values())
        lines.append(f"schema       {n_errs} error(s) across {len(result.schema_errors)} file(s)")
        for _fp, errs in sorted(result.schema_errors.items()):
            for e in errs:
                lines.append(f"  {e}")
    else:
        lines.append(f"schema       ok — {result.files_checked} manifest(s) checked")
    lines.append("")

    # freshness
    if result.stale:
        lines.append(f"freshness    {len(result.stale)} stale")
        for s in result.stale:
            if s.age_days is None:
                lines.append(
                    f"  {s.manifest_path} :: group {s.group_name!r}: never rotated "
                    f"(max_age_days={s.max_age_days})"
                )
            else:
                lines.append(
                    f"  {s.manifest_path} :: group {s.group_name!r}: last_rotated {s.last_rotated} "
                    f"({s.age_days} days old, max_age_days={s.max_age_days})"
                )
    else:
        lines.append("freshness    ok")
    lines.append("")

    # publication — one line per issue, sorted by (manifest, secret, env).
    def _sort_key(row: Any) -> tuple[str, str, str]:
        return (row.manifest_path, row.local_name or "", row.env)

    if result.publication_errors or result.missing or result.differs or result.store_only:
        if result.publication_errors:
            lines.append("publication  reach errors:")
            for err in result.publication_errors:
                lines.append(f"  {err}")
        if result.missing:
            lines.append(f"publication  {len(result.missing)} missing")
            for m in sorted(result.missing, key=_sort_key):
                lines.append(
                    f"  {m.manifest_path}  secrets[{m.local_name!r}]  env={m.env}  "
                    f"publisher={m.publisher}  {m.store_address}"
                )
        if result.differs:
            lines.append(f"publication  {len(result.differs)} differs")
            for d in sorted(result.differs, key=_sort_key):
                base = (
                    f"  {d.manifest_path}  secrets[{d.local_name!r}]  env={d.env}  "
                    f"publisher={d.publisher}  {d.store_address}"
                )
                if verbose:
                    base += f"  manifest={d.manifest_value!r} store={d.store_value!r}"
                lines.append(base)
        if result.store_only:
            lines.append(
                f"publication  {len(result.store_only)} store_only (declared by no manifest)"
            )
            for s in sorted(result.store_only, key=lambda r: (r.publisher, r.env, r.store_address)):
                lines.append(f"  env={s.env}  publisher={s.publisher}  {s.store_address}")
    else:
        lines.append("publication  ok")
    lines.append("")

    if result.valid:
        lines.append(f"valid: {result.files_checked} manifest(s) checked")
    else:
        n_issues = (
            sum(len(v) for v in result.schema_errors.values())
            + len(result.stale)
            + len(result.missing)
            + len(result.differs)
            + len(result.store_only)
            + len(result.publication_errors)
        )
        lines.append(f"invalid: {n_issues} issue(s)")

    return "\n".join(lines) + "\n"


def _render_json(result: ValidateResult) -> str:
    payload = {
        "project": result.project_name,
        "target": result.target,
        "envs": list(result.envs),
        "valid": result.valid,
        "files_checked": result.files_checked,
        "schema": {
            "ok": not result.schema_errors,
            "errors": [
                {"manifest": fp, "errors": errs}
                for fp, errs in sorted(result.schema_errors.items())
            ],
        },
        "freshness": {
            "ok": not result.stale,
            "stale": [
                {
                    "manifest": s.manifest_path,
                    "group": s.group_name,
                    "max_age_days": s.max_age_days,
                    "age_days": s.age_days,
                    "last_rotated": (
                        s.last_rotated.isoformat()
                        if isinstance(s.last_rotated, dt.date)
                        else s.last_rotated
                    ),
                }
                for s in result.stale
            ],
        },
        "publication": {
            "ok": (
                not result.missing
                and not result.differs
                and not result.store_only
                and not result.publication_errors
            ),
            "missing": [
                {
                    "manifest": m.manifest_path,
                    "local_name": m.local_name,
                    "env": m.env,
                    "publisher": m.publisher,
                    "store_address": m.store_address,
                }
                for m in result.missing
            ],
            "differs": [
                {
                    "manifest": d.manifest_path,
                    "local_name": d.local_name,
                    "env": d.env,
                    "publisher": d.publisher,
                    "store_address": d.store_address,
                    "manifest_value": d.manifest_value,
                    "store_value": d.store_value,
                }
                for d in result.differs
            ],
            "store_only": [
                {"env": s.env, "publisher": s.publisher, "store_address": s.store_address}
                for s in result.store_only
            ],
            "reach_errors": list(result.publication_errors),
        },
    }
    return json.dumps(payload, indent=2, default=str)


# ---------- orchestration ----------


def run(args: argparse.Namespace) -> int:
    cfg = load_config(resolve_config_path(args.config))
    project = load_project(cfg.project_dir)
    target_name: str = args.target
    target_cfg = cfg.target(target_name)
    envs = cfg.select_envs(target_name, list(getattr(args, "envs", None) or []))
    target = targets_mod.load(project.dir, cfg.directories.targets, target_name)

    files = manifest_yaml_files(project, cfg, target_cfg.manifests)
    unlisted = {f.stem for f in manifest_yaml_files(project, cfg)} - set(target_cfg.manifests)

    result = ValidateResult(
        project_name=project.name,
        project_dir=project.dir,
        manifests_dir=cfg.directories.manifests,
        files_checked=len(files),
        target=target_name,
        envs=envs,
    )

    # Constructing a destination opens nothing (clients are lazy), so the
    # schema gate can use the target's instances for section checks.
    loaded = build_destinations(cfg, project, target)
    try:
        by_name: dict[str, Publisher] = {}
        for ld in loaded:
            if ld.publisher is not None:
                by_name.setdefault(ld.destination.publisher, ld.publisher)

        # `accounts/` was retired (F-56 D2): a login is its manifest's
        # `login:` block. Report a leftover directory before the
        # per-manifest checks.
        if (project.dir / "accounts").is_dir():
            result.schema_errors["accounts/"] = [
                "accounts/: the accounts directory is retired; move each login into "
                f"its manifest's `login:` block ({cfg.directories.manifests}/<slug>.yaml) "
                "and delete accounts/"
            ]

        # A file below a subdirectory has no slug, so no target can list
        # it: name each one before the per-manifest checks.
        nested = nested_manifest_files(project, cfg)
        if nested:
            result.schema_errors[f"{cfg.directories.manifests}/"] = [
                f"{fp.relative_to(project.dir).as_posix()}: manifests sit directly under "
                f"{cfg.directories.manifests}/ (the slug is the file stem); move it to "
                f"{cfg.directories.manifests}/<slug>.yaml"
                for fp in nested
            ]

        parsed_per_file: dict[Path, dict] = {}
        declared: dict[str, set[str] | None] = {}
        for fp in files:
            rel = fp.relative_to(project.dir).as_posix()
            errors, parsed = _validate_file(fp, project, cfg=cfg, publishers=by_name)
            if errors:
                result.schema_errors[rel] = errors
            declared[slug_for(fp, project, cfg)] = (
                None if parsed is None else declared_secrets(parsed)
            )
            if parsed is not None and not errors:
                parsed_per_file[fp] = parsed
        # A manifest no target lists (a login-only one) is still read by
        # `km login`, `km mfa` and `login.from`: its `login:` and `notes:`
        # are checked whichever target is validated.
        manifests_dir = (project.dir / cfg.directories.manifests).resolve()
        for fp in manifest_yaml_files(project, cfg):
            if fp.stem not in unlisted:
                continue
            rel = fp.relative_to(project.dir).as_posix()
            try:
                data = load_manifest(fp)
            except yaml.YAMLError as e:
                result.schema_errors[rel] = [f"{rel}: YAML parse error: {e}"]
                continue
            except (FileNotFoundError, ValueError) as e:
                result.schema_errors[rel] = [f"{rel}: {e}"]
                continue
            if data is None:
                result.schema_errors[rel] = [f"{rel}: top-level must be a mapping"]
                continue
            errors = [f"{rel}: {e}" for e in login_errors(data, manifests_dir)]
            if errors:
                result.schema_errors[rel] = errors
        target_errors = undeclared_target_values(target, declared, unlisted)
        if target_errors:
            result.schema_errors[target.label] = target_errors

        schema_only = bool(getattr(args, "schema_only", False))
        if not schema_only:
            for fp, parsed in parsed_per_file.items():
                result.stale.extend(_check_freshness(parsed, fp, project.dir))

            missing, differs, store_only, reach = _check_publication(
                parsed_per_file, project, cfg, loaded, target, envs
            )
            result.missing.extend(missing)
            result.differs.extend(differs)
            result.store_only.extend(store_only)
            result.publication_errors.extend(reach)
    finally:
        close_all(loaded)

    as_json = bool(getattr(args, "json", False))
    verbose = bool(getattr(args, "verbose", False))

    if as_json:
        sys.stdout.write(_render_json(result) + "\n")
    else:
        sys.stdout.write(_render_human(result, verbose=verbose))

    if result.publication_errors:
        return 2
    return 0 if result.valid else 1
