"""Publisher contract — one seam every backing store implements.

`km publish` and `km validate` drive every store through this interface
so the commands themselves stay store-agnostic. Concrete
implementations live as modules under :mod:`km.publishers` (built-ins)
and under each project's publishers/ directory (project plugins); the
loader picks one by name at dispatch time.

Constructors receive one destination's configuration (the keys of a
``targets.<target>`` entry other than ``publisher`` and ``envs``) and
are responsible for demarshalling what they need (host, paths, etc.)
into typed instance state. Per-target state (km's own token for the
instance, a project id) is not configuration: the loader sets
:attr:`Publisher.target_state` from ``targets/<target>.yaml``.

The contract, in the order ``km publish`` drives it per destination:

- :meth:`Publisher.ensure`   — create the destination's structure if
  missing (folders, roles, output dirs); idempotent; default no-op.
- :meth:`Publisher.publish`  — write the selected secret values out,
  unconditionally (F-54 Decision S1: no compare-before-write).
- :meth:`Publisher.apply`    — restart or reload what reads the
  destination; default no-op. Called only when publish wrote something
  to the destination for that env.

and, for the other commands:

- :meth:`Publisher.validate` — schema-check the per-manifest section.
- :meth:`Publisher.collect`  — read the publisher's runtime state and
  classify against the manifests; used by `km validate` to detect
  missing, differing and store-only secrets.
- :meth:`Publisher.unpublish` — remove named secrets from the store
  (kept for a later prune verb; nothing calls it today; default no-op).

Manifests carry the per-publisher section under
``publishers.<name>:`` (plural ``publishers``). Each section's shape
is opaque to km — the publisher interprets it. Most publishers use an
env-keyed mapping (``dev:``, ``prod:``, …); ``template_file`` uses a
list of per-spec entries instead. The base class's
:meth:`Publisher.declared_envs` covers the env-keyed-mapping case;
publishers with other shapes (template_file) override it.
Underscore-prefixed keys in env-keyed mappings are reserved for
publisher-defined meta and are skipped by the default env iteration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CollectedEntry:
    """One row returned by :meth:`Publisher.collect`.

    Internal classification used by ``km validate`` to derive the
    user-facing categories. The four states are:
    ``same``         — manifest and store agree on value.
    ``differs``      — both have it, values diverge.
    ``manifest_only``— manifest declares it, store missing it.
    ``store_only``   — store has it, no manifest claims it (a failure:
                       nothing is created in a store by hand).
    """

    state: str
    manifest_path: Path | None
    local_name: str | None
    store_address: str
    value: str
    manifest_value: str | None = None  # set only for `differs` (paired with `value`)


@dataclass(frozen=True)
class CollectResult:
    entries: list[CollectedEntry]


@dataclass(frozen=True)
class PublishResult:
    written: int
    skipped: int
    failed: int


@dataclass(frozen=True)
class EnsureResult:
    """What :meth:`Publisher.ensure` created (or, in a dry run, would create).

    ``created`` holds human-readable labels (``folder /jwt/account``);
    ``failed`` counts structure that could not be created. ``values``
    names the per-target values (``(manifest slug, secret)``) ensure
    recorded in ``targets/<target>.yaml``, so the command can overlay
    them and re-publish the destinations that ran before this one.
    """

    created: tuple[str, ...] = ()
    failed: int = 0
    values: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ManifestRef:
    """Per-manifest payload handed to :meth:`Publisher.collect`.

    ``section`` is opaque to km — typically a Mapping for env-keyed
    publishers (infisical, postgresql) and a list for template_file.
    """

    path: Path
    secrets: Mapping[str, str]
    section: Any


class Publisher(ABC):
    """Abstract base for every publisher.

    Implementations are expected to be cheap to construct — heavier
    resources (HTTP clients, file handles) should be lazily opened on
    first use and torn down via ``close()``. km holds publisher
    instances for the duration of one command run.

    The loader sets :attr:`templates_root` after construction — the
    project-relative ``directories.templates`` path resolved to an
    absolute :class:`Path`. A publisher that renders templates
    (``template_file``) anchors relative template paths there; where it
    writes is its destination's configuration (F-56 D4), never the
    project.

    The loader also sets :attr:`target_name` and :attr:`target_state`
    (this publisher's ``_publishers.<name>`` mapping from
    ``targets/<target>.yaml``, ``{}`` when absent), and
    :attr:`target_values`, the run's shared :class:`km.targets.TargetValues`,
    which a publisher whose ``ensure`` creates per-instance credentials
    records into with :func:`km.targets.update`.

    :attr:`keeps_target_state` is ``True`` for a publisher whose
    ``_publishers.<name>`` state names one instance (``infisical``): a
    target may list it once, since a second destination would share the
    first one's credentials.

    :attr:`env_partitioned` is ``False`` for stores with no notion of an
    environment: the commands then drive the publisher
    once per destination, with the first selected env.
    """

    name: str  # subclass declares this as a class attribute
    env_partitioned: bool = True
    keeps_target_state: bool = False
    templates_root: Path | None = None  # set by km.loader.load_publisher
    target_name: str | None = None  # set by km.loader.load_publisher
    target_state: Mapping[str, Any] = {}  # set by km.loader.load_publisher
    target_file: Path | None = None  # set by km.loader.load_publisher
    target_values: Any = None  # km.targets.TargetValues; set by km.loader.load_publisher

    def __init__(self, config: Mapping[str, Any]) -> None:
        # Subclasses that need typed config override this and demarshal.
        return

    @classmethod
    def schema_instance(cls) -> Publisher:
        """An instance good enough for :meth:`validate`, built from no
        destination. Raises :class:`ValueError` (or :class:`TypeError`)
        when the class cannot check a section without one. Default:
        construct from ``{}``."""
        return cls({})

    def bind_config(self, cfg: Any) -> None:
        """Called by the loader after construction with the project's
        :class:`km.config.LoadedConfig`, for a publisher whose checks
        depend on the other destinations (``yaml_file``'s file keys).
        Default: nothing."""
        return

    def close(self) -> None:
        return

    def __enter__(self) -> Publisher:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @abstractmethod
    def validate(
        self,
        *,
        manifest_path: Path,
        section: Any,
        secrets: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Schema-check the per-manifest section.

        Returns a list of human-readable error strings; empty list means
        the section is well-formed.
        """

    @abstractmethod
    def publish(
        self,
        *,
        manifest_path: Path,
        env: str,
        secrets: Mapping[str, str],
        section: Any,
        dry_run: bool,
        names: Collection[str] | None = None,
    ) -> PublishResult:
        """Write secret values out for one ``(manifest, env)`` pair.

        ``names`` is the selection (``km publish --only``): write only the
        addresses of those secrets, or, for a whole-file store, every file
        that uses one of them. ``None`` (``--all``) writes everything.
        ``secrets`` always holds the whole manifest, so a file can be
        rendered in full. ``written`` counts what was (or, in a dry run,
        would be) written: the command applies only where it is non-zero.
        """

    def ensure(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
        dry_run: bool,
    ) -> EnsureResult:
        """Create whatever ``manifests`` need in this destination for
        ``env`` and is missing (idempotent). Runs before :meth:`publish`.

        In a dry run, report what would be created without creating it.
        Default: nothing to do.
        """
        return EnsureResult()

    def provides(self) -> set[tuple[str, str]]:
        """The per-target values (``(manifest slug, secret)``) this
        destination's :meth:`ensure` records. ``km publish`` does not
        fail on them being empty before ensure has run. Default: none."""
        return set()

    def apply(self, *, env: str, dry_run: bool) -> None:
        """Restart or reload what reads this destination for ``env``.
        Runs after :meth:`publish`. Default: nothing to do."""
        return

    def applies(self) -> bool:
        """Whether this destination has an :meth:`apply` to run. Default:
        the class overrides :meth:`apply`; a publisher whose apply is
        configuration (``template_file``'s ``apply:``) overrides this."""
        return type(self).apply is not Publisher.apply

    @abstractmethod
    def collect(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
    ) -> CollectResult:
        """Read every key from the store for one env; classify against
        ``manifests`` and return four-state rows.
        """

    def unpublish(
        self,
        *,
        manifest_path: Path,
        section: Any,
        names: list[str],
        envs: list[str] | None = None,
        dry_run: bool = False,
    ) -> PublishResult:
        """Remove the named secrets from the store, across every env in
        ``section`` they're declared in (or just ``envs`` if supplied).

        Default: no-op. Publishers that own a mutable per-key store
        (Infisical) override; whole-file publishers (template_file)
        inherit the no-op since "removing one var" isn't a meaningful
        operation for a re-rendered file. Nothing calls it today (F-54
        Decision Q2); it stays for a later prune verb.
        """
        return PublishResult(written=0, skipped=0, failed=0)

    def declared_envs(self, section: Any) -> set[str]:
        """Return the set of envs this publisher's section declares.

        Within the envs a destination accepts, the manifest says which
        of them a secret goes to: orchestration uses this to enumerate
        the per-destination per-env work units (publish, validate).

        Default: top-level keys whose values are mappings, skipping
        underscore-prefixed meta keys. Publishers whose section is not
        an env-keyed mapping (e.g. template_file's list of specs)
        override.
        """
        out: set[str] = set()
        if not isinstance(section, Mapping):
            return out
        for key, val in section.items():
            if not isinstance(key, str) or not key or key.startswith("_"):
                continue
            if isinstance(val, Mapping):
                out.add(key)
        return out


__all__ = [
    "CollectedEntry",
    "CollectResult",
    "EnsureResult",
    "ManifestRef",
    "Publisher",
    "PublishResult",
]
