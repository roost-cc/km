"""Producer contract.

Every producer subclasses :class:`Producer`. The constructor receives
the producer's slice of the project config (``cfg.producers[<name>]``,
or ``{}`` if absent); the subclass demarshals what it needs into typed
instance state. Concrete producers implement at minimum
:meth:`Producer.produce`; :meth:`Producer.output_names` has a sensible
default and only needs overriding when the producer emits synthetic
names not present in the input entries (e.g. ed25519's paired public
key).

    output_names(entries, ctx) -> list[str]
        Names this producer would write for ``entries``, without minting
        anything or touching external systems. Used by orchestration to
        preview rotations before deciding to actually run them.

    produce(entries, ctx) -> ProduceResult | list[Secret]
        Mint fresh value(s) for ``entries``. May call external systems
        (e.g. AWS, Twilio, an operator at the keyboard). Returns either
        a :class:`ProduceResult` (full result with optional removals
        and a producer-supplied ``last_rotated`` value) or, for the
        common case of "just these new values", a plain list of
        :class:`Secret` instances which the orchestration will wrap.

Each call corresponds to one rotation group: ``entries`` is the group's
list of declared secret entries.

The :class:`ProduceContext` parameter carries per-call state — the
parsed manifest yaml, the rotation group's own block (so the producer
can read its own ``last_rotated`` history, ``max_age_days``, etc.),
the manifest's full ``secrets:`` block (so a producer like ``twilio``
can read a sibling Account SID stored as a separate ``static`` group),
and the operational ``revoke_old`` flag.

``current_secrets`` carries the **entire manifest's** ``secrets:`` block,
not just the group being rotated. Vendors often split immutable
identifiers into a sibling ``static`` group — the rotating producer
still needs to read those values for authentication.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ManifestEntry:
    """One secret a rotation group owns.

    ``format`` is interpreted by the producer. Some producers ignore it
    entirely; others use it to dispatch (e.g. ``aws_iam`` reads
    ``aws-access-key-id`` vs ``aws-secret-access-key``); some accept a
    mapping for typed configuration (``passphrase`` reads
    ``{length, alphabet}`` from a mapping when provided).
    """

    name: str
    format: Any
    group_name: str
    manifest_path: Path


@dataclass(frozen=True)
class Secret:
    """A produced secret: a name plus its freshly-minted value."""

    name: str
    value: str


@dataclass(frozen=True)
class ProduceContext:
    """Per-call state for a produce() / output_names() invocation."""

    manifest: Mapping[str, Any]
    current_secrets: Mapping[str, str]
    group: Mapping[str, Any] = field(default_factory=dict)
    revoke_old: bool = False
    # The manifest's effective login (km.manifest_io.effective_login), or
    # None when it has no ``login:``.
    login: Mapping[str, Any] | None = None


_UNSET = object()


@dataclass(frozen=True)
class ProduceResult:
    """The result of one produce() call.

    - ``secrets``: values to write into the manifest's ``secrets:``
      block (added if absent, replaced if already present).
    - ``remove``: names to delete from the manifest's ``secrets:`` block
      and from any publisher store that mirrors them. Most producers
      leave this empty; producers that maintain a bounded set of
      historical secrets emit removals when entries roll out.
    - ``last_rotated``: producer-supplied value to write into the
      rotation group's ``last_rotated`` field. ``None`` means "use the
      orchestration default" (today's date, scalar form). Producers
      that maintain a list-shape ``last_rotated`` history return the
      full updated list here.
    """

    secrets: tuple[Secret, ...] = ()
    remove: tuple[str, ...] = ()
    last_rotated: Any = None


class ProducerContractError(Exception):
    """Raised when a producer is asked to do something it cannot honor."""


class Producer(ABC):
    """Abstract base for every producer.

    Constructors receive the producer's config block (the value of
    ``cfg.producers[<name>]``, or ``{}`` if no block was declared) and
    are responsible for demarshalling what they need. Producers without
    config rely on the default no-op ``__init__``.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        # Subclasses that need typed config override this and demarshal.
        return

    def output_names(self, entries: list[ManifestEntry], ctx: ProduceContext) -> list[str]:
        """Names this producer would emit for ``entries``.

        Default: one name per entry. Override when the producer derives
        synthetic names (e.g. ed25519 emits a paired public-key entry
        alongside each declared private key).

        Pure: must not call external systems or have side effects.
        """
        return [e.name for e in entries]

    @abstractmethod
    def produce(
        self, entries: list[ManifestEntry], ctx: ProduceContext
    ) -> ProduceResult | list[Secret]:
        """Mint fresh values for ``entries``."""
