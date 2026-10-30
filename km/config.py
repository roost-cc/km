"""Project config loading + path resolution for `km`.

Each project carries its own ``config.yaml`` at the project root. The
file holds:

- ``directories:`` — project-relative paths to ``manifests``,
  ``producers``, ``publishers``, ``runbooks``, ``templates``, ``targets``
  (any omitted key inherits the conventional name). ``accounts`` and
  ``files`` are retired and fail the load.
- ``environments:`` — the application's deployment environments
  (``dev``, ``test``, …), declared once.
- ``targets:`` — named targets, each a mapping
  ``{manifests: [<slug>…], destinations: [...]}`` (F-56 D3). ``manifests``
  lists the slugs (file stems directly under ``directories.manifests``)
  the target publishes and validates; each must name a manifest.
  ``destinations`` run in the order listed. Each names a ``publisher:``,
  the ``envs:`` it accepts (a subset of ``environments:``) and carries the
  publisher's whole configuration as its remaining keys; nothing is
  inherited from anywhere else.

  A ``destinations`` entry may instead name a ``producer:``; it carries
  that producer's configuration (e.g. ``aws_iam``'s ``aws_profile``) and
  is not a destination. Producers read the first such entry across all
  targets.

The old top-level ``publishers:`` and ``producers:`` sections are a load
error: their contents moved onto destinations.

String values may reference environment variables via ``$VAR`` or
``${VAR}``; an undefined variable is a hard error. Each target expands on
its own: a target that references an unset variable fails only when a
command names it, so ``local`` still runs without the variables another
target needs (F-55 D1).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from km._envexpand import expand as _expand_env_vars
from km._envexpand import undefined as _undefined_env_vars
from km.manifest import TEMPLATE_FILENAMES

CONFIG_FILENAME = "config.yaml"

DEFAULT_DIRECTORIES: dict[str, str] = {
    "manifests": "manifests",
    "producers": "producers",
    "publishers": "publishers",
    "runbooks": "runbooks",
    "templates": "templates",
    "targets": "targets",
}

# ``directories:`` keys that no longer exist, with what replaced them.
RETIRED_DIRECTORIES: dict[str, str] = {
    "accounts": "a login is its manifest's `login:` block (F-56 D2)",
    "files": (
        "rendered files leave the project; each `template_file` destination names "
        "its `root:` (F-56 D4)"
    ),
}

# Keys of a destination entry that km reads; every other key is the
# publisher's configuration.
_DESTINATION_KEYS = frozenset({"publisher", "envs"})

_TARGET_KEYS = frozenset({"manifests", "destinations"})
_TARGET_SHAPE = "`{manifests: [<slug>…], destinations: [...]}`"


def find_config(start: Path | None = None) -> Path:
    """Walk up from ``start`` to find ``config.yaml``.

    ``start`` defaults to :func:`km.location.default_start` (``-C/--dir``, then
    ``$KM_PROJECT_DIR``, then the cwd).

    Errors out via :class:`SystemExit` if the walk reaches the filesystem
    root without finding one.
    """
    from km.location import default_start

    cur = (start or default_start()).resolve()
    origin = cur
    while True:
        candidate = cur / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    sys.stderr.write(f"no {CONFIG_FILENAME} found walking up from {origin}\n")
    raise SystemExit(2)


def resolve_config_path(explicit: str | None, *hints: Path) -> str:
    """Pick the right config.yaml for this command run.

    Resolution order:
    1. ``--config`` (whatever the user passed) — used as-is.
    2. Walk up from the first ``hints`` directory (a path given on the command line).
    3. Walk up from :func:`km.location.default_start` (``-C/--dir``, then
       ``$KM_PROJECT_DIR``, then the cwd).
    """
    if explicit:
        return explicit
    for h in hints:
        if h is not None:
            return str(find_config(h))
    return str(find_config())


@dataclass
class Directories:
    """Project-relative subdirectory names (resolved against the project root)."""

    manifests: str = "manifests"
    producers: str = "producers"
    publishers: str = "publishers"
    runbooks: str = "runbooks"
    templates: str = "templates"
    targets: str = "targets"


@dataclass(frozen=True)
class Destination:
    """One entry of a target: a publisher, its envs and its configuration."""

    publisher: str
    envs: tuple[str, ...]
    config: dict[str, Any]
    index: int = 0  # position in the target's list (execution order)

    @property
    def label(self) -> str:
        return f"{self.index}:{self.publisher}"


@dataclass(frozen=True)
class Target:
    """One entry of ``targets:``: the manifests it publishes, and where to."""

    name: str
    manifests: tuple[str, ...]  # slugs, in the order listed
    destinations: list[Destination]


@dataclass
class LoadedConfig:
    """A project's ``config.yaml`` resolved into a typed structure."""

    path: Path  # absolute path to the config file
    project_dir: Path  # the directory containing config.yaml
    directories: Directories
    environments: list[str] = field(default_factory=list)
    targets: dict[str, Target] = field(default_factory=dict)
    # Producer configuration from ``producer:`` entries of the targets.
    producers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Targets that reference unset variables: name → the variables. Not
    # parsed, and an error only when a command names them.
    unresolved: dict[str, list[str]] = field(default_factory=dict)

    def target(self, name: str) -> Target:
        """Return target ``name``; usage error if undeclared or unresolved."""
        if name in self.unresolved:
            names = ", ".join(f"${n}" for n in self.unresolved[name])
            sys.stderr.write(
                f"config {self.path}: target {name!r}: undefined env var(s): {names}\n"
            )
            raise SystemExit(2)
        if name not in self.targets:
            declared = ", ".join(sorted([*self.targets, *self.unresolved])) or "(none)"
            sys.stderr.write(
                f"error: unknown target {name!r}; {self.path.name} declares: {declared}\n"
            )
            raise SystemExit(2)
        return self.targets[name]

    def select_envs(self, target: str, envs: list[str] | None) -> list[str]:
        """Resolve the env arguments for ``target``.

        Empty/None means every env the target's destinations accept, in
        ``environments:`` order. An env not in ``environments:`` is a usage
        error naming the declared ones.
        """
        destinations = self.target(target).destinations
        accepted = {e for d in destinations for e in d.envs}
        if not envs:
            return [e for e in self.environments if e in accepted]
        unknown = [e for e in envs if e not in self.environments]
        if unknown:
            declared = ", ".join(self.environments) or "(none)"
            sys.stderr.write(
                f"error: unknown environment(s) {', '.join(unknown)}; "
                f"{self.path.name} declares: {declared}\n"
            )
            raise SystemExit(2)
        not_accepted = [e for e in envs if e not in accepted]
        if not_accepted:
            sys.stderr.write(
                f"error: target {target!r} accepts no destination for "
                f"environment(s) {', '.join(not_accepted)} "
                f"(accepted: {', '.join(e for e in self.environments if e in accepted)})\n"
            )
            raise SystemExit(2)
        out: list[str] = []
        for e in envs:
            if e not in out:
                out.append(e)
        return out


