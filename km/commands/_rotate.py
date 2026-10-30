"""`km rotate` implementation.

Every rotation-bearing manifest has the same shape: a top-level
``secrets:`` block, a per-publisher ``publishers:`` block, and a
``rotation:`` block whose groups each declare a ``producer:``. The
rotate command dispatches by the group's ``producer:``.

- ``km rotate`` (no slug): walks every manifest
  (``<directories.manifests>/*.yaml``), invokes each due group's
  producer, writes minted values back into
  the manifest's ``secrets:`` block. ``interactive`` and ``static``
  groups are silently skipped in batch mode (the former is
  operator-driven, the latter is a no-op anyway). Rotation changes the
  manifest only and calls no publisher (F-54 Decision Q2) — run
  ``km publish <target>`` afterwards to write the fresh values.
- ``km rotate <slug>``: same dispatch against the manifest
  ``<directories.manifests>/<slug>.yaml`` (an unknown slug is a usage
  error listing the slugs). An ``interactive`` group runs the login
  menu + paste-back flow.
  Login-only manifests with no ``rotation:`` block exit 2 with a clear
  message — they're login material, not rotation participants.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from km.commands._manifests import manifest_yaml_files, resolve_slug
from km.config import load as load_config
from km.config import resolve_config_path
from km.loader import load_producer
from km.manifest_io import effective_login, load_manifest, write_secrets
from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    ProducerContractError,
    ProduceResult,
    Secret,
)
from km.project import Project, load_project
from km.selection import rotation_groups

# Producers that should be skipped when rotating in batch mode (no
# explicit path). Operator-driven producers and no-ops have nothing to
# do during a bulk sweep.
_BATCH_SKIP_PRODUCERS = frozenset({"interactive", "static"})


def last_rotated_date(last: Any) -> dt.date | None:
    """Return the rotation date for a ``last_rotated`` value, or None.

    Accepts a date, an ISO date string, or None.
    """
    if last is None:
        return None
    if isinstance(last, dt.date):
        return last
    try:
        return dt.date.fromisoformat(str(last))
    except ValueError:
        return None


def _due_groups(parsed: dict, *, force: str | None = None) -> Iterator[tuple[str, dict]]:
    today = dt.date.today()
    for gname, gdata in rotation_groups(parsed).items():
        if force is not None:
            if gname == force:
                yield (gname, gdata)
            continue
        max_age = gdata.get("max_age_days") or 0
        if max_age == 0:
            continue
        last_date = last_rotated_date(gdata.get("last_rotated"))
        if last_date is None:
            yield (gname, gdata)
            continue
        if (today - last_date).days >= max_age:
            yield (gname, gdata)


def _build_entries(gdata: dict, *, gname: str, manifest_path: Path) -> list[ManifestEntry]:
    out: list[ManifestEntry] = []
    for s in gdata.get("secrets") or []:
        if isinstance(s, dict) and isinstance(s.get("name"), str):
            out.append(
                ManifestEntry(
                    name=s["name"],
                    format=s.get("format"),
                    group_name=gname,
                    manifest_path=manifest_path,
                )
            )
        elif isinstance(s, str):
            out.append(
                ManifestEntry(
                    name=s,
                    format=None,
                    group_name=gname,
                    manifest_path=manifest_path,
                )
            )
    return out


def _validate_name(
    name: str,
    allowed: set[str],
    *,
    manifest_path: Path,
    gname: str,
) -> bool:
    """Validate that ``name`` is in the producer's allowed-names set.

    The set may contain literal names and glob patterns (entries
    containing ``*``); names matching either are accepted.
    """
    if name in allowed:
        return True
    import fnmatch

    for pat in allowed:
        if "*" in pat and fnmatch.fnmatchcase(name, pat):
            return True
    sys.stderr.write(
        f"error: {manifest_path}: group {gname!r}: "
        f"producer returned name {name!r} not declared in group's secrets list "
        "or producer output_names()\n"
    )
    return False


def _collect_results(
    secrets: list[Secret],
    allowed_names: set[str],
    *,
    manifest_path: Path,
    gname: str,
) -> dict[str, str] | None:
    """Validate a producer's returned Secrets against the group's contract."""
    name_to_value: dict[str, str] = {}
    for r in secrets:
        if not isinstance(r, Secret):
            sys.stderr.write(
                f"error: {manifest_path}: group {gname!r}: "
                f"producer returned a non-Secret result: {r!r}\n"
            )
            return None
        if not _validate_name(r.name, allowed_names, manifest_path=manifest_path, gname=gname):
            return None
        name_to_value[r.name] = r.value
    return name_to_value


def _rotate_one_manifest(
    manifest_path: Path,
    *,
    cfg,
    project: Project,
    revoke_old: bool,
    dry_run: bool,
    force: str | None,
    skip_producers: frozenset[str] = frozenset(),
) -> tuple[int, int]:
    """Run every due group in one manifest. Returns (rotated, failed) counts."""
    parsed = load_manifest(manifest_path)
    if parsed is None:
        sys.stderr.write(f"error: {manifest_path}: top-level must be a mapping\n")
        return (0, 1)
    try:
        login = effective_login(parsed, (project.dir / cfg.directories.manifests).resolve())
    except ValueError as err:
        sys.stderr.write(f"error: {manifest_path}: {err}\n")
        return (0, 1)

    rotated_n = 0
    failed_n = 0
    secrets_block = parsed.get("secrets") or {}
    if not isinstance(secrets_block, dict):
        secrets_block = {}

    for gname, gdata in _due_groups(parsed, force=force):
        producer_name = gdata.get("producer")
        if not isinstance(producer_name, str) or not producer_name:
            sys.stderr.write(
                f"error: {manifest_path}: group {gname!r}: "
                "no `producer:` set — required for rotation\n"
            )
            failed_n += 1
            continue
        if producer_name in skip_producers:
            continue
        try:
            producer = load_producer(producer_name, project, cfg)
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(f"error: {manifest_path}: group {gname!r}: {err}\n")
            failed_n += 1
            continue

        entries = _build_entries(gdata, gname=gname, manifest_path=manifest_path)
        current_secrets = {n: v for n, v in secrets_block.items() if isinstance(n, str)}

        ctx = ProduceContext(
            manifest=parsed,
            current_secrets=current_secrets,
            group=gdata,
            revoke_old=revoke_old,
            login=login,
        )

        # Producers self-declare their authoritative name set via
        # output_names(); we union it with declared entries so producers
        # that don't override output_names still work.
        try:
            declared_output_names = producer.output_names(entries, ctx)
        except ProducerContractError as err:
            sys.stderr.write(
                f"error: {manifest_path}: group {gname!r}: contract violation: {err}\n"
            )
            failed_n += 1
            continue
        allowed_names = {e.name for e in entries} | set(declared_output_names)

        sys.stdout.write(
            f"rotating {manifest_path.name} group={gname!r} "
            f"producer={producer_name!r}{' [dry-run]' if dry_run else ''}\n"
        )

        if dry_run:
            invalid = [
                n
                for n in declared_output_names
                if not _validate_name(n, allowed_names, manifest_path=manifest_path, gname=gname)
            ]
            if invalid:
                failed_n += 1
                continue
            sys.stdout.write(
                f"  [dry-run] would update secrets: {', '.join(sorted(declared_output_names))}\n"
            )
            rotated_n += 1
            continue

        try:
            raw_result = producer.produce(entries, ctx)
        except ProducerContractError as err:
            sys.stderr.write(
                f"error: {manifest_path}: group {gname!r}: contract violation: {err}\n"
            )
            failed_n += 1
            continue
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(f"error: {manifest_path}: group {gname!r}: {err}\n")
            failed_n += 1
            continue

        result = _normalize_produce_result(raw_result)
        if result is None:
            sys.stderr.write(
                f"error: {manifest_path}: group {gname!r}: producer returned "
                f"unsupported result type: {type(raw_result).__name__}\n"
            )
            failed_n += 1
            continue

        name_to_value = _collect_results(
            list(result.secrets),
            allowed_names,
            manifest_path=manifest_path,
            gname=gname,
        )
        if name_to_value is None:
            failed_n += 1
            continue

        last_rotated_kw: dict[str, Any] = {}
        if result.last_rotated is not None:
            last_rotated_kw["last_rotated"] = result.last_rotated

        try:
            write_secrets(
                manifest_path,
                gname,
                name_to_value,
                remove=result.remove,
                **last_rotated_kw,
            )
        except Exception as err:  # noqa: BLE001
            sys.stderr.write(
                f"error: {manifest_path}: group {gname!r}: failed to write manifest: {err}\n"
            )
            failed_n += 1
            continue
        secrets_block = {
            n: v
            for n, v in {**secrets_block, **name_to_value}.items()
            if n not in set(result.remove)
        }
        msg_parts = [f"updated {len(name_to_value)} secret(s): {', '.join(sorted(name_to_value))}"]
        if result.remove:
            msg_parts.append(f"removed {len(result.remove)}: {', '.join(sorted(result.remove))}")
        sys.stdout.write("  " + "; ".join(msg_parts) + "\n")

        rotated_n += 1

    return (rotated_n, failed_n)


def _normalize_produce_result(raw: Any) -> ProduceResult | None:
    if isinstance(raw, ProduceResult):
        return raw
    if isinstance(raw, list):
        return ProduceResult(secrets=tuple(raw))
    return None


def _run_single(args: argparse.Namespace) -> int:
    cfg = load_config(resolve_config_path(args.config))
    project = load_project(cfg.project_dir)
    manifest_path = resolve_slug(project, cfg, args.slug)

    parsed = load_manifest(manifest_path)
    if parsed is None:
        sys.stderr.write(f"error: {manifest_path}: top-level must be a mapping\n")
        return 2
    if parsed.get("rotation") is None:
        sys.stderr.write(
            f"error: {manifest_path}: login-only manifest, "
            "no rotation block — did you mean to edit it?\n"
        )
        return 2

    if args.force is not None:
        if args.force not in rotation_groups(parsed):
            sys.stderr.write(
                f"error: {manifest_path}: --force value {args.force!r} matches no rotation group\n"
            )
            return 1

    rotated, failed = _rotate_one_manifest(
        manifest_path,
        cfg=cfg,
        project=project,
        revoke_old=args.revoke_old,
        dry_run=args.dry_run,
        force=args.force,
    )
    if rotated == 0 and failed == 0 and args.force is None:
        sys.stdout.write(
            f"{manifest_path.name}: no rotation groups are due — "
            "skipping (re-run with `--force <group>` to rotate anyway)\n"
        )
    sys.stdout.write(f"\nrotated: {rotated}, failed: {failed}\n")
    if rotated and not args.dry_run:
        sys.stdout.write(
            "hint: run `km publish <target> [<env>...] --all --execute` "
            "to write the new values to each target\n"
        )
    return 1 if failed else 0


def _run_batch(args: argparse.Namespace) -> int:
    cfg = load_config(resolve_config_path(args.config))
    project = load_project(cfg.project_dir)
    files = manifest_yaml_files(project, cfg)

    total_rotated = 0
    total_failed = 0
    for f in files:
        try:
            parsed = load_manifest(f)
        except yaml.YAMLError as e:
            sys.stderr.write(f"error: {f}: yaml parse error: {e}\n")
            total_failed += 1
            continue
        except (FileNotFoundError, ValueError) as e:
            sys.stderr.write(f"error: {f}: {e}\n")
            total_failed += 1
            continue
        if parsed is None:
            continue
        if parsed.get("rotation") is None:
            continue
        skip = _BATCH_SKIP_PRODUCERS if args.force is None else frozenset()
        rotated, failed = _rotate_one_manifest(
            f,
            cfg=cfg,
            project=project,
            revoke_old=args.revoke_old,
            dry_run=args.dry_run,
            force=args.force,
            skip_producers=skip,
        )
        total_rotated += rotated
        total_failed += failed

    sys.stdout.write(f"\nbatch rotated: {total_rotated}, failed: {total_failed}\n")
    if total_rotated and not args.dry_run:
        sys.stdout.write(
            "hint: run `km publish <target> [<env>...] --all --execute` "
            "to write the new values to each target\n"
        )
    return 1 if total_failed else 0


def run(args: argparse.Namespace) -> int:
    if getattr(args, "slug", None):
        return _run_single(args)
    return _run_batch(args)
