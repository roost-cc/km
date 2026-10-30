"""Per-run destination publishers built on top of :mod:`km.loader`.

``km publish`` and ``km validate`` materialize one Publisher per
destination of the named target, in the target's order, reuse each
instance (and therefore its per-env client cache) across every manifest
in a run, and call ``close()`` on each at the end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from km.config import Destination, LoadedConfig
from km.loader import load_publisher
from km.project import Project
from km.publishers import Publisher
from km.targets import TargetValues


@dataclass
class LoadedDestination:
    destination: Destination
    publisher: Publisher | None  # None when construction failed
    error: str | None = None


def build_destinations(
    cfg: LoadedConfig,
    project: Project,
    target: TargetValues,
) -> list[LoadedDestination]:
    """Construct a publisher per destination of ``target``, in order.

    A destination whose publisher fails to load is returned with
    ``publisher=None`` and the error, so callers can report it and
    carry on (validate) or stop before any write (publish). A second
    destination of a publisher that keeps per-target state is such an
    error: both would read and write the one ``_publishers.<name>``.
    """
    out: list[LoadedDestination] = []
    stateful: dict[str, str] = {}  # publisher name → first destination label
    for dest in cfg.target(target.name).destinations:
        try:
            pub = load_publisher(dest, project, cfg, target)
        except Exception as err:  # noqa: BLE001
            out.append(LoadedDestination(destination=dest, publisher=None, error=str(err)))
            continue
        if pub.keeps_target_state:
            first = stateful.setdefault(dest.publisher, dest.label)
            if first != dest.label:
                pub.close()
                out.append(
                    LoadedDestination(
                        destination=dest,
                        publisher=None,
                        error=(
                            f"{dest.publisher} is already destination {first}; its "
                            f"{target.label} state names one instance, so a target may "
                            "list it once"
                        ),
                    )
                )
                continue
        out.append(LoadedDestination(destination=dest, publisher=pub))
    return out


def close_all(loaded: list[LoadedDestination]) -> None:
    for ld in loaded:
        if ld.publisher is None:
            continue
        try:
            ld.publisher.close()
        except Exception:  # noqa: BLE001
            pass


def destination_envs(
    publisher: Publisher,
    destination: Destination,
    selected: list[str],
    section: Any,
) -> list[str]:
    """The envs to drive ``publisher`` with for one manifest section.

    The selected envs the destination accepts, narrowed to those the
    manifest declares for the publisher. A publisher with no notion of
    an environment runs once, with the first accepted env.
    """
    accepted = [e for e in selected if e in destination.envs]
    if not publisher.env_partitioned:
        return accepted[:1]
    declared = publisher.declared_envs(section)
    return [e for e in accepted if e in declared]


BuildDestinations = Callable[[LoadedConfig, Project, TargetValues], list[LoadedDestination]]

__all__ = [
    "BuildDestinations",
    "LoadedDestination",
    "build_destinations",
    "close_all",
    "destination_envs",
]
