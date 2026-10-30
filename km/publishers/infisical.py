"""InfisicalPublisher — Infisical-backed implementation of :class:`Publisher`.

Owns the per-env :class:`InfisicalClient` lifecycle for secrets and
uses :class:`InfisicalAdmin` for what :meth:`InfisicalPublisher.ensure`
creates: from an instance with no admin to a project with its
environments, folders and machine identities (F-54 D3), recorded in
``targets/<target>.yaml`` (D4).
"""

from __future__ import annotations

import fnmatch
import sys
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Any

from km import targets as targets_mod
from km.generators import ALPHANUMERIC, random_string
from km.infisical import Identity, InfisicalAdmin, InfisicalClient
from km.publishers import (
    CollectedEntry,
    CollectResult,
    EnsureResult,
    ManifestRef,
    Publisher,
    PublishResult,
)


def _split_full_path(full: str) -> tuple[str, str]:
    """Split ``/foo/bar/baz`` into ``("baz", "/foo/bar")``."""
    fp = full.rstrip("/")
    folder, _, secret = fp.rpartition("/")
    if not folder:
        folder = "/"
    return secret, folder


def _coerce_runtime_key(raw_path: str, name: str) -> tuple[str, str]:
    path = raw_path if raw_path.startswith("/") else "/" + raw_path
    path = path.rstrip("/") or "/"
    return (path, name)


def _resolve_addresses(
    env_map: Mapping[str, Any],
    secrets_keys: Iterable[str],
) -> list[tuple[str, str]]:
    """Expand an env-map into ``(local_name, full_path)`` pairs.

    Keys containing ``*`` are globs: every secret name matching the
    glob (via :func:`fnmatch.fnmatchcase`) gets its own pair, with the
    full path computed as ``<prefix>/<matched_name>`` (the value is
    treated as a folder prefix). Literal keys produce one pair as-is.
    """
    secrets_list = list(secrets_keys)
    out: list[tuple[str, str]] = []
    for local_pattern, ipath in env_map.items():
        if not isinstance(local_pattern, str) or not isinstance(ipath, str):
            continue
        if "*" in local_pattern:
            prefix = ipath.rstrip("/")
            for sk in secrets_list:
                if fnmatch.fnmatchcase(sk, local_pattern):
                    full = f"{prefix}/{sk}" if prefix else f"/{sk}"
                    out.append((sk, full))
        else:
            out.append((local_pattern, ipath))
    return out


def _mint_admin_password() -> str:
    """A 32-char alphanumeric password that meets Infisical's policy
    (at least one letter and one digit)."""
    while True:
        pw = random_string(32, ALPHANUMERIC)
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw


# (state key, identity name, project role) for each identity ensure keeps.
_IDENTITIES = (
    ("token", "km", "admin"),
    ("services_token", "services", "viewer"),
)
# Where ensure may mirror a value into a manifest overlay (``values:``).
_VALUE_KEYS = ("project_id", "services_token")
# What Infisical creates with every new project.
_DEFAULT_ENVS = frozenset({"dev", "staging", "prod"})


