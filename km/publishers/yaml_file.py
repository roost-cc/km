"""YamlFilePublisher — set secrets at JSONPath-selected nodes of yaml files.

A service that reads its secrets from yaml (Synapse's homeserver config
and appservice registration) keeps them among
much else. This publisher sets each secret at the one node an RFC 9535
JSONPath query selects, keeping the rest of the file (comments and
quoting included), and its optional ``apply`` runs once per env after
the writes so the reader picks them up. It replaced the ``synapse``
publisher, whose two file names were fixed (F-56 D5).

Destination configuration (``{env}`` in a path is the env; a path is
absolute after ``$VAR`` expansion, or ``./``-relative to the config
dir)::

    - publisher: yaml_file
      envs: [dev]
      files:
        homeserver: $HOME/secrets/synapse/homeserver.{env}.yaml
        appservice: $HOME/secrets/synapse/app-bot.{env}.yaml
      apply: [docker, compose, -f, $HOME/stack/docker-compose.yml, restart, synapse]

Manifest section shape (env-keyed; each address is ``<key>:<query>``,
split at the first ``:``, ``<key>`` one of the destination's ``files``
and ``<query>`` a JSONPath query starting at ``$``)::

    publishers:
      yaml_file:
        dev:
          db_password: homeserver:$.database.args.password
          as_token: appservice:$.as_token

A query must select exactly one node, a scalar that already exists: the
publisher edits a file, it does not write one, and a filter that starts
matching a second node is an error rather than a guess. Two addresses
selecting one node are an error too.

An env's update is all or nothing. ``publish`` (once per manifest)
checks each address against the file, in a dry run too, and stages it;
a failure discards the env's staged updates and fails every later
publish to that env. ``apply``, which ``km publish`` runs only after
every manifest's publish to the env succeeded, writes the staged
updates: holding a lock on each file's directory, it re-reads every
file, sets every node and replaces the files, putting back the ones
already replaced if a later one fails. Then it runs the ``apply``
command, if any. ``collect``
reads each node back: equal → ``same``, different → ``differs``, file
absent or no node → ``manifest_only``. There is no ``store_only``: the
files hold far more than secrets.

A destination handles the addresses whose ``<key>`` its ``files`` name.
An address whose key another target's ``yaml_file`` destination names
belongs to that destination and is skipped here; a key that no
``yaml_file`` destination of any target names is an error.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path
from typing import Any

import jsonpath_rfc9535 as jsonpath
from ruamel.yaml import YAML

from km.publishers import (
    CollectedEntry,
    CollectResult,
    ManifestRef,
    Publisher,
    PublishResult,
)

_CONFIG_KEYS = ("files", "apply")
_APPLY_TIMEOUT_S = 300


def _guess_indent(text: str) -> tuple[int, int, int]:
    """The file's ``(mapping, sequence, offset)`` indentation, so a
    round trip re-emits it as written (ruamel's default is 2, 2, 0)."""
    mapping: int | None = None
    offset: int | None = None
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for prev, cur in zip(lines, lines[1:], strict=False):
        body = prev.lstrip()
        if body.startswith("- ") or not body.split(" #")[0].rstrip().endswith(":"):
            continue
        p, c = len(prev) - len(body), len(cur) - len(cur.lstrip())
        if c <= p:
            continue
        if cur.lstrip().startswith("- "):
            offset = c - p if offset is None else offset
        elif mapping is None:
            mapping = c - p
    off = offset or 0
    return mapping or 2, off + 2, off


def _round_trip(text: str) -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap a long line
    mapping, sequence, offset = _guess_indent(text)
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    return yaml


def _load(path: Path) -> tuple[Any, YAML]:
    text = path.read_text(encoding="utf-8")
    yaml = _round_trip(text)
    return yaml.load(text), yaml


def _dump(yaml: YAML, data: Any, stream: Any) -> None:
    yaml.dump(data, stream)


class AddressError(ValueError):
    """An address that does not select exactly one scalar node."""


def _parse_address(address: Any) -> tuple[str, str] | None:
    """``homeserver:$.database.args.password`` → ``("homeserver", "$.database.args.password")``."""
    if not isinstance(address, str) or ":" not in address:
        return None
    key, _, query = address.partition(":")
    if not key or not query.startswith("$"):
        return None
    return key, query


def _compile(query: str) -> jsonpath.JSONPathQuery:
    return jsonpath.compile(query)


def _select(data: Any, query: str) -> list[jsonpath.JSONPathNode]:
    return list(_compile(query).find(data))


def _one_scalar(nodes: list[jsonpath.JSONPathNode], what: str) -> jsonpath.JSONPathNode:
    """The one node of ``nodes``; :class:`AddressError` naming ``what``
    unless there is exactly one and it is a scalar."""
    if len(nodes) != 1:
        raise AddressError(f"{what} selects {len(nodes)} nodes (exactly one must match)")
    node = nodes[0]
    if isinstance(node.value, (Mapping, list)):
        raise AddressError(
            f"{what} selects a {type(node.value).__name__.lower()}, not a scalar ({node.path()})"
        )
    return node


def _set(data: Any, location: tuple[Any, ...], value: str) -> None:
    """Set the node at ``location`` (keys and indices from the document
    root) in the round-trip document."""
    parent = data
    for step in location[:-1]:
        parent = parent[step]
    parent[location[-1]] = value


def _tmp_beside(path: Path, text: str) -> Path:
    """A temporary file beside ``path`` holding ``text``, with ``path``'s
    mode, ready to replace it: a reader never sees a half-written file."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        shutil.copymode(path, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


@contextlib.contextmanager
def _locked(dirs: list[Path]) -> Iterator[None]:
    """Hold an exclusive lock on each directory (sorted, so two runs never
    wait on each other), so two publishes never interleave their
    read-modify-write of a file there."""
    with contextlib.ExitStack() as stack:
        for d in sorted(set(dirs)):
            fd = os.open(d, os.O_RDONLY)
            stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield


class YamlFilePublisher(Publisher):
    name = "yaml_file"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        unknown = sorted(str(k) for k in config if k not in _CONFIG_KEYS)
        if unknown:
            raise ValueError(
                f"yaml_file publisher: unknown configuration key(s) {', '.join(unknown)} "
                f"(known: {', '.join(_CONFIG_KEYS)})"
            )
        files = config.get("files")
        if not isinstance(files, Mapping) or not files:
            raise ValueError(
                "yaml_file publisher: `files` is required, a mapping of <key> → yaml path"
            )
        paths: dict[str, str] = {}
        for key, raw in files.items():
            if not isinstance(key, str) or not key or ":" in key:
                raise ValueError(
                    f"yaml_file publisher: `files` key must be a non-empty string without "
                    f"`:` (got {key!r})"
                )
            if not isinstance(raw, str) or not raw.startswith("/"):
                raise ValueError(
                    f"yaml_file publisher: `files.{key}` must be absolute after $VAR "
                    f"expansion, or ./-relative to the config dir (got {raw!r})"
                )
            try:
                raw.format(env="env")
            except (KeyError, IndexError, ValueError) as err:
                raise ValueError(
                    f"yaml_file publisher: `files.{key}` may hold only the `{{env}}` "
                    f"placeholder (got {raw!r}: {err!r})"
                ) from err
            paths[key] = raw
        apply = config.get("apply")
        if apply is not None and (
            not isinstance(apply, list)
            or not apply
            or not all(isinstance(a, str) and a for a in apply)
        ):
            raise ValueError("yaml_file publisher: `apply` must be a non-empty list of strings")
        self._paths: dict[str, str] | None = paths
        self._apply: list[str] | None = list(apply) if apply is not None else None
        self._all_keys: frozenset[str] | None = None
        # env → (path, location) → (what, query, value): the env's update,
        # written by apply. An env in _failed writes nothing this run.
        self._staged: dict[str, dict[tuple[Path, tuple[Any, ...]], tuple[str, str, str]]] = {}
        self._failed: set[str] = set()

    @classmethod
    def schema_instance(cls) -> YamlFilePublisher:
        """An instance with no destination: checks addresses and queries,
        and ``<key>`` only once :meth:`bind_config` has named every
        destination's keys."""
        inst = cls.__new__(cls)
        inst._paths = None
        inst._apply = None
        inst._all_keys = None
        inst._staged = {}
        inst._failed = set()
        return inst

    def bind_config(self, cfg: Any) -> None:
        """Record the ``files`` keys of every ``yaml_file`` destination of
        every target, so an address another destination handles is told
        apart from an unknown key."""
        targets = getattr(cfg, "targets", None)
        if not isinstance(targets, Mapping):
            return
        keys: set[str] = set()
        for target in targets.values():
            for dest in getattr(target, "destinations", ()):
                files = dest.config.get("files") if dest.publisher == self.name else None
                if isinstance(files, Mapping):
                    keys.update(str(k) for k in files)
        self._all_keys = frozenset(keys)

    def applies(self) -> bool:
        return True  # apply writes the staged files, command or not

    # --- paths ---

    def _path(self, key: str, env: str) -> Path:
        assert self._paths is not None
        return Path(self._paths[key].format(env=env)).resolve()

    def _check_key(self, key: str, what: str) -> None:
        """Raise unless ``key`` is a file key some destination handles:
        any target's once :meth:`bind_config` ran, else this one's."""
        if self._all_keys is not None:
            if key not in self._all_keys and (self._paths is None or key not in self._paths):
                known = sorted(self._all_keys | set(self._paths or ()))
                raise AddressError(
                    f"{what}: unknown file key {key!r} (no yaml_file destination names it; "
                    f"known: {', '.join(known) or '(none)'})"
                )
            return
        if self._paths is not None and key not in self._paths:
            raise AddressError(
                f"{what}: unknown file key {key!r} (the destination's `files`: "
                f"{', '.join(self._paths)})"
            )

    def _handles(self, key: str) -> bool:
        """Whether this destination writes ``key``'s file (its ``files``
        name it)."""
        return self._paths is None or key in self._paths

    # --- validate ---

    def validate(
        self,
        *,
        manifest_path: Path,
        section: Any,
        secrets: Mapping[str, Any] | None = None,
    ) -> list[str]:
        loc = str(manifest_path)
        if not isinstance(section, Mapping) or not section:
            return [f"{loc}: `publishers.yaml_file` must be a non-empty env-keyed mapping"]
        known = set(secrets) if isinstance(secrets, Mapping) and secrets else set()
        errors: list[str] = []
        for env, body in section.items():
            if isinstance(env, str) and env.startswith("_"):
                continue
            if not isinstance(env, str) or not env:
                errors.append(f"{loc}: publishers.yaml_file key must be a non-empty env name")
                continue
            if not isinstance(body, Mapping) or not body:
                errors.append(
                    f"{loc}: publishers.yaml_file[{env!r}] must be a non-empty mapping of "
                    "local_name → <key>:<query>"
                )
                continue
            for local_name, address in body.items():
                where = f"{loc}: publishers.yaml_file[{env!r}][{local_name!r}]"
                if known and local_name not in known:
                    errors.append(
                        f"{loc}: yaml_file entry for {local_name!r} has no matching secret"
                    )
                parsed = _parse_address(address)
                if parsed is None:
                    errors.append(
                        f"{where}: address must be `<key>:<query>`, the query a JSONPath "
                        f"starting at `$` (got {address!r})"
                    )
                    continue
                key, query = parsed
                try:
                    _compile(query)
                except jsonpath.JSONPathError as err:
                    first = str(err).splitlines()[0]
                    errors.append(f"{where}: query {query!r} does not parse: {first}")
                try:
                    self._check_key(key, where)
                except AddressError as err:
                    errors.append(str(err))
        return errors

    # --- publish ---

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
        body = section.get(env) if isinstance(section, Mapping) else None
        if not isinstance(body, Mapping):
            return PublishResult(written=0, skipped=0, failed=0)

        skipped = 0
        fail = 0
        updates: dict[str, list[tuple[str, str, str]]] = {}
        for local_name, address in body.items():
            if names is not None and local_name not in names:
                continue
            parsed = _parse_address(address)
            if parsed is None:
                continue  # validate reports it
            key, query = parsed
            try:
                self._check_key(key, f"{manifest_path.name}: secret {local_name!r}")
            except AddressError as err:
                sys.stderr.write(f"  ERROR env={env} {err}\n")
                fail += 1
                continue
            if not self._handles(key):
                continue  # another target's yaml_file destination writes it
            value = secrets.get(local_name)
            if value is None or value == "":
                sys.stderr.write(
                    f"  {manifest_path.name}: secret {local_name!r} is empty; skipped\n"
                )
                skipped += 1
                continue
            updates.setdefault(key, []).append((local_name, query, str(value)))

        if env in self._failed:
            n = sum(len(fields) for fields in updates.values())
            if n:
                sys.stderr.write(
                    f"  {manifest_path.name}: {n} secret(s) not written: an earlier "
                    f"yaml_file write for env={env} failed\n"
                )
            return PublishResult(written=0, skipped=skipped + n, failed=fail)

        # Check every address against the file now, dry run or not; apply
        # writes them all once every manifest's publish succeeded.
        staged = self._staged.setdefault(env, {})
        mine: dict[tuple[Path, tuple[Any, ...]], tuple[str, str, str]] = {}
        ok = 0
        for key, fields in updates.items():
            path = self._path(key, env)
            try:
                data, _yaml = _load(path)
                for local_name, query, value in fields:
                    what = f"{manifest_path.name}: secret {local_name!r}: query {query!r}"
                    node = _one_scalar(_select(data, query), what)
                    at = (path, tuple(node.location))
                    if at in staged or at in mine:
                        other = (staged.get(at) or mine[at])[0]
                        raise AddressError(f"{what} selects the node {other} also sets")
                    mine[at] = (what, query, value)
                    if dry_run:
                        sys.stderr.write(
                            f"  [dry-run] env={env} secrets[{local_name!r}] -> {path}:{query}\n"
                        )
                ok += len(fields)
            except (OSError, ValueError, jsonpath.JSONPathError) as err:
                sys.stderr.write(f"  ERROR writing {path} (env={env}): {err}\n")
                fail += len(fields)
        if fail:
            # All or nothing: drop what this env staged, write nothing to it.
            self._staged.pop(env, None)
            self._failed.add(env)
            sys.stderr.write(f"  env={env}: no yaml_file written (a write failed)\n")
            return PublishResult(written=0, skipped=skipped, failed=fail)
        staged.update(mine)
        return PublishResult(written=ok, skipped=skipped, failed=fail)

    # --- apply ---

    def _commit(self, update: Mapping[tuple[Path, tuple[Any, ...]], tuple[str, str, str]]) -> None:
        """Write ``update`` to its files, all or none. Holding the files'
        directory locks, re-read each file (a concurrent run's write is
        kept), set each node, then replace the files one by one, putting
        back the originals if one fails. Raises :class:`RuntimeError`."""
        by_path: dict[Path, list[tuple[str, str, str]]] = {}
        for (path, _loc), entry in update.items():
            by_path.setdefault(path, []).append(entry)
        paths = sorted(by_path)
        with _locked([p.parent for p in paths]):
            originals: dict[Path, str] = {}
            tmps: dict[Path, Path] = {}
            try:
                try:
                    for path in paths:
                        originals[path] = path.read_text(encoding="utf-8")
                        yaml = _round_trip(originals[path])
                        data = yaml.load(originals[path])
                        # Re-check what publish checked: a concurrent write
                        # can make a query match more nodes, or two queries
                        # meet on one node.
                        located: dict[tuple[Any, ...], tuple[str, str]] = {}
                        for what, query, value in by_path[path]:
                            at = tuple(_one_scalar(_select(data, query), what).location)
                            if at in located:
                                raise AddressError(
                                    f"{what} selects the node {located[at][0]} also sets "
                                    "(the file changed since publish checked it)"
                                )
                            located[at] = (what, value)
                        for location, (_what, value) in located.items():
                            _set(data, location, value)
                        out = io.StringIO()
                        _dump(yaml, data, out)
                        tmps[path] = _tmp_beside(path, out.getvalue())
                except (OSError, ValueError, jsonpath.JSONPathError) as err:
                    raise RuntimeError(f"nothing written: {err}") from err
                replaced: list[Path] = []
                try:
                    for path in paths:
                        os.replace(tmps[path], path)
                        del tmps[path]
                        replaced.append(path)
                except OSError as err:
                    stuck: list[str] = []
                    for path in replaced:
                        try:
                            tmps[path] = _tmp_beside(path, originals[path])
                            os.replace(tmps[path], path)
                            del tmps[path]
                        except OSError as undo:
                            stuck.append(f"{path} ({undo})")
                    if stuck:
                        raise RuntimeError(
                            f"writing {', '.join(str(p) for p in paths)} failed ({err}) and "
                            f"putting back the originals failed for {', '.join(stuck)}: "
                            "those files hold the new values, the others the old "
                            "(state indeterminate; re-run the publish)"
                        ) from err
                    raise RuntimeError(f"nothing written (originals put back): {err}") from err
            finally:
                for tmp in tmps.values():
                    tmp.unlink(missing_ok=True)

    def apply(self, *, env: str, dry_run: bool) -> None:
        """Write the env's staged update (all its files or none), then run
        the destination's ``apply`` command, so the reader picks up the
        edited files."""
        update = self._staged.pop(env, {})
        if dry_run:
            return
        if update:
            self._commit(update)
        if self._apply is None:
            return
        try:
            subprocess.run(
                self._apply,
                timeout=_APPLY_TIMEOUT_S,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"apply command timed out after {_APPLY_TIMEOUT_S}s") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise RuntimeError(f"apply command failed (exit {e.returncode}): {stderr or e}") from e

    # --- collect ---

    def collect(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
    ) -> CollectResult:
        """Read each node back. An address that cannot be read (unknown
        key, a query matching several nodes or a non-scalar) raises: the
        destination's state is then unknown."""
        loaded: dict[str, Any] = {}

        def file_data(key: str) -> Any:
            if key not in loaded:
                try:
                    loaded[key] = _load(self._path(key, env))[0]
                except FileNotFoundError:
                    loaded[key] = None
            return loaded[key]

        entries: list[CollectedEntry] = []
        for ref in manifests:
            body = ref.section.get(env) if isinstance(ref.section, Mapping) else None
            if not isinstance(body, Mapping):
                continue
            for local_name, address in body.items():
                parsed = _parse_address(address)
                if parsed is None:
                    continue
                key, query = parsed
                what = f"{ref.path.name}: secret {local_name!r}: query {query!r}"
                self._check_key(key, what)
                if not self._handles(key):
                    continue  # another target's yaml_file destination holds it
                manifest_value = ref.secrets.get(local_name)
                manifest_str = "" if manifest_value is None else str(manifest_value)
                assert self._paths is not None
                store_address = f"{self._paths[key].format(env=env)}:{query}"
                data = file_data(key)
                nodes = _select(data, query) if data is not None else []
                if not nodes:
                    state, value, paired = "manifest_only", manifest_str, None
                else:
                    store_value = _one_scalar(nodes, what).value
                    if str(store_value) == manifest_str:
                        state, value, paired = "same", manifest_str, None
                    else:
                        state, value, paired = "differs", str(store_value), manifest_str
                entries.append(
                    CollectedEntry(
                        state=state,
                        manifest_path=ref.path,
                        local_name=local_name,
                        store_address=store_address,
                        value=value,
                        manifest_value=paired,
                    )
                )
        return CollectResult(entries=entries)


__all__ = ["YamlFilePublisher"]
