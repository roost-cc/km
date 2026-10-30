"""TemplateFilePublisher — render `${var}` templates to per-env output files.

The second backing store km can drive (S-378). Reads a template at
``template`` (relative to the project's ``directories.templates``)
and writes a rendered file to ``output`` (with ``{env}`` substituted,
relative to the destination's ``root``), one per spec in the section's
list. The template language is Python's ``string.Template`` (only the
braced ``${name}`` form is accepted).

Manifest schema — the section is a list of specs directly::

    publishers:
      template_file:
        - template: infisical.tmpl.env
          output: infisical.{env}.env
          envs:
            dev:
              <local_name>: <template_var>
            test:
              <local_name>: <template_var>
        - template: another.tmpl.env
          output: another.{env}.env
          envs:
            dev: { ... }

For each per-env entry the rendered file's ``${template_var}`` is filled
from ``secrets[local_name]`` of the manifest carrying this section.
Specs whose ``envs:`` map omits the current env are skipped — env
membership is what selects a spec for an env.

There is no ``store_only``: the rendered file is the store, every line
came from us, and drift is whole-file (``differs`` or ``manifest_only``).

Destination configuration: a local destination names its ``root``, a
remote one its ``transport`` and ``remote_dir`` (F-56 D4; km supplies no
default, so a rendered file never lands in the project)::

    - publisher: template_file
      envs: [dev]
      root: $HOME/secrets

    - publisher: template_file
      envs: [dev]
      transport: [ssh, deploy@staging.example.com]
      remote_dir: /home/deploy/secrets
      apply: [ssh, deploy@staging.example.com, systemctl, --user, restart, app-stack]

- ``root``: the directory outputs render into (``<root>/<output>``);
  absolute after ``$VAR`` expansion, or ``./``-relative to the config
  dir. Required without ``transport``, not allowed with it.
- ``transport`` (F-54 D5): a command that runs a shell command on the
  target, given as its one extra argument (``ssh.sh`` is one; ``[sh,
  -c]`` is the local equivalent). A remote destination writes each file
  straight to ``<remote_dir>/<output>`` by running ``mkdir``/``cat``/``mv``
  through the transport with the rendered bytes on stdin: the file is
  staged beside the target's, then moved over it, so a failure leaves the
  live file as it was. Nothing is written locally. Readback runs
  ``sha256sum`` on the remote file through the transport, so no secret
  travels back. ``remote_dir`` is required with ``transport``.
- ``apply``: a command run once per env after every file of the
  destination is published, when the publish wrote one.

``output:`` is relative to the root (or ``remote_dir``) and may not
leave it.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import secrets as secrets_mod
import shlex
import subprocess
import sys
from collections.abc import Collection, Mapping
from pathlib import Path
from string import Template
from typing import Any

from km.publishers import (
    CollectedEntry,
    CollectResult,
    EnsureResult,
    ManifestRef,
    Publisher,
    PublishResult,
)

_REQUIRED_SPEC_KEYS = frozenset({"template", "output", "envs"})
_CONFIG_KEYS = frozenset({"root", "transport", "remote_dir", "apply"})
_TRANSPORT_TIMEOUT_S = 60
_APPLY_TIMEOUT_S = 300


def _command(config: Mapping[str, Any], key: str) -> list[str] | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(a, str) and a for a in value):
        raise ValueError(f"template_file publisher: `{key}` must be a non-empty list of strings")
    return list(value)


def check_config(config: Mapping[str, Any]) -> None:
    """Raise :class:`ValueError` when ``config`` is not a valid
    destination configuration. ``km.config`` runs it at load, so a bad
    destination fails the load rather than the run."""
    unknown = sorted(str(k) for k in config if k not in _CONFIG_KEYS)
    if unknown:
        raise ValueError(
            f"template_file publisher: unknown configuration key(s) {', '.join(unknown)} "
            f"(known: {', '.join(sorted(_CONFIG_KEYS))})"
        )
    transport = _command(config, "transport")
    _command(config, "apply")
    remote_dir = config.get("remote_dir")
    root = config.get("root")
    if transport is not None:
        if not isinstance(remote_dir, str) or not remote_dir.startswith("/"):
            raise ValueError(
                "template_file publisher: `transport` needs `remote_dir`, the "
                "absolute directory on the target that outputs render into"
            )
        if root is not None:
            raise ValueError(
                "template_file publisher: `root` is a local destination's; a remote one "
                "writes under `remote_dir` and nothing locally"
            )
        return
    if remote_dir is not None:
        raise ValueError("template_file publisher: `remote_dir` needs `transport`")
    if root is None:
        raise ValueError(
            "template_file publisher: `root` is required, the directory outputs render "
            "into (absolute, or ./-relative to the config dir)"
        )
    if not isinstance(root, str) or not root.startswith("/"):
        raise ValueError(
            f"template_file publisher: `root` must be absolute after $VAR expansion, or "
            f"./-relative to the config dir (got {root!r})"
        )


class TemplateFilePublisher(Publisher):
    """Renders ``${var}`` templates and audits their outputs.

    Relative ``template:`` paths resolve against
    :attr:`Publisher.templates_root` (from ``directories.templates``, set
    by the loader); absolute ones are accepted as-is. ``output:`` paths
    resolve against the destination's ``root``, or ``remote_dir`` on the
    target.
    """

    name = "template_file"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        check_config(config)
        self._transport = _command(config, "transport")
        self._apply = _command(config, "apply")
        self._remote_dir: str | None = config.get("remote_dir")
        root = config.get("root")
        self._root: Path | None = Path(root).resolve() if root is not None else None

    @classmethod
    def schema_instance(cls) -> TemplateFilePublisher:
        """An instance with no destination, good for :meth:`validate` only."""
        inst = cls.__new__(cls)
        inst._transport = inst._apply = None
        inst._remote_dir = inst._root = None
        return inst

    @property
    def remote(self) -> bool:
        return self._transport is not None

    def applies(self) -> bool:
        return self._apply is not None

    def declared_envs(self, section: Any) -> set[str]:
        """template_file's section is a list of ``{template, output, envs}`` specs."""
        out: set[str] = set()
        if not isinstance(section, list):
            return out
        for spec in section:
            if not isinstance(spec, Mapping):
                continue
            envs = spec.get("envs")
            if not isinstance(envs, Mapping):
                continue
            for env in envs.keys():
                if isinstance(env, str) and env:
                    out.add(env)
        return out

    # --- validate ---

    def validate(
        self,
        *,
        manifest_path: Path,
        section: Any,
        secrets: Mapping[str, Any] | None = None,
    ) -> list[str]:
        loc = str(manifest_path)
        errors: list[str] = []
        if not isinstance(section, list) or not section:
            return [f"{loc}: `publishers.template_file` must be a non-empty list of specs"]

        secrets_keys: set[str] = (
            {k for k in secrets.keys() if isinstance(k, str)}
            if isinstance(secrets, Mapping) and secrets
            else set()
        )
        for i, spec in enumerate(section):
            errors.extend(self._validate_spec(loc, i, spec, secrets_keys))
        return errors

    def _validate_spec(
        self,
        loc: str,
        idx: int,
        spec: Any,
        secrets_keys: set[str],
    ) -> list[str]:
        prefix = f"{loc}: publishers.template_file[{idx}]"
        errors: list[str] = []
        if not isinstance(spec, Mapping):
            return [f"{prefix} must be a mapping"]

        # Reject unknown keys outright — extension is cheap when needed.
        for key in spec.keys():
            if key not in _REQUIRED_SPEC_KEYS:
                errors.append(f"{prefix}: unknown key {key!r}")

        template_rel = spec.get("template")
        if not isinstance(template_rel, str) or not template_rel:
            errors.append(f"{prefix}.template must be a non-empty string")
            template_rel = None

        output_rel = spec.get("output")
        if not isinstance(output_rel, str) or not output_rel:
            errors.append(f"{prefix}.output must be a non-empty string")
        else:
            # Resolve output with a probe value substituted for `{env}` so
            # path-traversal / leading-slash escapes are caught at validate
            # time, not deferred to publish where they only surface as a
            # `failed` count.
            try:
                probe_output = output_rel.format(env="_validate")
            except (KeyError, IndexError) as e:
                errors.append(f"{prefix}.output: format placeholder error: {e}")
                probe_output = None
            if probe_output is not None:
                try:
                    _check_output(probe_output)
                except ValueError as e:
                    errors.append(f"{prefix}.output: {e}")

        envs = spec.get("envs")
        if not isinstance(envs, Mapping) or not envs:
            errors.append(f"{prefix}.envs must be a non-empty mapping")
            return errors

        # Resolve and read the template before validating per-env bodies.
        template_text: str | None = None
        template_vars: set[str] = set()
        if template_rel:
            try:
                template_path = self._resolve_template_path(template_rel)
            except ValueError as e:
                errors.append(f"{prefix}.template: {e}")
                template_path = None
            if template_path is not None:
                try:
                    template_text = template_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    errors.append(f"{prefix}.template: file does not exist ({template_rel})")
                except OSError as e:
                    errors.append(f"{prefix}.template: {e}")
        if template_text is not None:
            try:
                template_vars = _extract_template_vars(template_text)
            except ValueError as e:
                errors.append(f"{prefix}.template: {e}")
                template_text = None

        for env_name, env_body in envs.items():
            env_prefix = f"{prefix}.envs[{env_name!r}]"
            if not isinstance(env_name, str) or not env_name:
                errors.append(f"{prefix}.envs key must be a non-empty env name (got {env_name!r})")
                continue
            if not isinstance(env_body, Mapping) or not env_body:
                errors.append(
                    f"{env_prefix} must be a non-empty mapping of local_name → template_var"
                )
                continue
            seen_vars: dict[str, str] = {}
            body_vars: set[str] = set()
            checked_secret_names: set[str] = set()
            for local_name, tmpl_var in env_body.items():
                if not isinstance(local_name, str) or not local_name:
                    errors.append(
                        f"{env_prefix} key must be a non-empty local secret "
                        f"name (got {local_name!r})"
                    )
                    continue
                # Vendor-style cross-reference: when secrets: is non-empty
                # the manifest is the source of truth and every
                # template_file local_name must resolve to a key there.
                # (Empty secrets: = system-style manifest; values are
                # minted at rotation and checked through the rotation
                # cross-reference walk.)
                if (
                    secrets_keys
                    and local_name not in secrets_keys
                    and local_name not in checked_secret_names
                ):
                    errors.append(
                        f"{loc}: template_file entry for '{local_name}' has no matching secret"
                    )
                    checked_secret_names.add(local_name)
                if not isinstance(tmpl_var, str) or not tmpl_var:
                    errors.append(
                        f"{env_prefix}[{local_name!r}]: template var must be a "
                        f"non-empty string (got {tmpl_var!r})"
                    )
                    continue
                if tmpl_var in seen_vars:
                    errors.append(
                        f"{env_prefix}: template var {tmpl_var!r} is bound to "
                        f"both {seen_vars[tmpl_var]!r} and {local_name!r}"
                    )
                    continue
                seen_vars[tmpl_var] = local_name
                body_vars.add(tmpl_var)

            if template_text is None:
                continue
            missing = sorted(template_vars - body_vars)
            extra = sorted(body_vars - template_vars)
            for var in missing:
                errors.append(
                    f"{env_prefix}: template var ${{{var}}} has no entry in this env's body"
                )
            for var in extra:
                errors.append(
                    f"{env_prefix}: entry binds template var {var!r} which is "
                    "not present in the template"
                )

        return errors

    # --- ensure ---

    def ensure(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
        dry_run: bool,
    ) -> EnsureResult:
        """Create the output directory of every spec rendered for ``env``
        (a remote destination's are made on the target as it writes)."""
        if self.remote:
            return EnsureResult()
        dirs: list[Path] = []
        for ref in manifests:
            if not isinstance(ref.section, list):
                continue
            for spec in ref.section:
                if not isinstance(spec, Mapping):
                    continue
                envs = spec.get("envs")
                output_rel = spec.get("output")
                if not isinstance(envs, Mapping) or env not in envs:
                    continue
                if not isinstance(output_rel, str):
                    continue
                try:
                    output_path = self._output_path(output_rel.format(env=env))
                    assert output_path is not None
                    parent = output_path.parent
                except (ValueError, KeyError, IndexError):
                    continue  # publish reports the bad spec
                if parent not in dirs:
                    dirs.append(parent)
        created: list[str] = []
        failed = 0
        for d in dirs:
            if d.is_dir():
                continue
            label = f"directory {d}"
            if dry_run:
                sys.stderr.write(f"  [dry-run] would create {label}\n")
                created.append(label)
                continue
            try:
                d.mkdir(parents=True, exist_ok=True)
                created.append(label)
            except OSError as err:
                sys.stderr.write(f"  ERROR creating {label}: {err}\n")
                failed += 1
        return EnsureResult(created=tuple(created), failed=failed)

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
        """Render every spec for ``env``; with ``names``, only the specs
        whose env body uses a selected secret, each in full."""
        if not isinstance(section, list):
            return PublishResult(written=0, skipped=0, failed=0)

        ok = 0
        skipped = 0
        fail = 0
        manifest_label = manifest_path.name

        for spec in section:
            if not isinstance(spec, Mapping):
                continue
            envs = spec.get("envs")
            if not isinstance(envs, Mapping):
                continue
            env_body = envs.get(env)
            if not isinstance(env_body, Mapping):
                continue
            if names is not None and not any(n in names for n in env_body):
                continue

            template_rel = spec.get("template")
            output_rel = spec.get("output")
            if not isinstance(template_rel, str) or not isinstance(output_rel, str):
                continue

            try:
                template_path = self._resolve_template_path(template_rel)
                rendered_output = output_rel.format(env=env)
                output_path = self._output_path(rendered_output)
            except (ValueError, KeyError, IndexError) as err:
                sys.stderr.write(f"  ERROR rendering {output_rel} (env={env}): {err}\n")
                fail += 1
                continue

            try:
                template_text = template_path.read_text(encoding="utf-8")
            except OSError as err:
                sys.stderr.write(f"  ERROR reading template {template_rel} (env={env}): {err}\n")
                fail += 1
                continue

            template_vars: dict[str, str] = {}
            empty_skipped = False
            for local_name, tmpl_var in env_body.items():
                if not isinstance(local_name, str) or not isinstance(tmpl_var, str):
                    continue
                value = secrets.get(local_name)
                if value is None or value == "":
                    sys.stderr.write(
                        f"  {manifest_label}: env={env} ${{{tmpl_var}}}={local_name!r} "
                        "empty; skipped\n"
                    )
                    empty_skipped = True
                    break
                template_vars[tmpl_var] = str(value)

            if empty_skipped:
                skipped += 1
                continue

            try:
                rendered = Template(template_text).substitute(template_vars)
            except KeyError as err:
                sys.stderr.write(
                    f"  ERROR rendering {output_rel} (env={env}): unresolved template var {err}\n"
                )
                fail += 1
                continue

            if dry_run:
                where = (
                    f"{self._remote_path(rendered_output)} through the transport"
                    if self.remote
                    else str(output_path)
                )
                sys.stderr.write(f"  [dry-run] would render env={env} output={where}\n")
                ok += 1
                continue

            if self.remote:
                if self._write_remote(rendered_output, rendered.encode("utf-8"), env):
                    ok += 1
                else:
                    fail += 1
                continue

            # Staged beside the output, then moved over it, so a reader
            # never sees a half-written file. Staging names are unique per
            # call, so two publishes of one output never commit or remove
            # each other's bytes.
            assert output_path is not None
            tmp: Path | None = None
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = _create_stage(output_path)
                tmp.write_text(rendered, encoding="utf-8")
                os.replace(tmp, output_path)
            except OSError as err:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
                sys.stderr.write(f"  ERROR writing {output_rel} (env={env}): {err}\n")
                fail += 1
                continue
            ok += 1

        return PublishResult(written=ok, skipped=skipped, failed=fail)

    # --- apply ---

    def apply(self, *, env: str, dry_run: bool) -> None:
        """Run the destination's ``apply`` command (once per env, after
        every file of the destination is published)."""
        if self._apply is None or dry_run:
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
        entries: list[CollectedEntry] = []
        for ref in manifests:
            if not isinstance(ref.section, list):
                continue
            for spec in ref.section:
                if not isinstance(spec, Mapping):
                    continue
                envs = spec.get("envs")
                if not isinstance(envs, Mapping):
                    continue
                env_body = envs.get(env)
                if not isinstance(env_body, Mapping):
                    continue
                template_rel = spec.get("template")
                output_rel = spec.get("output")
                if not isinstance(template_rel, str) or not isinstance(output_rel, str):
                    continue

                try:
                    rendered_output = output_rel.format(env=env)
                    template_path = self._resolve_template_path(template_rel)
                    output_path = self._output_path(rendered_output)
                except (ValueError, KeyError, IndexError) as err:
                    sys.stderr.write(
                        f"warn: {ref.path}: template_file collect skipped {output_rel} "
                        f"(env={env}): {err}\n"
                    )
                    continue

                expected_bytes: bytes | None = None
                render_error: str | None = None
                try:
                    template_text = template_path.read_text(encoding="utf-8")
                    template_vars: dict[str, str] = {}
                    expected_ok = True
                    for local_name, tmpl_var in env_body.items():
                        if not isinstance(local_name, str) or not isinstance(tmpl_var, str):
                            render_error = f"non-string entry in envs[{env!r}] body"
                            expected_ok = False
                            break
                        value = ref.secrets.get(local_name)
                        if value is None:
                            render_error = (
                                f"secrets[{local_name!r}] missing — cannot compute expected render"
                            )
                            expected_ok = False
                            break
                        template_vars[tmpl_var] = str(value)
                    if expected_ok:
                        expected_bytes = (
                            Template(template_text).substitute(template_vars).encode("utf-8")
                        )
                except (OSError, KeyError, ValueError) as err:
                    render_error = f"{type(err).__name__}: {err}"
                    expected_bytes = None

                # A remote destination is read back through the transport
                # (a transport failure is a reach error); a local one from
                # the file on disk.
                actual_sha: str | None
                if output_path is None:
                    actual_sha = self._remote_sha256(rendered_output)
                else:
                    try:
                        actual_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
                    except FileNotFoundError:
                        actual_sha = None
                    except OSError as err:
                        sys.stderr.write(
                            f"warn: {ref.path}: template_file collect read "
                            f"{rendered_output}: {err}\n"
                        )
                        actual_sha = None

                if actual_sha is None:
                    state = "manifest_only"
                elif (
                    expected_bytes is not None
                    and actual_sha == hashlib.sha256(expected_bytes).hexdigest()
                ):
                    state = "same"
                else:
                    state = "differs"
                    if expected_bytes is None:
                        sys.stderr.write(
                            f"warn: {ref.path}: template_file expected render failed for "
                            f"{rendered_output} (env={env}): "
                            f"{render_error or 'unknown error'} — reporting as differs\n"
                        )

                for local_name, tmpl_var in env_body.items():
                    if not isinstance(local_name, str) or not isinstance(tmpl_var, str):
                        continue
                    address = f"{rendered_output}:{tmpl_var}"
                    manifest_value = ref.secrets.get(local_name)
                    manifest_value_str = "" if manifest_value is None else str(manifest_value)
                    if state == "differs":
                        # Whole-file drift: we can't attribute which entry
                        # diverged. Leave manifest_value unset so audit's
                        # verbose suffix doesn't print "manifest='X'
                        # store='X'" for an honest drift.
                        entries.append(
                            CollectedEntry(
                                state="differs",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=address,
                                value=manifest_value_str,
                            )
                        )
                    elif state == "manifest_only":
                        entries.append(
                            CollectedEntry(
                                state="manifest_only",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=address,
                                value=manifest_value_str,
                            )
                        )
                    else:
                        entries.append(
                            CollectedEntry(
                                state="same",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=address,
                                value=manifest_value_str,
                            )
                        )

        return CollectResult(entries=entries)

    # --- helpers ---

    def _resolve_template_path(self, raw: str) -> Path:
        """Resolve a manifest-supplied ``template:`` path.

        Absolute paths (typically produced by env-var expansion at
        manifest load — e.g. ``$HOME/...``) are accepted as-is.
        Relative paths resolve against the project's
        ``directories.templates`` root (set on
        :attr:`Publisher.templates_root` by the loader).
        """
        if self.templates_root is None:
            raise ValueError(
                "templates_root not set on publisher — load via km.loader.load_publisher "
                "(or assign `pub.templates_root = <Path>` directly in tests)"
            )
        return self._resolve_against(raw, self.templates_root, "templates_root")

    def _output_path(self, rendered_output: str) -> Path | None:
        """The local path ``rendered_output`` renders to, ``<root>/<output>``;
        None for a remote destination, which writes nothing locally."""
        _check_output(rendered_output)
        if self.remote:
            return None
        if self._root is None:
            raise ValueError("template_file publisher: no `root` (a schema-only instance)")
        return self._resolve_against(rendered_output, self._root, "root")

    def _remote_path(self, rendered_output: str) -> str:
        assert self._remote_dir is not None
        return posixpath.join(self._remote_dir, rendered_output)

    def _write_remote(self, rendered_output: str, data: bytes, env: str) -> bool:
        """Stage ``data`` on the target, then move it over the target's
        file; on a failed move remove the staged file, so the live file is
        as it was. False (reported) when the write failed."""
        try:
            stage = self._stage_remote(rendered_output, data)
        except RuntimeError as err:
            sys.stderr.write(
                f"  ERROR copying {rendered_output} (env={env}) to the target: {err}\n"
            )
            return False
        try:
            remote = self._commit_remote(stage, rendered_output)
        except RuntimeError as err:
            sys.stderr.write(
                f"  ERROR copying {rendered_output} (env={env}) to the target: {err}\n"
            )
            self._discard_remote(stage)
            return False
        sys.stderr.write(f"  copied env={env} {rendered_output} -> {remote}\n")
        return True

    def _run_transport(self, script: str, data: bytes | None = None) -> bytes:
        """Run ``script`` on the target through the transport; return stdout."""
        assert self._transport is not None
        try:
            done = subprocess.run(
                [*self._transport, script],
                input=data if data is not None else b"",
                capture_output=True,
                timeout=_TRANSPORT_TIMEOUT_S,
                check=True,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"transport timed out after {_TRANSPORT_TIMEOUT_S}s") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"transport failed (exit {e.returncode}): {stderr or e}") from e
        except OSError as e:
            raise RuntimeError(f"transport could not run: {e}") from e
        return done.stdout

    def _stage_remote(self, rendered_output: str, data: bytes) -> str:
        """Copy ``data`` to a staging file beside the target's file for
        ``rendered_output``; return the staging path."""
        remote = self._remote_path(rendered_output)
        q = shlex.quote
        tmp = posixpath.join(
            posixpath.dirname(remote),
            f".{posixpath.basename(remote)}.{secrets_mod.token_hex(8)}.tmp",
        )
        self._run_transport(
            f"umask 077 && mkdir -p {q(posixpath.dirname(remote))} && "
            f"{{ cat > {q(tmp)} || {{ rm -f {q(tmp)}; exit 1; }}; }}",
            data,
        )
        return tmp

    def _commit_remote(self, stage: str, rendered_output: str) -> str:
        """Move a staged file over the target's file; return its path."""
        remote = self._remote_path(rendered_output)
        q = shlex.quote
        self._run_transport(f"mv -f {q(stage)} {q(remote)} || {{ rm -f {q(stage)}; exit 1; }}")
        return remote

    def _discard_remote(self, stage: str) -> None:
        """Remove a staged file; the target's file was never touched."""
        try:
            self._run_transport(f"rm -f {shlex.quote(stage)}")
        except RuntimeError as err:
            sys.stderr.write(
                f"  ERROR removing {stage} from the target (its live file is unchanged): {err}\n"
            )

    def _remote_sha256(self, rendered_output: str) -> str | None:
        """The sha256 of the target's file, or None when it is absent."""
        remote = shlex.quote(self._remote_path(rendered_output))
        out = self._run_transport(
            f"if [ -f {remote} ]; then sha256sum < {remote}; else echo absent; fi"
        )
        token = out.decode("utf-8", "replace").split()[:1]
        if token == ["absent"]:
            return None
        if not token or len(token[0]) != 64:
            raise RuntimeError(f"transport readback: unexpected sha256sum output {out[:80]!r}")
        return token[0]

    def _resolve_against(self, raw: str, root: Path, root_label: str) -> Path:
        if raw.startswith("/"):
            return Path(raw).resolve()
        candidate = (root / raw).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path escapes {root_label} (got {raw!r})")
        return candidate


