"""PostgreSQL publisher — sets database user passwords via ``ALTER USER``.

For each ``(env, local_name, pg_user)`` declared in a manifest's
``publishers.postgresql:`` section, the publisher connects with the
env's admin credentials and executes::

    ALTER USER "<pg_user>" WITH PASSWORD '<secret_value>';

via ``psycopg.sql.Identifier`` / ``psycopg.sql.Literal`` so identifiers
and values are escaped by the driver — no string interpolation into
SQL.

Manifest section shape (env-parent)::

    publishers:
      postgresql:
        dev:
          synapse_password: synapse           # local_name: pg_user
          infisical_password: infisical
          assistant_password: assistant

Destination configuration (env-keyed admin connection params)::

    targets:
      local:
        - publisher: postgresql
          envs: [dev]
          dev:
            host: localhost
            port: 5432
            database: postgres        # admin connection target
            user: postgres            # admin role (needs CREATEROLE)
            password: postgres        # optional (peer auth = omit)
          test:
            host: localhost
            database: postgres
            login_file: $HOME/secrets/km/postgresql-admin.yaml

``login_file`` replaces ``user`` and ``password``: a yaml file holding
``user:`` and ``password:``, read when km first connects, so the admin
login sits outside the project (rendered there by ``km publish km``).

``ensure()`` creates a named role that is missing (``CREATE ROLE …
WITH LOGIN``); ``publish()`` then sets its password. Dry-run prints the
SQL it would execute, with the password redacted.

``collect()`` verifies publication by connecting *as the rotated user*
with the manifest value. Auth success → ``same``; password-auth
failure → ``differs``; role missing in ``pg_roles`` → ``manifest_only``.
The test-auth uses the admin's ``database:`` as the connection target.

``unpublish()`` is the base-class no-op — postgres user accounts aren't
a name-keyed store km prunes; lifecycle of users themselves is the
operator's job.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from ruamel.yaml import YAML, YAMLError

from km.publishers import (
    CollectedEntry,
    CollectResult,
    EnsureResult,
    ManifestRef,
    Publisher,
    PublishResult,
)


def _env_address(env: str, pg_user: str) -> str:
    """Stable address string for audit / output rows."""
    return f"postgres://{env}/{pg_user}"


def _read_login_file(env: str, path: str) -> tuple[str, str | None]:
    """The ``(user, password)`` a ``login_file`` holds; RuntimeError
    naming the file unless it is a mapping with a non-empty ``user``."""
    try:
        data = YAML(typ="safe").load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise RuntimeError(
            f"postgresql publisher: env={env} login_file {path} does not exist "
            f"(render it first: km publish km)"
        ) from err
    except (OSError, YAMLError) as err:
        raise RuntimeError(f"postgresql publisher: env={env} login_file {path}: {err}") from err
    user = data.get("user") if isinstance(data, Mapping) else None
    password = data.get("password") if isinstance(data, Mapping) else None
    if (
        not isinstance(user, str)
        or not user
        or (password is not None and not isinstance(password, str))
    ):
        raise RuntimeError(
            f"postgresql publisher: env={env} login_file {path} must be a mapping with a "
            f"non-empty string `user:` and a string `password:`"
        )
    return user, password


class PostgresqlPublisher(Publisher):
    name = "postgresql"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        if not isinstance(config, Mapping) or not config:
            raise ValueError(
                "postgresql publisher: config must be a non-empty env-keyed "
                "mapping of {host, port, database, user, password}"
            )
        env_cfgs: dict[str, dict[str, Any]] = {}
        for env_name, params in config.items():
            if not isinstance(env_name, str) or not env_name:
                raise ValueError(
                    f"postgresql publisher: env name must be a non-empty string (got {env_name!r})"
                )
            if not isinstance(params, Mapping):
                raise ValueError(f"postgresql publisher: config[{env_name!r}] must be a mapping")
            login_file = params.get("login_file")
            if login_file is not None:
                if not isinstance(login_file, str) or not login_file.startswith("/"):
                    raise ValueError(
                        f"postgresql publisher: config[{env_name!r}].login_file must be "
                        f"absolute after $VAR expansion, or ./-relative to the config dir "
                        f"(got {login_file!r})"
                    )
                if "user" in params or "password" in params:
                    raise ValueError(
                        f"postgresql publisher: config[{env_name!r}] sets login_file "
                        f"and user/password; the login comes from one of them"
                    )
            for key in ("host", "database") if login_file else ("host", "database", "user"):
                v = params.get(key)
                if not isinstance(v, str) or not v:
                    raise ValueError(
                        f"postgresql publisher: config[{env_name!r}].{key} "
                        f"is required (non-empty string)"
                    )
            port = params.get("port", 5432)
            if not isinstance(port, int) or isinstance(port, bool) or port < 1:
                raise ValueError(
                    f"postgresql publisher: config[{env_name!r}].port "
                    f"must be a positive int (got {port!r})"
                )
            password = params.get("password")
            if password is not None and not isinstance(password, str):
                raise ValueError(
                    f"postgresql publisher: config[{env_name!r}].password must be a string when set"
                )
            env_cfgs[env_name] = {
                "host": params["host"],
                "port": port,
                "database": params["database"],
                "user": params.get("user"),
                "password": password,
                "login_file": login_file,
            }
        self._env_cfgs = env_cfgs
        self._admin_conns: dict[str, psycopg.Connection] = {}

    # --- connection lifecycle ---

    def _admin_connect(self, env: str) -> psycopg.Connection:
        if env in self._admin_conns:
            return self._admin_conns[env]
        if env not in self._env_cfgs:
            raise RuntimeError(
                f"postgresql publisher: no config for env={env!r} (have: {sorted(self._env_cfgs)})"
            )
        params = self._env_cfgs[env]
        user, password = params["user"], params["password"]
        if params["login_file"]:
            user, password = _read_login_file(env, params["login_file"])
        kwargs: dict[str, Any] = {
            "host": params["host"],
            "port": params["port"],
            "dbname": params["database"],
            "user": user,
        }
        if password:
            kwargs["password"] = password
        conn = psycopg.connect(**kwargs)
        conn.autocommit = True  # ALTER USER and pg_roles SELECT don't need a tx
        self._admin_conns[env] = conn
        return conn

    def close(self) -> None:
        for c in self._admin_conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._admin_conns.clear()

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
            return [f"{loc}: `publishers.postgresql` must be a mapping"]
        for key, value in section.items():
            if isinstance(key, str) and key.startswith("_"):
                continue  # reserved meta keys (none today)
            if not isinstance(key, str) or not key:
                errors.append(
                    f"{loc}: publishers.postgresql key must be a non-empty env name (got {key!r})"
                )
                continue
            env_name = key
            if not isinstance(value, Mapping):
                errors.append(
                    f"{loc}: publishers.postgresql[{env_name!r}] must be a "
                    f"mapping of local_name → postgres_username"
                )
                continue
            for local_name, pg_user in value.items():
                if not isinstance(local_name, str) or not local_name:
                    errors.append(
                        f"{loc}: publishers.postgresql[{env_name!r}] key must "
                        f"be a non-empty local secret name (got {local_name!r})"
                    )
                    continue
                if not isinstance(pg_user, str) or not pg_user:
                    errors.append(
                        f"{loc}: publishers.postgresql[{env_name!r}][{local_name!r}]: "
                        f"postgres username must be a non-empty string "
                        f"(got {pg_user!r})"
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
        """Create every role the manifests name for ``env`` that is missing,
        ``WITH LOGIN`` and no password (publish sets it next)."""
        users: list[str] = []
        for ref in manifests:
            env_map = ref.section.get(env) if isinstance(ref.section, Mapping) else None
            if not isinstance(env_map, Mapping):
                continue
            for local_name, pg_user in env_map.items():
                if (
                    isinstance(local_name, str)
                    and isinstance(pg_user, str)
                    and pg_user not in users
                ):
                    users.append(pg_user)
        if not users:
            return EnsureResult()
        admin = self._admin_connect(env)
        created: list[str] = []
        failed = 0
        for pg_user in users:
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (pg_user,))
                if cur.fetchone() is not None:
                    continue
            label = f"env={env} role {pg_user!r}"
            if dry_run:
                sys.stderr.write(
                    f'  [dry-run] would CREATE ROLE "{pg_user}" WITH LOGIN (env={env})\n'
                )
                created.append(label)
                continue
            try:
                with admin.cursor() as cur:
                    cur.execute(
                        sql.SQL("CREATE ROLE {user} WITH LOGIN").format(
                            user=sql.Identifier(pg_user)
                        )
                    )
                sys.stderr.write(f"  created {label}\n")
                created.append(label)
            except Exception as err:  # noqa: BLE001
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

        for local_name, pg_user in env_map.items():
            if not isinstance(local_name, str) or not isinstance(pg_user, str):
                continue
            if names is not None and local_name not in names:
                continue
            if local_name not in secrets:
                sys.stderr.write(
                    f"  {manifest_label}: postgresql entry for {local_name!r} "
                    "has no matching secret; skipped\n"
                )
                skipped += 1
                continue
            value = secrets[local_name]
            if value is None or value == "":
                sys.stderr.write(f"  {manifest_label}: secret {local_name!r} is empty; skipped\n")
                skipped += 1
                continue
            value_str = str(value)

            if dry_run:
                sys.stderr.write(
                    f"  [dry-run] env={env} secrets[{local_name!r}] -> "
                    f'ALTER USER "{pg_user}" WITH PASSWORD <secret>;\n'
                )
                ok += 1
                continue

            try:
                conn = self._admin_connect(env)
                stmt = sql.SQL("ALTER USER {user} WITH PASSWORD {pwd}").format(
                    user=sql.Identifier(pg_user),
                    pwd=sql.Literal(value_str),
                )
                with conn.cursor() as cur:
                    cur.execute(stmt)
                ok += 1
            except Exception as err:  # noqa: BLE001
                sys.stderr.write(f'  ERROR postgresql ALTER USER "{pg_user}" (env={env}): {err}\n')
                fail += 1

        return PublishResult(written=ok, skipped=skipped, failed=fail)

    # --- collect ---

    def collect(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
    ) -> CollectResult:
        if env not in self._env_cfgs:
            return CollectResult(entries=[])

        # Filter to manifests that actually declare this env on this publisher.
        relevant = [ref for ref in manifests if isinstance(ref.section.get(env), Mapping)]
        if not relevant:
            return CollectResult(entries=[])

        admin = self._admin_connect(env)
        params = self._env_cfgs[env]

        entries: list[CollectedEntry] = []
        for ref in relevant:
            env_map = ref.section.get(env)
            if not isinstance(env_map, Mapping):
                continue
            for local_name, pg_user in env_map.items():
                if not isinstance(local_name, str) or not isinstance(pg_user, str):
                    continue
                address = _env_address(env, pg_user)
                manifest_value = ref.secrets.get(local_name)
                manifest_value_str = "" if manifest_value is None else str(manifest_value)

                # Step 1: does the role exist?
                with admin.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (pg_user,))
                    exists = cur.fetchone() is not None
                if not exists:
                    entries.append(
                        CollectedEntry(
                            state="manifest_only",
                            manifest_path=ref.path,
                            local_name=local_name,
                            store_address=address,
                            value=manifest_value_str,
                        )
                    )
                    continue

                # Step 2: test-auth as the role using the manifest value.
                if not manifest_value_str:
                    # No value to test with — declare known but unverifiable.
                    entries.append(
                        CollectedEntry(
                            state="manifest_only",
                            manifest_path=ref.path,
                            local_name=local_name,
                            store_address=address,
                            value=manifest_value_str,
                        )
                    )
                    continue

                test_kwargs = {
                    "host": params["host"],
                    "port": params["port"],
                    "dbname": params["database"],
                    "user": pg_user,
                    "password": manifest_value_str,
                }
                try:
                    test_conn = psycopg.connect(**test_kwargs)
                    test_conn.close()
                    entries.append(
                        CollectedEntry(
                            state="same",
                            manifest_path=ref.path,
                            local_name=local_name,
                            store_address=address,
                            value=manifest_value_str,
                        )
                    )
                except psycopg.OperationalError as err:
                    msg = str(err).lower()
                    if "password authentication failed" in msg or "28p01" in msg:
                        entries.append(
                            CollectedEntry(
                                state="differs",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=address,
                                value="(unknown — postgres can't read passwords)",
                                manifest_value=manifest_value_str,
                            )
                        )
                    else:
                        # Network / db-access / config error: bubble up so
                        # validate surfaces it as a reach error.
                        raise

        return CollectResult(entries=entries)


__all__ = ["PostgresqlPublisher"]
