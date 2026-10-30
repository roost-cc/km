"""Dynamic loader for producers and publishers.

A name (yaml: ``producer: ed25519`` or ``publisher: infisical``) maps
to a python file ``<name>.py`` (hyphens become underscores). Lookup
order:

    1. project-local: <project_dir>/<directories.{producers,publishers}>/<name>.py
    2. km built-in:   <km install>/{producers,publishers}/<name>.py
    3. error: "no <kind> '<name>' found (looked in ... and ...)"

Each module must define exactly one concrete subclass of the requested
base class (:class:`km.producers.contract.Producer` or
:class:`km.publishers.Publisher`). A publisher is instantiated with one
destination's configuration (a ``targets.<target>`` entry); a producer
with its ``producer:`` entry from the targets, or ``{}``. The
constructor is responsible for demarshalling its config.

Resolved (name, base) pairs are cached for the duration of a process.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from km.config import Destination, LoadedConfig
from km.producers.contract import Producer
from km.project import Project
from km.publishers import Publisher
from km.targets import TargetValues

_KM_DIR = Path(__file__).resolve().parent

# Cache: (base_class_name, resolved name) -> class
_class_cache: dict[tuple[str, str], type] = {}


def _module_stem(name: str) -> str:
    return name.replace("-", "_")


def _find_subclass[T: (Producer, Publisher)](module, base: type[T]) -> type[T]:
    candidates: list[type] = []
    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name)
        if isinstance(obj, type) and obj is not base and issubclass(obj, base):
            candidates.append(obj)
    if not candidates:
        raise RuntimeError(f"{module.__name__}: no concrete {base.__name__} subclass found")
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise RuntimeError(
            f"{module.__name__}: multiple {base.__name__} subclasses ({names}); "
            "each module must define exactly one"
        )
    return candidates[0]


def _resolve_class[T: (Producer, Publisher)](
    name: str,
    *,
    base: type[T],
    project_subdir: str,
    project_dir: Path,
    builtin_subdir: str,
) -> type[T]:
    cache_key = (base.__name__, name)
    if cache_key in _class_cache:
        return _class_cache[cache_key]

    stem = _module_stem(name)
    project_path = project_dir / project_subdir / f"{stem}.py"
    builtin_path = _KM_DIR / builtin_subdir / f"{stem}.py"

    if project_path.is_file():
        module_name = f"_km_project.{builtin_subdir}.{stem}"
        spec = importlib.util.spec_from_file_location(module_name, project_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to load {project_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    elif builtin_path.is_file():
        module = importlib.import_module(f"km.{builtin_subdir}.{stem}")
    else:
        raise RuntimeError(
            f"no {base.__name__.lower()} {name!r} found "
            f"(looked in {project_path} and {builtin_path})"
        )

    cls = _find_subclass(module, base)
    _class_cache[cache_key] = cls
    return cls


def producer_class(name: str, project: Project, cfg: LoadedConfig) -> type[Producer]:
    """Resolve a producer class by name without constructing it."""
    return _resolve_class(
        name,
        base=Producer,
        project_subdir=cfg.directories.producers,
        project_dir=project.dir,
        builtin_subdir="producers",
    )


def publisher_class(name: str, project: Project, cfg: LoadedConfig) -> type[Publisher]:
    """Resolve a publisher class by name without constructing it."""
    return _resolve_class(
        name,
        base=Publisher,
        project_subdir=cfg.directories.publishers,
        project_dir=project.dir,
        builtin_subdir="publishers",
    )


def load_producer(name: str, project: Project, cfg: LoadedConfig) -> Producer:
    cls = producer_class(name, project, cfg)
    return cls(cfg.producers.get(name, {}))


def load_publisher(
    destination: Destination,
    project: Project,
    cfg: LoadedConfig,
    target: TargetValues | None = None,
) -> Publisher:
    """Construct the publisher for one destination of a target.

    The destination's configuration goes to the constructor; the
    target's per-instance state (``targets/<target>.yaml``
    ``_publishers.<name>``) is set afterwards, with the project roots.
    """
    cls = publisher_class(destination.publisher, project, cfg)
    inst = cls(dict(destination.config))
    _set_roots(inst, project, cfg)
    if target is not None:
        inst.target_name = target.name
        inst.target_state = target.publisher_state(destination.publisher)
        inst.target_file = target.path
        inst.target_values = target
    return inst


def schema_publisher(name: str, project: Project, cfg: LoadedConfig) -> Publisher | None:
    """A publisher instance good enough for :meth:`Publisher.validate`.

    Schema checks are destination-independent, so the instance is built
    from no configuration. Returns ``None`` when the class needs
    configuration to construct (the name still resolved); raises when
    the name does not resolve.
    """
    cls = publisher_class(name, project, cfg)
    try:
        inst = cls.schema_instance()
    except (ValueError, TypeError):
        return None
    _set_roots(inst, project, cfg)
    return inst


def _set_roots(inst: Publisher, project: Project, cfg: LoadedConfig) -> None:
    # The project's template root, for publishers that render templates
    # (template_file). Set after construction so it doesn't show up as an
    # unexpected key in the destination's configuration. Where a
    # publisher writes is its destination's configuration (F-56 D4).
    inst.templates_root = (project.dir / cfg.directories.templates).resolve()
    inst.bind_config(cfg)


__all__ = [
    "load_producer",
    "load_publisher",
    "producer_class",
    "publisher_class",
    "schema_publisher",
]