def _check_output(raw: str) -> None:
    """Raise :class:`ValueError` unless ``raw`` is a relative path that
    stays inside the root it renders under."""
    if raw.startswith("/"):
        raise ValueError(f"an output is relative to the destination's root (got {raw!r})")
    norm = posixpath.normpath(raw)
    if norm == "." or norm == ".." or norm.startswith("../"):
        raise ValueError(f"path escapes the destination's root (got {raw!r})")


def _create_stage(output_path: Path) -> Path:
    """Create an empty staging file beside ``output_path``, under a name
    no other call uses. Its mode is a plain open's (0666 less the umask),
    as the output's was before staging: a service in a container, Synapse
    for one, reads these files as another user."""
    tmp = output_path.parent / f".{output_path.name}.{secrets_mod.token_hex(8)}.tmp"
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
    return tmp


def _extract_template_vars(text: str) -> set[str]:
    """Return the set of ``${var}`` names referenced in ``text``.

    Only the braced form is accepted — bare ``$var`` is rejected so the
    schema is unambiguous (no way for an operator to silently rely on a
    bareword that the publisher's per-env body never declares).
    """
    out: set[str] = set()
    pattern = Template.pattern
    for m in pattern.finditer(text):
        if m.group("escaped") is not None:
            continue
        if m.group("invalid") is not None:
            raise ValueError(f"invalid template substitution at offset {m.start()}")
        named = m.group("named")
        braced = m.group("braced")
        if named is not None:
            raise ValueError(
                f"bare $-substitution {('$' + named)!r} not supported — use ${{...}} form"
            )
        if braced is not None:
            out.add(braced)
    return out


__all__ = ["TemplateFilePublisher"]