def _fail(path: Path, msg: str) -> None:
    sys.stderr.write(f"config {path}: {msg}\n")
    raise SystemExit(2)


def load(config_path: str | Path) -> LoadedConfig:
    path = Path(config_path).resolve()
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        sys.stderr.write(f"Failed to read config from {path}: {e}\n")
        raise SystemExit(2) from e
    except yaml.YAMLError as e:
        sys.stderr.write(f"Failed to parse config {path}: {e}\n")
        raise SystemExit(2) from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _fail(path, "top-level must be a mapping")

    retired = [k for k in ("publishers", "producers") if k in raw]
    if retired:
        _fail(
            path,
            f"`{'`, `'.join(retired)}:` is no longer accepted; declare "
            "`environments:` and `targets:` instead (each destination under "
            "`targets.<name>` names its `publisher:` and `envs:` and carries the "
            "publisher's whole configuration; producer configuration is a "
            "`producer:` entry of a target)",
        )

    raw = _expand_env_vars(raw, "config", skip_keys=frozenset({"targets"}))
    project_dir = path.parent

    raw_targets, unresolved = _expand_targets(raw.get("targets"))
    directories = _parse_directories(raw.get("directories"), path)
    environments = _parse_environments(raw.get("environments"), path)
    targets, producers = _parse_targets(
        raw_targets,
        environments,
        path,
        project_dir,
        _manifest_slugs(project_dir / directories.manifests),
    )

    return LoadedConfig(
        path=path,
        project_dir=project_dir,
        directories=directories,
        environments=environments,
        targets=targets,
        producers=producers,
        unresolved=unresolved,
    )


def _expand_targets(raw: Any) -> tuple[Any, dict[str, list[str]]]:
    """Expand each target's ``$VAR`` references on its own.

    A target that references an unset variable is left out of the returned
    mapping and listed, with its variables, in the second value.
    """
    if not isinstance(raw, dict):
        return raw, {}
    out: dict[Any, Any] = {}
    unresolved: dict[str, list[str]] = {}
    for tname, body in raw.items():
        names = _undefined_env_vars(body)
        if names:
            unresolved[tname] = names
        else:
            out[tname] = _expand_env_vars(body, f"config.targets.{tname}")
    return out, unresolved


def _parse_directories(raw: Any, path: Path) -> Directories:
    if raw is None:
        return Directories()
    if not isinstance(raw, dict):
        sys.stderr.write("config.directories must be a mapping\n")
        raise SystemExit(2)
    for key, why in RETIRED_DIRECTORIES.items():
        if key in raw:
            _fail(path, f"`directories.{key}` is retired: {why}; remove it")
    out = Directories()
    for key in DEFAULT_DIRECTORIES:
        val = raw.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or not val:
            sys.stderr.write(f"config.directories.{key} must be a non-empty string\n")
            raise SystemExit(2)
        setattr(out, key, val)
    return out


def _parse_environments(raw: Any, path: Path) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(e, str) and e for e in raw):
        _fail(path, "`environments:` must be a list of non-empty strings")
    if len(set(raw)) != len(raw):
        _fail(path, "`environments:` lists an environment twice")
    return list(raw)