class InfisicalPublisher(Publisher):
    """Drives Infisical via per-env :class:`InfisicalClient` instances.

    Destination configuration (a ``targets.<target>`` entry):

    - ``host``: Infisical API base URL.
    - ``admin_email``, ``org_name``: the instance admin and organisation
      :meth:`ensure` creates when the instance has no admin yet.
    - ``project_name``: the project :meth:`ensure` creates when the
      target has no project id yet.
    - ``values``: where :meth:`ensure` also records the project id and
      the services' token as per-target manifest values, so templates
      can render them (``{project_id: <slug>:<secret>,
      services_token: <slug>:<secret>}``).

    The first three are needed only when there is something to create.

    Per-target state (``targets/<target>.yaml`` ``_publishers.infisical``),
    because a token is only valid against the instance that minted it:

    - ``project_id``: the project to talk to.
    - ``token``: km's machine-identity token (project ``admin``) used for
      every project request.
    - ``services_token``: the ``services`` identity's token (project
      ``viewer``), unless ``values.services_token`` puts it in a manifest
      overlay instead.
    - ``admin_password``, ``admin_token``, ``org_id``: the instance
      admin's password and the bootstrap identity's token, recorded when
      :meth:`ensure` bootstrapped the instance; the token creates the
      project and the identities.

    A km environment maps to the Infisical environment slug of the same
    name inside the project. Clients are constructed lazily —
    :meth:`validate` never opens one.
    """

    name = "infisical"
    keeps_target_state = True

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        moved = [k for k in ("token", "workspace_id") if k in config]
        if moved:
            raise ValueError(
                f"infisical publisher: {', '.join(moved)} moved out of config.yaml; "
                "km reads `project_id` and `token` from targets/<target>.yaml "
                "under `_publishers.infisical`"
            )
        known = {"host", "admin_email", "org_name", "project_name", "values"}
        unknown = sorted(set(config) - known)
        if unknown:
            raise ValueError(f"infisical publisher: unknown config key(s) {', '.join(unknown)}")
        if not isinstance(config.get("host"), str) or not config["host"]:
            raise ValueError("infisical publisher: config.host is required (non-empty string)")
        for key in ("admin_email", "org_name", "project_name"):
            if key in config and (not isinstance(config[key], str) or not config[key]):
                raise ValueError(f"infisical publisher: config.{key} must be a non-empty string")
        self._host: str = config["host"]
        self._admin_email: str | None = config.get("admin_email")
        self._org_name: str | None = config.get("org_name")
        self._project_name: str | None = config.get("project_name")
        self._values: dict[str, tuple[str, str]] = {}
        values = config.get("values") or {}
        if not isinstance(values, Mapping):
            raise ValueError("infisical publisher: config.values must be a mapping")
        for key, where in values.items():
            if key not in _VALUE_KEYS:
                raise ValueError(
                    f"infisical publisher: config.values.{key}: unknown key "
                    f"(one of {', '.join(_VALUE_KEYS)})"
                )
            slug, sep, secret = where.partition(":") if isinstance(where, str) else ("", "", "")
            if not sep or not slug or not secret:
                raise ValueError(
                    f"infisical publisher: config.values.{key} must be "
                    f"`<manifest slug>:<secret>` (got {where!r})"
                )
            self._values[key] = (slug, secret)
        self._clients: dict[str, InfisicalClient] = {}
        self._instance_ensured = False
        self._planned = False  # dry run: the project does not exist yet
        self._planned_envs: set[str] = set()  # dry run: envs that do not exist yet
        self._recorded: list[tuple[str, str]] = []

    # --- per-target state ---

    def _state(self) -> dict[str, Any]:
        """This publisher's live ``_publishers.infisical`` mapping."""
        if self.target_values is not None:
            return self.target_values.publishers.setdefault(self.name, {})
        if not isinstance(self.target_state, dict):
            self.target_state = dict(self.target_state or {})
        return self.target_state  # type: ignore[return-value]

    def _target_label(self) -> str:
        if self.target_values is not None:
            return str(self.target_values.path)
        return str(self.target_file or f"targets/{self.target_name or '<target>'}.yaml")

    def _get(self, key: str) -> str:
        if key in self._values and key != "project_id":
            slug, secret = self._values[key]
            tv = self.target_values
            value = (tv.manifests.get(slug) or {}).get(secret) if tv is not None else None
        else:
            value = self._state().get(key)
        return value if isinstance(value, str) else ""

    def _set(self, updates: Mapping[str, str]) -> None:
        """Record ``updates`` (state key → value) and write the target file.

        Written after every step, so a later failure never loses a
        credential the instance has already issued.
        """
        tv = self.target_values
        if tv is None:
            raise RuntimeError(
                f"infisical publisher: no targets/<target>.yaml to record {', '.join(updates)} in"
            )
        manifests: dict[str, dict[str, str]] = {}
        state: dict[str, str] = {}
        recorded: list[tuple[str, str]] = []
        for key, value in updates.items():
            if key in self._values:
                slug, secret = self._values[key]
                manifests.setdefault(slug, {})[secret] = value
                recorded.append((slug, secret))
                if key != "project_id":
                    continue
            state[key] = value
        targets_mod.update(
            tv, manifests=manifests, publishers={self.name: state} if state else None
        )
        self._recorded.extend(recorded)

    def provides(self) -> set[tuple[str, str]]:
        return set(self._values.values())

    def _credentials(self) -> tuple[str, str]:
        state = self._state()
        missing = [
            k for k in ("project_id", "token") if not isinstance(state.get(k), str) or not state[k]
        ]
        if missing:
            raise RuntimeError(
                f"infisical publisher: {self._target_label()} has no _publishers.infisical."
                f"{' / '.join(missing)} for {self._host}"
            )
        return state["project_id"], state["token"]

    def _client_for(self, env: str) -> InfisicalClient:
        if env not in self._clients:
            project_id, token = self._credentials()
            self._clients[env] = InfisicalClient(
                host=self._host,
                token=token,
                project_id=project_id,
                environment=env,
            )
        return self._clients[env]

    def _admin(self, token: str | None) -> InfisicalAdmin:
        return InfisicalAdmin(host=self._host, token=token)

    # --- ensure ---

    def ensure(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
        dry_run: bool,
    ) -> EnsureResult:
        """Bring the destination to what ``manifests`` need for ``env``.

        Once per run: bootstrap the instance if it has no admin, create
        the project if the target has none, and the ``km`` (admin) and
        ``services`` (viewer) identities with Token Auth tokens, each
        recorded in ``targets/<target>.yaml`` as soon as it exists. Then,
        per env: the environment, then every folder the manifests' paths
        need. A target whose file already holds the project id and both
        tokens touches nothing at instance level.

        Raises :class:`RuntimeError` when the instance already has an
        admin but km holds no credential for it (F-54 D3), and when the
        instance has no admin but the target file holds credentials (they
        belong to an earlier instance).
        """
        self._recorded = []
        created: list[str] = []
        if not self._instance_ensured:
            created.extend(self._ensure_instance(dry_run))
            self._instance_ensured = True
        created.extend(self._ensure_environment(env, dry_run))
        folders = self._ensure_folders(env, manifests, dry_run)
        return EnsureResult(
            created=tuple(created) + folders.created,
            failed=folders.failed,
            values=tuple(dict.fromkeys(self._recorded)),
        )

    def _ensure_instance(self, dry_run: bool) -> list[str]:
        state = self._state()
        with self._admin(None) as anon:
            initialized = anon.is_initialized()
        known = [k for k in ("project_id", "token", "admin_token") if state.get(k)]
        if not initialized and known:
            recorded = "".join(f", {s}.{n}" for s, n in sorted(self.provides()))
            raise RuntimeError(
                f"target {self.target_name!r}: the Infisical at {self._host} has no admin, "
                f"but {self._target_label()} holds {', '.join(known)}: the file belongs to "
                "an earlier instance; remove _publishers.infisical"
                f"{recorded} from it and publish again"
            )
        need_project = not self._get("project_id")
        need_tokens = [t for t in _IDENTITIES if not self._get(t[0])]
        if not need_project and not need_tokens:
            if not dry_run:
                self._mirror_project_id()
            return []

        what = (["the project"] if need_project else []) + [
            f"the {name!r} identity's token" for _k, name, _r in need_tokens
        ]
        created: list[str] = []
        admin_token = state.get("admin_token")
        if not admin_token:
            if initialized:
                tv = self.target_values
                if tv is None or not tv.path.is_file():
                    raise RuntimeError(
                        f"target {self.target_name!r}: the Infisical at {self._host} already "
                        f"has an admin and {self._target_label()} does not exist, so km holds "
                        "no credential for it; restore the file from the credentials repo, "
                        "or start from an empty instance"
                    )
                raise RuntimeError(
                    f"target {self.target_name!r}: {self._target_label()} lacks "
                    f"{' and '.join(what)} and has no _publishers.infisical.admin_token "
                    f"to create them on {self._host}"
                )
            missing = [
                k
                for k, v in (("admin_email", self._admin_email), ("org_name", self._org_name))
                if not v
            ]
            if missing:
                raise RuntimeError(
                    f"target {self.target_name!r}: the Infisical at {self._host} has no admin; "
                    f"bootstrapping it needs config.{' and config.'.join(missing)} on the "
                    "infisical destination"
                )
            label = f"instance admin {self._admin_email} (organisation {self._org_name!r})"
            if dry_run:
                self._planned = need_project
                sys.stderr.write(f"  [dry-run] would bootstrap {label}\n")
                created.append(f"bootstrap {label}")
                return created + self._plan_rest(need_project, need_tokens)
            # The password is recorded before the instance takes it, so a
            # failure to record the token afterwards still leaves km a way in.
            password = _mint_admin_password()
            self._set({"admin_password": password})
            with self._admin(None) as anon:
                boot = anon.bootstrap(
                    email=self._admin_email,  # type: ignore[arg-type]
                    password=password,
                    organization=self._org_name,  # type: ignore[arg-type]
                )
            self._set({"admin_token": boot.token, "org_id": boot.org_id})
            sys.stderr.write(f"  bootstrapped {label}\n")
            created.append(f"bootstrap {label}")
            admin_token = boot.token

        with self._admin(admin_token) as admin:
            if need_project:
                if not self._project_name:
                    raise RuntimeError(
                        f"target {self.target_name!r}: creating the project needs "
                        "config.project_name on the infisical destination"
                    )
                if dry_run:
                    self._planned = True
                    return created + self._plan_rest(True, need_tokens)
                project_id = admin.create_project(self._project_name)
                try:
                    self._set({"project_id": project_id})
                except Exception as err:
                    raise RuntimeError(
                        f"target {self.target_name!r}: created project {self._project_name!r} "
                        f"({project_id}) on {self._host} but could not record it in "
                        f"{self._target_label()}: {err}; set _publishers.infisical.project_id "
                        f"to {project_id} there before publishing again"
                    ) from err
                sys.stderr.write(f"  created project {self._project_name!r} ({project_id})\n")
                created.append(f"project {self._project_name}")
            project_id = state["project_id"]
            if not dry_run:
                self._mirror_project_id()
            if not need_tokens:
                return created
            org_id = state.get("org_id")
            if not org_id:
                raise RuntimeError(
                    f"target {self.target_name!r}: {self._target_label()} has no "
                    "_publishers.infisical.org_id to create identities in"
                )
            existing = {i.name: i for i in admin.list_identities(org_id)}
            roles = admin.project_identity_roles(project_id)
            for key, ident_name, role in need_tokens:
                ident = existing.get(ident_name)
                if dry_run:
                    # Without km's token the project cannot be read yet.
                    self._planned = self._planned or key == "token"
                    if ident is None:
                        created.append(f"identity {ident_name}")
                    created.append(f"identity {ident_name} token")
                    sys.stderr.write(
                        f"  [dry-run] would create a token for identity {ident_name!r}\n"
                    )
                    continue
                if ident is None:
                    ident = Identity(
                        id=admin.create_identity(ident_name, org_id),
                        name=ident_name,
                        auth_methods=[],
                    )
                    created.append(f"identity {ident_name}")
                if "token-auth" not in ident.auth_methods:
                    admin.attach_token_auth(ident.id)
                    created.append(f"identity {ident_name} token auth")
                if role not in roles.get(ident.id, []):
                    admin.add_identity_to_project(project_id, ident.id, role)
                    created.append(f"identity {ident_name} project role {role}")
                token = admin.create_token(ident.id, f"km-{self.target_name or 'target'}")
                self._set({key: token})
                sys.stderr.write(f"  created identity {ident_name!r} ({role}) and its token\n")
                created.append(f"identity {ident_name} token")
        return created

    def _mirror_project_id(self) -> None:
        """Copy the project id to its ``values:`` overlay when it differs."""
        if "project_id" not in self._values or self.target_values is None:
            return
        slug, secret = self._values["project_id"]
        project_id = self._state().get("project_id")
        if (self.target_values.manifests.get(slug) or {}).get(secret) != project_id:
            self._set({"project_id": project_id})

    def _plan_rest(
        self, need_project: bool, need_tokens: Iterable[tuple[str, str, str]]
    ) -> list[str]:
        """Dry-run labels for what a fresh project would need."""
        out: list[str] = []
        if need_project:
            sys.stderr.write(f"  [dry-run] would create project {self._project_name!r}\n")
            out.append(f"project {self._project_name}")
        for _key, ident_name, role in need_tokens:
            sys.stderr.write(
                f"  [dry-run] would create identity {ident_name!r} ({role}) and its token\n"
            )
            out.append(f"identity {ident_name} token")
        return out

    def _ensure_environment(self, env: str, dry_run: bool) -> list[str]:
        if self._planned:
            if env in _DEFAULT_ENVS:
                return []
            sys.stderr.write(f"  [dry-run] would create environment {env!r}\n")
            return [f"environment {env}"]
        project_id, token = self._credentials()
        with self._admin(token) as admin:
            if env in admin.list_environments(project_id):
                return []
            if dry_run:
                # Infisical cannot list the folders of an env it lacks.
                self._planned_envs.add(env)
                sys.stderr.write(f"  [dry-run] would create environment {env!r}\n")
            else:
                admin.create_environment(project_id, env)
                sys.stderr.write(f"  created environment {env!r}\n")
        return [f"environment {env}"]

    def _ensure_folders(
        self, env: str, manifests: list[ManifestRef], dry_run: bool
    ) -> EnsureResult:
        needed: set[str] = set()
        for ref in manifests:
            env_map = ref.section.get(env) if isinstance(ref.section, Mapping) else None
            if not isinstance(env_map, Mapping):
                continue
            for _local, full_path in _resolve_addresses(env_map, ref.secrets.keys()):
                _name, folder = _split_full_path(full_path)
                # Every ancestor too, so creation goes top-down.
                parts = [p for p in folder.split("/") if p]
                for i in range(1, len(parts) + 1):
                    needed.add("/" + "/".join(parts[:i]))
        if not needed:
            return EnsureResult()
        client = None if self._planned or env in self._planned_envs else self._client_for(env)
        existing_by_parent: dict[str, set[str]] = {}
        created: list[str] = []
        failed = 0
        for folder in sorted(needed, key=lambda f: (f.count("/"), f)):
            parent, _, name = folder.rpartition("/")
            parent = parent or "/"
            if parent not in existing_by_parent:
                if client is None or any(c == parent for c in created):
                    existing_by_parent[parent] = set()  # just created (or would be): empty
                else:
                    existing_by_parent[parent] = {f["name"] for f in client.list_folders(parent)}
            if name in existing_by_parent[parent]:
                continue
            label = f"env={env} folder {folder}"
            if dry_run:
                sys.stderr.write(f"  [dry-run] would create {label}\n")
                created.append(folder)
                continue
            try:
                client.create_folder(folder)
                sys.stderr.write(f"  created {label}\n")
                created.append(folder)
            except Exception as err:  # noqa: BLE001
                sys.stderr.write(f"  ERROR creating {label}: {err}\n")
                failed += 1
        return EnsureResult(created=tuple(f"env={env} folder {f}" for f in created), failed=failed)

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()

    # --- validate ---

    def validate(
        self,
        *,
        manifest_path: Path,
        section: Mapping[str, Any],
        secrets: Mapping[str, Any] | None = None,
    ) -> list[str]:
        loc = str(manifest_path)
        errors: list[str] = []
        if not isinstance(section, Mapping):
            return [f"{loc}: `publishers.infisical` must be a mapping"]
        for key, value in section.items():
            if isinstance(key, str) and key.startswith("_"):
                # Reserved meta keys (none defined today). Silently skip.
                continue
            if not isinstance(key, str) or not key:
                errors.append(
                    f"{loc}: publishers.infisical key must be a non-empty env name (got {key!r})"
                )
                continue
            env_name = key
            if not isinstance(value, Mapping):
                errors.append(
                    f"{loc}: publishers.infisical[{env_name!r}] must be a mapping of "
                    f"local_name → infisical_path (got {type(value).__name__}). "
                    "The supported shape is env-parent — list every secret "
                    "under every env it should exist in, repeating as needed."
                )
                continue
            for local_name, ipath in value.items():
                if not isinstance(local_name, str) or not local_name:
                    errors.append(
                        f"{loc}: publishers.infisical[{env_name!r}] key must be a "
                        f"non-empty local secret name (got {local_name!r})"
                    )
                    continue
                if not isinstance(ipath, str) or not ipath.startswith("/"):
                    errors.append(
                        f"{loc}: publishers.infisical[{env_name!r}][{local_name!r}]: "
                        f"path must be a string starting with '/' (got {ipath!r})"
                    )
        return errors

    # --- publish ---

    def publish(
        self,
        *,
        manifest_path: Path,
        env: str,
        secrets: Mapping[str, str],
        section: Mapping[str, Any],
        dry_run: bool,
        names: Collection[str] | None = None,
    ) -> PublishResult:
        env_map = section.get(env)
        if not isinstance(env_map, Mapping):
            return PublishResult(written=0, skipped=0, failed=0)

        ok = 0
        skipped = 0
        fail = 0
        manifest_label = manifest_path.name

        for local_name, full_path in _resolve_addresses(env_map, secrets.keys()):
            if names is not None and local_name not in names:
                continue
            if local_name not in secrets:
                sys.stderr.write(
                    f"  {manifest_label}: infisical entry for '{local_name}' "
                    "has no matching secret; skipped\n"
                )
                skipped += 1
                continue
            value = secrets[local_name]
            if value is None or value == "":
                sys.stderr.write(f"  {manifest_label}: secret '{local_name}' is empty; skipped\n")
                skipped += 1
                continue
            secret_name, folder = _split_full_path(full_path)
            if dry_run:
                sys.stderr.write(
                    f"  [dry-run] would write env={env} path={folder} name={secret_name}\n"
                )
                ok += 1
                continue
            try:
                self._client_for(env).set(secret_name, str(value), folder)
                ok += 1
            except Exception as err:
                sys.stderr.write(f"  ERROR writing {folder}/{secret_name} (env={env}): {err}\n")
                fail += 1

        return PublishResult(written=ok, skipped=skipped, failed=fail)

    # --- unpublish ---

    def unpublish(
        self,
        *,
        manifest_path: Path,
        section: Mapping[str, Any],
        names: list[str],
        envs: list[str] | None = None,
        dry_run: bool = False,
    ) -> PublishResult:
        deleted = 0
        failed = 0
        name_set = set(names)
        if not name_set:
            return PublishResult(written=0, skipped=0, failed=0)
        for env_name, env_map in section.items():
            if not isinstance(env_name, str) or env_name.startswith("_"):
                continue
            if envs is not None and env_name not in envs:
                continue
            if not isinstance(env_map, Mapping):
                continue
            for local_name, full_path in _resolve_addresses(env_map, name_set):
                if local_name not in name_set:
                    continue
                secret_name, folder = _split_full_path(full_path)
                if dry_run:
                    sys.stderr.write(
                        f"  [dry-run] would delete env={env_name} path={folder} name={secret_name}\n"
                    )
                    deleted += 1
                    continue
                try:
                    self._client_for(env_name).delete(secret_name, folder)
                    deleted += 1
                except Exception as err:
                    sys.stderr.write(
                        f"  ERROR deleting {folder}/{secret_name} (env={env_name}): {err}\n"
                    )
                    failed += 1
        return PublishResult(written=deleted, skipped=0, failed=failed)

    # --- collect ---

    def collect(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
    ) -> CollectResult:
        client = self._client_for(env)
        runtime_infos = client.list("/", recursive=True)
        runtime: dict[tuple[str, str], str] = {}
        for info in runtime_infos:
            key = _coerce_runtime_key(info.path, info.name)
            runtime[key] = info.value

        entries: list[CollectedEntry] = []
        claimed: set[tuple[str, str]] = set()

        for ref in manifests:
            env_map = ref.section.get(env)
            if not isinstance(env_map, Mapping):
                continue
            for local_name, full_path in _resolve_addresses(env_map, ref.secrets.keys()):
                secret_name, folder = _split_full_path(full_path)
                key = (folder, secret_name)
                claimed.add(key)
                manifest_value = ref.secrets.get(local_name)
                manifest_value_str = None if manifest_value is None else str(manifest_value)
                if key in runtime:
                    runtime_value = runtime[key]
                    if manifest_value_str is None or manifest_value_str == "":
                        entries.append(
                            CollectedEntry(
                                state="same",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=full_path,
                                value=runtime_value,
                            )
                        )
                    elif manifest_value_str == runtime_value:
                        entries.append(
                            CollectedEntry(
                                state="same",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=full_path,
                                value=runtime_value,
                            )
                        )
                    else:
                        entries.append(
                            CollectedEntry(
                                state="differs",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=full_path,
                                value=runtime_value,
                                manifest_value=manifest_value_str,
                            )
                        )
                else:
                    entries.append(
                        CollectedEntry(
                            state="manifest_only",
                            manifest_path=ref.path,
                            local_name=local_name,
                            store_address=full_path,
                            value=manifest_value_str or "",
                        )
                    )

        for key, runtime_value in sorted(runtime.items()):
            if key in claimed:
                continue
            folder, secret_name = key
            store_address = f"{folder}/{secret_name}" if folder != "/" else f"/{secret_name}"
            entries.append(
                CollectedEntry(
                    state="store_only",
                    manifest_path=None,
                    local_name=None,
                    store_address=store_address,
                    value=runtime_value,
                )
            )

        return CollectResult(entries=entries)


__all__ = ["InfisicalPublisher"]
