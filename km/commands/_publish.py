"""`km publish <target> [<env>...]` — write the manifests' secrets to a target.

A **target** in ``config.yaml`` lists the manifests it publishes (by
slug) and an ordered list of destinations (each a publisher, its
configuration and the envs it accepts); an **environment** is one of the
config's ``environments:``. With no envs given, every env the target
accepts is published.

The run, in order:

1. Load every manifest the target lists and overlay its per-target values
   (``targets/<target>.yaml``, F-54 Decision Q1). A manifest that fails
   to parse, or a target value no manifest declares, stops the run
   before anything is written.
2. Resolve the selection: exactly one of ``--all`` (every secret of
   every manifest; writing everything is always said out loud) or
   ``--only <slug>[:<name>]``, repeatable (F-54 Decision S2, resolved by
   :mod:`km.selection`). An unknown slug or name, or a slug the target
   does not list, is a usage error.
3. Check for empty secrets among the selected secrets of every manifest
   the target publishes. A generated secret (``passphrase``, ``uuid``,
   ``base64_32``, ``ed25519`` producer) is minted into its manifest; any
   other empty secret (``static``, ``interactive``, a vendor producer,
   no group) fails the publish naming manifest and secret, before any
   write: only a human or the vendor can produce it. Overlaid names are
   never minted.
4. Per destination, in the target's order, per env:
   ``ensure`` → ``publish`` (every selected manifest, the selected
   names) → ``apply``, the last only when the publish wrote something
   to that destination for that env and no write there failed. Each
   ensure holds the target's lock (:func:`km.targets.ensuring`), so two
   runs on one target never both create one resource.

Dry run by default: nothing is minted into a manifest or written to a
store, each publisher lists what it would write, and each apply that
would run is listed. ``--execute`` writes. Publishers write
unconditionally (Decision S1).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
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
    slug_for,
    target_secrets,
    undeclared_target_values,
)
from km.config import LoadedConfig, resolve_config_path
from km.config import load as load_config
from km.loader import load_producer
from km.manifest_io import load_manifest, write_secrets
from km.producers.contract import ManifestEntry, ProduceContext, ProduceResult, Secret
from km.project import Project, load_project
from km.publishers._discovery import (
    LoadedDestination,
    build_destinations,
    close_all,
    destination_envs,
)
from km.selection import (
    Selection,
    SelectionError,
    group_secret_names,
    rotation_groups,
)
from km.selection import resolve as resolve_selection

# Producers that mint a value from nothing; an empty secret they own is
# minted on publish. Everything else needs a human or a vendor.
GENERATED_PRODUCERS = frozenset({"passphrase", "uuid", "base64_32", "ed25519"})


@dataclass
class _Manifest:
    path: Path
    rel: str
    slug: str
    parsed: dict
    secrets: dict[str, str]
    overlaid: set[str] = field(default_factory=set)


@dataclass
class _Mint:
    manifest: _Manifest
    group: str
    gdata: dict
    names: list[str]


def _norm(producer: Any) -> str:
    return producer.replace("-", "_") if isinstance(producer, str) else ""


def _group_of(parsed: Mapping[str, Any]) -> dict[str, tuple[str, dict]]:
    """secret name → (group name, group block)."""
    out: dict[str, tuple[str, dict]] = {}
    for gname, gdata in rotation_groups(parsed).items():
        for name in group_secret_names(gdata):
            out[name] = (gname, gdata)
    return out


def _load_manifests(
    project: Project,
    cfg: LoadedConfig,
    target: targets_mod.TargetValues,
    slugs: tuple[str, ...],
) -> tuple[list[_Manifest], list[str]]:
    """Load the manifests ``slugs`` names (the target's list)."""
    out: list[_Manifest] = []
    errors: list[str] = []
    for path in manifest_yaml_files(project, cfg, slugs):
        rel = path.relative_to(project.dir).as_posix()
        try:
            parsed = load_manifest(path)
        except yaml.YAMLError as e:
            errors.append(f"{rel}: YAML parse error: {e}")
            continue
        except (FileNotFoundError, ValueError) as e:
            errors.append(f"{rel}: {e}")
            continue
        if parsed is None:
            continue
        if parsed.get("secrets") is not None and not isinstance(parsed.get("secrets"), dict):
            errors.append(f"{rel}: `secrets:` must be a mapping")
            continue
        slug = slug_for(path, project, cfg)
        secrets, overlaid = target_secrets(parsed, slug, target)
        out.append(
            _Manifest(
                path=path, rel=rel, slug=slug, parsed=parsed, secrets=secrets, overlaid=overlaid
            )
        )
    return out, errors


def _published_by_target(m: _Manifest, publisher_names: set[str]) -> bool:
    block = m.parsed.get("publishers")
    if not isinstance(block, dict):
        return False
    return any(name in publisher_names and section is not None for name, section in block.items())


def _plan_mints(
    manifests: list[_Manifest],
    publisher_names: set[str],
    target_label: str,
    selection: Selection | None = None,
    provided: set[tuple[str, str]] | frozenset = frozenset(),
) -> tuple[list[_Mint], list[str]]:
    """Find the empty selected secrets of every manifest the target publishes.

    ``provided`` names the per-target values a destination's ensure
    records (an Infisical project id or token): empty until it has run,
    neither minted nor an error.
    """
    mints: list[_Mint] = []
    errors: list[str] = []
    for m in manifests:
        if not _published_by_target(m, publisher_names):
            continue
        if selection is not None and m.slug not in selection:
            continue
        owners = _group_of(m.parsed)
        by_group: dict[str, _Mint] = {}
        for name, value in m.secrets.items():
            if value != "":
                continue
            if selection is not None and name not in selection[m.slug]:
                continue
            if (m.slug, name) in provided:
                continue
            owner = owners.get(name)
            producer = _norm(owner[1].get("producer")) if owner else ""
            if name in m.overlaid or producer not in GENERATED_PRODUCERS:
                if name in m.overlaid:
                    why = f"is empty in the target's values file ({target_label})"
                elif owner is None:
                    why = "is empty and in no rotation group"
                else:
                    why = f"is empty and its producer {owner[1].get('producer')!r} cannot mint it"
                errors.append(
                    f"{m.rel}: secret {name!r} {why}; only a human or the vendor can produce it"
                )
                continue
            gname, gdata = owner
            by_group.setdefault(gname, _Mint(manifest=m, group=gname, gdata=gdata, names=[]))
            by_group[gname].names.append(name)
        mints.extend(by_group.values())
    return mints, errors


def _mint(mint: _Mint, project: Project, cfg: LoadedConfig, *, dry_run: bool) -> str | None:
    """Mint ``mint.names`` and (unless dry run) write them into the manifest.

    Returns an error string, or None on success. ``last_rotated`` is set
    to today only when the group had none: minting the missing half of a
    group is not a rotation of the whole group.
    """
    m = mint.manifest
    entries = [
        ManifestEntry(
            name=s["name"] if isinstance(s, dict) else s,
            format=s.get("format") if isinstance(s, dict) else None,
            group_name=mint.group,
            manifest_path=m.path,
        )
        for s in mint.gdata.get("secrets") or []
        if (s.get("name") if isinstance(s, dict) else s) in mint.names
    ]
    try:
        producer = load_producer(mint.gdata["producer"], project, cfg)
        raw = producer.produce(
            entries,
            ProduceContext(manifest=m.parsed, current_secrets=dict(m.secrets), group=mint.gdata),
        )
    except Exception as err:  # noqa: BLE001
        return f"{m.rel}: group {mint.group!r}: minting failed: {err}"
    result = raw if isinstance(raw, ProduceResult) else ProduceResult(secrets=tuple(raw))
    values = {s.name: s.value for s in result.secrets if isinstance(s, Secret)}
    if set(values) != set(mint.names):
        return (
            f"{m.rel}: group {mint.group!r}: producer minted {sorted(values)}, "
            f"expected {sorted(mint.names)}"
        )
    names = ", ".join(sorted(values))
    if dry_run:
        sys.stderr.write(f"  [dry-run] would mint {m.rel} group={mint.group!r}: {names}\n")
    else:
        last = mint.gdata.get("last_rotated")
        try:
            write_secrets(
                m.path,
                mint.group,
                values,
                last_rotated=last if last is not None else dt.date.today().isoformat(),
            )
        except Exception as err:  # noqa: BLE001
            return f"{m.rel}: group {mint.group!r}: failed to write minted values: {err}"
        sys.stderr.write(f"  minted {m.rel} group={mint.group!r}: {names}\n")
    m.secrets.update(values)
    return None


def _overlay_recorded(
    values: tuple[tuple[str, str], ...],
    by_slug: Mapping[str, _Manifest],
    target: targets_mod.TargetValues,
) -> dict[Path, set[str]]:
    """Lay the per-target values an ensure just recorded over the run's
    manifests; return manifest path → the names that changed."""
    out: dict[Path, set[str]] = {}
    for slug, name in values:
        m = by_slug.get(slug)
        if m is None:
            continue
        m.secrets[name] = (target.manifests.get(slug) or {}).get(name, "")
        m.overlaid.add(name)
        out.setdefault(m.path, set()).add(name)
    return out


def run(args: argparse.Namespace) -> int:
    cfg = load_config(resolve_config_path(args.config))
    project = load_project(cfg.project_dir)
    target_name: str = args.target
    target_cfg = cfg.target(target_name)
    destinations = target_cfg.destinations
    envs = cfg.select_envs(target_name, list(args.envs or []))
    target = targets_mod.load(project.dir, cfg.directories.targets, target_name)
    dry_run = not args.execute

    manifests, errors = _load_manifests(project, cfg, target, target_cfg.manifests)
    unlisted = {f.stem for f in manifest_yaml_files(project, cfg)} - set(target_cfg.manifests)
    if not errors:
        errors = undeclared_target_values(
            target, {m.slug: declared_secrets(m.parsed) for m in manifests}, unlisted
        )
    if errors:
        for e in errors:
            sys.stderr.write(f"error: {e}\n")
        sys.stderr.write("\nnothing written: fix the files above first\n")
        return 1

    selection: Selection | None = None
    if not getattr(args, "all", False):
        try:
            selection = resolve_selection(
                args.only or [],
                {m.slug: (m.parsed, m.secrets) for m in manifests},
                target=target_name,
                unlisted=unlisted,
            )
        except SelectionError as err:
            sys.stderr.write(f"error: {err}\n")
            return 2
        manifests = [m for m in manifests if m.slug in selection]

    loaded = build_destinations(cfg, project, target)
    try:
        load_errors = [
            f"target {target_name!r} destination {ld.destination.label}: {ld.error}"
            for ld in loaded
            if ld.publisher is None
        ]
        if load_errors:
            for e in load_errors:
                sys.stderr.write(f"error: {e}\n")
            return 2

        publisher_names = {d.publisher for d in destinations}
        provided = {p for ld in loaded if ld.publisher for p in ld.publisher.provides()}
        mints, errors = _plan_mints(manifests, publisher_names, target.label, selection, provided)
        if errors:
            for e in errors:
                sys.stderr.write(f"error: {e}\n")
            sys.stderr.write("\nnothing written: empty secrets km cannot mint\n")
            return 1
        if mints:
            sys.stderr.write("=== minting empty generated secrets ===\n")
        for mint in mints:
            err = _mint(mint, project, cfg, dry_run=dry_run)
            if err:
                sys.stderr.write(f"error: {err}\n")
                return 1

        totals = {"written": 0, "skipped": 0, "failed": 0, "created": 0}
        triples = [(m.path, m.parsed, m.secrets) for m in manifests]
        selected_names: dict[Path, set[str] | None] = {
            m.path: (None if selection is None else selection[m.slug]) for m in manifests
        }
        by_slug = {m.slug: m for m in manifests}
        # (destination position, names) for each set of per-target values an
        # ensure recorded: the destinations before it published without them.
        recorded: list[tuple[int, dict[Path, set[str]]]] = []

        def drive(
            ld: LoadedDestination, names_for: Mapping[Path, set[str] | None], *, ensure: bool
        ) -> None:
            dest, pub = ld.destination, ld.publisher
            assert pub is not None
            refs = [r for r in manifest_refs(dest.publisher, triples) if r.path in names_for]
            dest_envs: list[str] = []
            for ref in refs:
                for e in destination_envs(pub, dest, envs, ref.section):
                    if e not in dest_envs:
                        dest_envs.append(e)
            dest_envs.sort(key=envs.index)
            for env in dest_envs:
                env_refs = [r for r in refs if env in destination_envs(pub, dest, envs, r.section)]
                sys.stderr.write(
                    f"=== {target_name} [{dest.label}] env={env} "
                    f"({len(env_refs)} manifest(s)){'' if ensure else ' (new target values)'} ===\n"
                )
                if ensure:
                    # One ensure at a time per target, on values re-read under
                    # the lock: a concurrent run's creations are seen, not redone.
                    lock = (
                        contextlib.nullcontext(set()) if dry_run else targets_mod.ensuring(target)
                    )
                    try:
                        with lock as reread:
                            ensured = pub.ensure(env=env, manifests=env_refs, dry_run=dry_run)
                    except Exception as err:  # noqa: BLE001
                        sys.stderr.write(f"  ERROR ensure failed: {err}; skipping publish\n")
                        totals["failed"] += 1
                        continue
                    totals["created"] += len(ensured.created)
                    new_values = tuple(dict.fromkeys((*ensured.values, *sorted(reread))))
                    if new_values:
                        recorded.append(
                            (dest.index, _overlay_recorded(new_values, by_slug, target))
                        )
                    if ensured.failed:
                        totals["failed"] += ensured.failed
                        sys.stderr.write("  ensure incomplete; skipping publish\n")
                        continue
                written = 0
                failed = 0
                for ref in env_refs:
                    try:
                        result = pub.publish(
                            manifest_path=ref.path,
                            env=env,
                            secrets=ref.secrets,
                            section=ref.section,
                            dry_run=dry_run,
                            names=names_for[ref.path],
                        )
                    except Exception as err:  # noqa: BLE001
                        rel = ref.path.relative_to(project.dir).as_posix()
                        sys.stderr.write(f"  ERROR publish failed for {rel}: {err!r}\n")
                        failed += 1
                        continue
                    written += result.written
                    failed += result.failed
                    totals["written"] += result.written
                    totals["skipped"] += result.skipped
                totals["failed"] += failed
                # Apply only where the selection wrote (Decision S1), and never
                # over a partial publish: it would activate a half-written env.
                if written == 0 or not pub.applies():
                    continue
                if failed:
                    sys.stderr.write(
                        f"  apply skipped [{dest.label}] env={env}: {failed} write(s) failed\n"
                    )
                    continue
                sys.stderr.write(
                    f"  {'[dry-run] would apply' if dry_run else 'apply'} "
                    f"[{dest.label}] env={env}\n"
                )
                try:
                    pub.apply(env=env, dry_run=dry_run)
                except Exception as err:  # noqa: BLE001
                    sys.stderr.write(f"  ERROR apply failed: {err}\n")
                    totals["failed"] += 1

        for ld in loaded:
            drive(ld, selected_names, ensure=True)
        # A destination's ensure may create per-target values (an Infisical
        # project id, the services' token) that an earlier destination
        # renders: publish just those names again where they were used.
        for position, names_for in recorded:
            for ld in loaded:
                if ld.destination.index < position:
                    drive(ld, names_for, ensure=False)
    finally:
        close_all(loaded)

    counts = (
        f"written: {totals['written']}, created: {totals['created']}, "
        f"skipped: {totals['skipped']}, failed: {totals['failed']}"
    )
    if dry_run:
        # Publishers count "would write" entries under ``written`` in a dry run.
        sys.stderr.write(f"\n[dry-run] {counts} (pass --execute to write)\n")
    else:
        sys.stderr.write(f"\n{counts}\n")
    return 0 if totals["failed"] == 0 else 1


__all__ = ["GENERATED_PRODUCERS", "run"]