def _manifest_slugs(manifests_dir: Path) -> set[str]:
    """The slugs of the manifests directly under ``manifests_dir``."""
    if not manifests_dir.is_dir():
        return set()
    return {
        f.stem
        for f in manifests_dir.glob("*.yaml")
        if f.is_file() and f.name not in TEMPLATE_FILENAMES
    }


def _parse_targets(
    raw: Any,
    environments: list[str],
    path: Path,
    project_dir: Path,
    slugs: set[str],
) -> tuple[dict[str, Target], dict[str, dict[str, Any]]]:
    if raw is None:
        return {}, {}
    if not isinstance(raw, dict):
        _fail(path, f"`targets:` must be a mapping of target name → {_TARGET_SHAPE}")
    if raw and not environments:
        _fail(path, "`targets:` needs `environments:` (the envs destinations may accept)")
    targets: dict[str, Target] = {}
    producers: dict[str, dict[str, Any]] = {}
    for tname, body in raw.items():
        if not isinstance(tname, str) or not tname:
            _fail(path, f"target name must be a non-empty string (got {tname!r})")
        if isinstance(body, list):
            _fail(
                path,
                f"targets.{tname} is a list; a target is now a mapping {_TARGET_SHAPE}: "
                "list the manifest slugs it publishes under `manifests:` and move its "
                "entries under `destinations:`",
            )
        if not isinstance(body, dict):
            _fail(path, f"targets.{tname} must be a mapping {_TARGET_SHAPE}")
        unknown_keys = sorted(str(k) for k in body if k not in _TARGET_KEYS)
        if unknown_keys:
            _fail(
                path,
                f"targets.{tname}: unknown key(s) {', '.join(unknown_keys)} "
                f"(a target is {_TARGET_SHAPE})",
            )
        manifests = body.get("manifests")
        if (
            not isinstance(manifests, list)
            or not manifests
            or not all(isinstance(m, str) and m for m in manifests)
        ):
            _fail(path, f"targets.{tname}.manifests is required (non-empty list of slugs)")
        if len(set(manifests)) != len(manifests):
            _fail(path, f"targets.{tname}.manifests lists a slug twice")
        unknown = [m for m in manifests if m not in slugs]
        if unknown:
            _fail(
                path,
                f"targets.{tname}.manifests: {', '.join(unknown)} names no manifest "
                f"(have: {', '.join(sorted(slugs)) or '(none)'})",
            )
        entries = body.get("destinations")
        if not isinstance(entries, list) or not entries:
            _fail(path, f"targets.{tname}.destinations must be a non-empty list")
        destinations: list[Destination] = []
        for i, entry in enumerate(entries):
            where = f"targets.{tname}.destinations[{i}]"
            if not isinstance(entry, dict):
                _fail(path, f"{where} must be a mapping")
            if "producer" in entry:
                pname = entry.get("producer")
                if "publisher" in entry or not isinstance(pname, str) or not pname:
                    _fail(path, f"{where}: a `producer:` entry names one producer and no publisher")
                if pname not in producers:
                    rest = {k: v for k, v in entry.items() if k != "producer"}
                    producers[pname] = _resolve_relative_paths(rest, project_dir)
                continue
            pub = entry.get("publisher")
            if not isinstance(pub, str) or not pub:
                _fail(path, f"{where}.publisher is required (non-empty string)")
            envs = entry.get("envs")
            if not isinstance(envs, list) or not envs or not all(isinstance(e, str) for e in envs):
                _fail(path, f"{where}.envs is required (non-empty list of environments)")
            unknown = [e for e in envs if e not in environments]
            if unknown:
                _fail(
                    path,
                    f"{where}.envs: {', '.join(unknown)} not in `environments:` "
                    f"({', '.join(environments)})",
                )
            config = _resolve_relative_paths(
                {k: v for k, v in entry.items() if k not in _DESTINATION_KEYS}, project_dir
            )
            if pub == "template_file":
                # Where rendered files land is checked at load (F-56 D4).
                from km.publishers.template_file import check_config

                try:
                    check_config(config)
                except ValueError as e:
                    _fail(path, f"{where}: {e}")
            destinations.append(
                Destination(
                    publisher=pub,
                    envs=tuple(envs),
                    config=config,
                    index=len(destinations),
                )
            )
        targets[tname] = Target(name=tname, manifests=tuple(manifests), destinations=destinations)
    return targets, producers


def _resolve_relative_paths(entry: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Resolve ``./`` and ``../`` string values against the config dir,
    in lists and nested mappings too (``yaml_file``'s ``files:``)."""

    def resolve(v: Any) -> Any:
        if isinstance(v, str) and (v.startswith("./") or v.startswith("../")):
            return str((config_dir / v).resolve())
        if isinstance(v, list):
            return [resolve(el) for el in v]
        if isinstance(v, dict):
            return {k: resolve(el) for k, el in v.items()}
        return v

    return {k: resolve(v) for k, v in entry.items()}
