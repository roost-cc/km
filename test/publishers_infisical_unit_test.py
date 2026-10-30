"""Unit tests for InfisicalPublisher (ensure / publish / collect / validate).

Uses an in-memory adapter — no network. The adapter implements the
:class:`InfisicalClient` surface used by the publisher (``list``,
``set``, ``delete``, ``close``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from km import targets as targets_mod
from km.infisical import Bootstrap, Identity, SecretInfo, SecretMetadata
from km.publishers import CollectResult, ManifestRef
from km.publishers.infisical import InfisicalPublisher


class _FakeAdapter:
    """In-memory Infisical client. One instance per env."""

    def __init__(self, env: str) -> None:
        self.env = env
        # store[(folder, name)] = value
        self.store: dict[tuple[str, str], str] = {}

    def list(self, path: str, *, recursive: bool = False) -> list[SecretInfo]:
        return [
            SecretInfo(
                name=name,
                path=folder,
                value=value,
                metadata=SecretMetadata(updated_at="", comment=""),
            )
            for (folder, name), value in self.store.items()
        ]

    def set(self, name: str, value: str, path: str, *, comment: str | None = None) -> None:
        self.store[(path, name)] = value

    def get(self, name, path):
        return None

    def delete(self, name, path):
        self.store.pop((path, name), None)

    def close(self) -> None:
        pass


_BASE_CONFIG = {"host": "http://x"}


def _pub_with_clients() -> tuple[InfisicalPublisher, dict[str, _FakeAdapter]]:
    """Return a publisher whose `_client_for(env)` returns a per-env fake."""
    pub = InfisicalPublisher(_BASE_CONFIG)
    adapters: dict[str, _FakeAdapter] = {}

    def _client_for(env: str) -> _FakeAdapter:
        if env not in adapters:
            adapters[env] = _FakeAdapter(env)
        return adapters[env]

    pub._client_for = _client_for  # type: ignore[method-assign]
    return pub, adapters


@pytest.fixture
def pub_and_adapters():
    return _pub_with_clients()


# --- construction ---


def test_constructor_requires_host() -> None:
    with pytest.raises(ValueError, match="host"):
        InfisicalPublisher({})


def test_constructor_rejects_token_and_workspace_in_config() -> None:
    """km's token and project id are per-target values, not configuration."""
    with pytest.raises(ValueError, match="targets/<target>.yaml"):
        InfisicalPublisher({"host": "h", "token": "t"})
    with pytest.raises(ValueError, match="workspace_id"):
        InfisicalPublisher({"host": "h", "workspace_id": "ws"})


def test_credentials_come_from_the_target_state() -> None:
    pub = InfisicalPublisher(_BASE_CONFIG)
    pub.target_name = "local"
    with pytest.raises(RuntimeError, match="targets/local.yaml"):
        pub._client_for("dev")
    pub.target_state = {"project_id": "ws", "token": "t"}
    client = pub._client_for("dev")
    assert (client.project_id, client.token, client.environment) == ("ws", "t", "dev")
    pub.close()


# --- validate ---


def test_validate_accepts_well_formed_section() -> None:
    pub = InfisicalPublisher(_BASE_CONFIG)
    section: Mapping = {
        "dev": {"client_id": "/oauth/google/client_id"},
        "prod": {"client_id": "/oauth/google/client_id"},
    }
    assert pub.validate(manifest_path=Path("x.yaml"), section=section) == []


def test_validate_rejects_non_mapping_section() -> None:
    pub = InfisicalPublisher(_BASE_CONFIG)
    errs = pub.validate(manifest_path=Path("x.yaml"), section=[])  # type: ignore[arg-type]
    assert any("must be a mapping" in e for e in errs)


def test_validate_rejects_relative_path() -> None:
    pub = InfisicalPublisher(_BASE_CONFIG)
    errs = pub.validate(
        manifest_path=Path("x.yaml"),
        section={"dev": {"k": "relative"}},
    )
    assert any("must be a string starting with '/'" in e for e in errs)


def test_validate_silently_ignores_underscore_meta_keys() -> None:
    """Reserved (`_`-prefixed) keys are stripped silently — no schema error."""
    pub = InfisicalPublisher(_BASE_CONFIG)
    section: Mapping = {
        "dev": {"k": "/jwt/account/private_key"},
        "_meta": "anything",
    }
    assert pub.validate(manifest_path=Path("x.yaml"), section=section) == []


# --- publish ---


def test_publish_writes_each_declared_secret(pub_and_adapters) -> None:
    pub, adapters = pub_and_adapters
    result = pub.publish(
        manifest_path=Path("g.yaml"),
        env="dev",
        secrets={"client_id": "CID", "client_secret": "SEC"},
        section={
            "dev": {
                "client_id": "/oauth/g/client_id",
                "client_secret": "/oauth/g/client_secret",
            }
        },
        dry_run=False,
    )
    assert result.written == 2
    assert result.skipped == 0
    assert result.failed == 0
    assert adapters["dev"].store == {
        ("/oauth/g", "client_id"): "CID",
        ("/oauth/g", "client_secret"): "SEC",
    }


def test_publish_writes_only_the_selected_names(pub_and_adapters) -> None:
    pub, adapters = pub_and_adapters
    result = pub.publish(
        manifest_path=Path("g.yaml"),
        env="dev",
        secrets={"client_id": "CID", "client_secret": "SEC", "jwt_a": "A", "jwt_b": "B"},
        section={
            "dev": {
                "client_id": "/oauth/g/client_id",
                "client_secret": "/oauth/g/client_secret",
                "jwt_*": "/jwt",
            }
        },
        dry_run=False,
        names={"client_secret", "jwt_b"},
    )
    assert (result.written, result.skipped) == (2, 0)
    assert adapters["dev"].store == {
        ("/oauth/g", "client_secret"): "SEC",
        ("/jwt", "jwt_b"): "B",
    }


def test_publish_dry_run_does_not_write(pub_and_adapters) -> None:
    pub, adapters = pub_and_adapters
    result = pub.publish(
        manifest_path=Path("g.yaml"),
        env="dev",
        secrets={"k": "v"},
        section={"dev": {"k": "/x/k"}},
        dry_run=True,
    )
    assert result.written == 1  # counted as "would have written"
    assert "dev" not in adapters or adapters["dev"].store == {}


def test_publish_skips_missing_or_empty_secrets(pub_and_adapters) -> None:
    pub, _ = pub_and_adapters
    result = pub.publish(
        manifest_path=Path("g.yaml"),
        env="dev",
        secrets={"k": ""},  # empty
        section={"dev": {"k": "/x/k", "missing": "/x/missing"}},
        dry_run=False,
    )
    assert result.written == 0
    assert result.skipped == 2  # one empty, one not in secrets


def test_publish_no_section_for_env(pub_and_adapters) -> None:
    """If the section has no entry for the requested env, publish is a no-op."""
    pub, _ = pub_and_adapters
    result = pub.publish(
        manifest_path=Path("g.yaml"),
        env="prod",
        secrets={"k": "v"},
        section={"dev": {"k": "/x/k"}},
        dry_run=False,
    )
    assert result.written == 0
    assert result.skipped == 0


# --- collect ---


def _ref(path: Path, secrets: dict[str, str], section: dict) -> ManifestRef:
    return ManifestRef(path=path, secrets=secrets, section=section)


def test_collect_reports_same_when_values_match(pub_and_adapters) -> None:
    pub, adapters = pub_and_adapters
    pub._client_for("dev").store[("/oauth/g", "client_id")] = "CID"

    refs = [
        _ref(
            Path("g.yaml"),
            secrets={"client_id": "CID"},
            section={"dev": {"client_id": "/oauth/g/client_id"}},
        )
    ]
    result = pub.collect(env="dev", manifests=refs)
    assert len(result.entries) == 1
    assert result.entries[0].state == "same"


def test_collect_reports_differs_when_values_diverge(pub_and_adapters) -> None:
    pub, _ = pub_and_adapters
    pub._client_for("dev").store[("/oauth/g", "client_id")] = "STORE_VALUE"

    refs = [
        _ref(
            Path("g.yaml"),
            secrets={"client_id": "YAML_VALUE"},
            section={"dev": {"client_id": "/oauth/g/client_id"}},
        )
    ]
    result = pub.collect(env="dev", manifests=refs)
    assert [e.state for e in result.entries] == ["differs"]
    diff = result.entries[0]
    assert diff.value == "STORE_VALUE"
    assert diff.manifest_value == "YAML_VALUE"


def test_collect_reports_manifest_only(pub_and_adapters) -> None:
    pub, _ = pub_and_adapters
    refs = [
        _ref(
            Path("g.yaml"),
            secrets={"client_id": "CID"},
            section={"dev": {"client_id": "/oauth/g/client_id"}},
        )
    ]
    result = pub.collect(env="dev", manifests=refs)
    assert [e.state for e in result.entries] == ["manifest_only"]


def test_collect_reports_store_only(pub_and_adapters) -> None:
    pub, _ = pub_and_adapters
    pub._client_for("dev").store[("/strays", "x")] = "v"

    refs: list[ManifestRef] = []
    result = pub.collect(env="dev", manifests=refs)
    assert [e.state for e in result.entries] == ["store_only"]
    assert result.entries[0].store_address == "/strays/x"


def test_collect_with_null_manifest_value_is_same(pub_and_adapters) -> None:
    """A system-style manifest declares the address but `secrets:` is
    empty (rotation producer owns the value). Treat as `same` when the
    address exists in the store."""
    pub, _ = pub_and_adapters
    pub._client_for("dev").store[("/jwt/account", "private_key")] = "REAL"

    refs = [
        _ref(
            Path("jwt.yaml"),
            secrets={"account_private_key": None},  # type: ignore[dict-item]
            section={"dev": {"account_private_key": "/jwt/account/private_key"}},
        )
    ]
    result = pub.collect(env="dev", manifests=refs)
    assert [e.state for e in result.entries] == ["same"]


# --- ensure ---


class _FolderAdapter(_FakeAdapter):
    def __init__(self, env: str, folders: set[str]) -> None:
        super().__init__(env)
        self.folders = set(folders)
        self.created: list[str] = []

    def list_folders(self, path: str) -> list[dict[str, str]]:
        prefix = "" if path == "/" else path
        return [
            {"id": f, "name": f.rsplit("/", 1)[1]}
            for f in sorted(self.folders)
            if f.rsplit("/", 1)[0] == prefix
        ]

    def create_folder(self, path: str) -> None:
        self.created.append(path)
        self.folders.add(path)


class _FakeAdmin:
    """In-memory instance: the :class:`InfisicalAdmin` surface ensure uses."""

    def __init__(self, *, initialized: bool = False) -> None:
        self.initialized = initialized
        self.projects: dict[str, set[str]] = {}
        self.identities: dict[str, Identity] = {}
        self.roles: dict[tuple[str, str], list[str]] = {}
        self.tokens: dict[str, str] = {}  # token → identity id
        self.calls: list[str] = []  # mutating calls, in order

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def is_initialized(self) -> bool:
        return self.initialized

    def bootstrap(self, *, email: str, password: str, organization: str) -> Bootstrap:
        assert not self.initialized
        assert len(password) >= 14
        self.initialized = True
        self.calls.append(f"bootstrap {email} {organization}")
        self.tokens["admin-tok"] = "admin-id"
        return Bootstrap(org_id="org-1", token="admin-tok")

    def create_project(self, name: str) -> str:
        pid = f"proj-{len(self.projects) + 1}"
        self.projects[pid] = {"dev", "staging", "prod"}
        self.calls.append(f"project {name}")
        return pid

    def list_environments(self, project_id: str) -> list[str]:
        return sorted(self.projects[project_id])

    def create_environment(self, project_id: str, slug: str) -> None:
        self.projects[project_id].add(slug)
        self.calls.append(f"env {slug}")

    def list_identities(self, org_id: str) -> list[Identity]:
        return list(self.identities.values())

    def create_identity(self, name: str, org_id: str) -> str:
        iid = f"id-{name}"
        self.identities[iid] = Identity(id=iid, name=name, auth_methods=[])
        self.calls.append(f"identity {name}")
        return iid

    def attach_token_auth(self, identity_id: str) -> None:
        self.identities[identity_id].auth_methods.append("token-auth")
        self.calls.append(f"token-auth {identity_id}")

    def create_token(self, identity_id: str, name: str) -> str:
        tok = f"tok-{identity_id}-{len(self.tokens)}"
        self.tokens[tok] = identity_id
        self.calls.append(f"token {identity_id}")
        return tok

    def project_identity_roles(self, project_id: str) -> dict[str, list[str]]:
        return {i: r for (p, i), r in self.roles.items() if p == project_id}

    def add_identity_to_project(self, project_id: str, identity_id: str, role: str) -> None:
        self.roles[(project_id, identity_id)] = [role]
        self.calls.append(f"member {identity_id} {role}")


_ENSURE_CONFIG = {
    "host": "http://x",
    "admin_email": "admin@example.com",
    "org_name": "Roost",
    "project_name": "roost",
    "values": {
        "project_id": "infisical:secret_manager_project",
        "services_token": "infisical:secret_manager_token",
    },
}

_REFS = [
    ManifestRef(
        path=Path("m.yaml"),
        secrets={"k": "v"},
        section={"dev": {"k": "/jwt/account/k"}, "test": {"k": "/jwt/account/k"}},
    )
]


def _ensure_pub(
    target_file: Path,
    admin: _FakeAdmin,
    folders: dict[str, _FolderAdapter] | None = None,
    config: Mapping | None = None,
) -> InfisicalPublisher:
    """A publisher for target ``t`` over a fake instance, its state read
    from ``target_file`` the way the loader does."""
    pub = InfisicalPublisher(config or _ENSURE_CONFIG)
    tv = targets_mod.load(target_file.parent.parent, target_file.parent.name, "t")
    pub.target_name = "t"
    pub.target_values = tv
    pub.target_state = tv.publisher_state("infisical")
    pub.target_file = tv.path
    pub._admin = lambda token: admin  # type: ignore[method-assign]
    adapters = folders if folders is not None else {}

    def _client_for(env: str) -> _FolderAdapter:
        pub._credentials()  # the real client needs km's credentials too
        return adapters.setdefault(env, _FolderAdapter(env, set()))

    pub._client_for = _client_for  # type: ignore[method-assign]
    return pub


def test_ensure_from_an_empty_instance_creates_everything(tmp_path: Path) -> None:
    target_file = tmp_path / "targets" / "t.yaml"
    admin = _FakeAdmin()
    pub = _ensure_pub(target_file, admin)
    dev = pub.ensure(env="dev", manifests=_REFS, dry_run=False)
    test = pub.ensure(env="test", manifests=_REFS, dry_run=False)

    assert admin.calls == [
        "bootstrap admin@example.com Roost",
        "project roost",
        "identity km",
        "token-auth id-km",
        "member id-km admin",
        "token id-km",
        "identity services",
        "token-auth id-services",
        "member id-services viewer",
        "token id-services",
        "env test",  # dev comes with every new project
    ]
    assert admin.projects["proj-1"] >= {"dev", "test"}
    assert dev.failed == 0 and test.failed == 0
    assert "env=dev folder /jwt/account" in dev.created
    assert "environment test" in test.created
    assert set(dev.values) == {
        ("infisical", "secret_manager_project"),
        ("infisical", "secret_manager_token"),
    }
    assert test.values == ()

    on_disk = yaml.safe_load(target_file.read_text())
    state = on_disk["_publishers"]["infisical"]
    assert state["project_id"] == "proj-1"
    assert admin.tokens[state["token"]] == "id-km"
    assert admin.tokens[state["admin_token"]] == "admin-id"
    assert state["org_id"] == "org-1" and len(state["admin_password"]) == 32
    assert "services_token" not in state  # it lives where `values:` puts it
    assert on_disk["infisical"]["secret_manager_project"] == "proj-1"
    assert admin.tokens[on_disk["infisical"]["secret_manager_token"]] == "id-services"


@pytest.mark.parametrize(
    ("fails_on", "instance_calls", "kept", "message"),
    [
        # Recording before the instance changes: nothing is created.
        ("admin_password", [], set(), "disk full"),
        # After bootstrap: the password km recorded first still opens it.
        ("admin_token", ["bootstrap admin@example.com Roost"], {"admin_password"}, "disk full"),
        # After the project: the error names its id.
        (
            "project_id",
            ["bootstrap admin@example.com Roost", "project roost"],
            {"admin_password", "admin_token", "org_id"},
            "proj-1",
        ),
    ],
)
def test_ensure_keeps_a_way_back_when_recording_fails_after_the_instance_changed(
    tmp_path: Path, monkeypatch, fails_on, instance_calls, kept, message
) -> None:
    real_update = targets_mod.update

    def update(values, *, manifests=None, publishers=None):
        if fails_on in ((publishers or {}).get("infisical") or {}):
            raise OSError("disk full")
        return real_update(values, manifests=manifests, publishers=publishers)

    monkeypatch.setattr(targets_mod, "update", update)
    target_file = tmp_path / "targets" / "t.yaml"
    admin = _FakeAdmin()
    pub = _ensure_pub(target_file, admin)
    with pytest.raises(Exception, match=message):
        pub.ensure(env="dev", manifests=_REFS, dry_run=False)
    assert admin.calls == instance_calls
    on_disk = yaml.safe_load(target_file.read_text()) if target_file.exists() else {}
    assert set((on_disk.get("_publishers") or {}).get("infisical") or {}) == kept


@pytest.mark.parametrize(
    "fails_on",
    ["admin_password", "admin_token", "project_id", "token", "secret_manager_token"],
)
def test_ensure_retry_after_a_failed_record_uses_only_recorded_credentials(
    tmp_path: Path, monkeypatch, fails_on
) -> None:
    """A credential km failed to record is not in the run's values either,
    so the next ensure of the run never goes on with it."""
    real_update = targets_mod.update
    failures = [fails_on]

    def replace(_values):
        raise OSError("disk full")

    def update(values, *, manifests=None, publishers=None):
        keys = set((publishers or {}).get("infisical") or {})
        keys |= {k for body in (manifests or {}).values() for k in body}
        if failures and failures[0] in keys:
            failures.pop()
            # The file write itself fails, inside the real update.
            with monkeypatch.context() as m:
                m.setattr(targets_mod, "_replace", replace)
                return real_update(values, manifests=manifests, publishers=publishers)
        return real_update(values, manifests=manifests, publishers=publishers)

    monkeypatch.setattr(targets_mod, "update", update)
    target_file = tmp_path / "targets" / "t.yaml"
    admin = _FakeAdmin()
    pub = _ensure_pub(target_file, admin)
    tv = pub.target_values

    def on_disk() -> tuple[dict, dict]:
        disk = targets_mod.load(tmp_path, "targets", "t")
        return disk.manifests, disk.publishers

    def in_memory() -> tuple[dict, dict]:
        drop = {k: v for k, v in tv.publishers.items() if v}  # _state() may add an empty one
        return {k: v for k, v in tv.manifests.items() if v}, drop

    with pytest.raises(Exception):  # noqa: B017
        pub.ensure(env="dev", manifests=_REFS, dry_run=False)
    assert in_memory() == on_disk()

    try:
        pub.ensure(env="dev", manifests=_REFS, dry_run=False)
    except RuntimeError as err:
        # Without the admin token on record, km cannot reach the instance.
        assert fails_on == "admin_token" and "admin_token" in str(err)
    assert in_memory() == on_disk()
    state = on_disk()[1].get("infisical") or {}
    if state.get("token"):
        assert admin.tokens[state["token"]] == "id-km"


def test_concurrent_ensure_of_one_instance_creates_and_records_everything_once(
    tmp_path: Path,
) -> None:
    """Two runs ensure one empty instance at once, each inside
    `targets.ensuring` as `km publish` does: one bootstraps and creates;
    the other finds it all recorded and uses the same credentials."""
    import contextlib
    import threading

    both_checked = threading.Barrier(2)

    class _Admin(_FakeAdmin):
        def is_initialized(self) -> bool:
            with contextlib.suppress(threading.BrokenBarrierError):
                both_checked.wait(timeout=0.5)
            return super().is_initialized()

    target_file = tmp_path / "targets" / "t.yaml"
    admin = _Admin()
    pubs = [_ensure_pub(target_file, admin) for _ in range(2)]
    errors: list[BaseException] = []

    def run(pub: InfisicalPublisher) -> None:
        try:
            with targets_mod.ensuring(pub.target_values):
                pub.ensure(env="dev", manifests=_REFS, dry_run=False)
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=run, args=(p,)) for p in pubs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert [c for c in admin.calls if c.startswith(("bootstrap", "project", "identity"))] == [
        "bootstrap admin@example.com Roost",
        "project roost",
        "identity km",
        "identity services",
    ]
    assert len(admin.tokens) == 3  # admin, km, services
    on_disk = targets_mod.load(tmp_path, "targets", "t")
    for pub in pubs:
        assert pub.target_values.publishers == on_disk.publishers
        assert pub.target_values.manifests == on_disk.manifests


def test_a_second_ensure_creates_nothing_and_leaves_the_file_byte_identical(
    tmp_path: Path,
) -> None:
    target_file = tmp_path / "targets" / "t.yaml"
    admin = _FakeAdmin()
    folders: dict[str, _FolderAdapter] = {}
    first = _ensure_pub(target_file, admin, folders)
    for env in ("dev", "test"):
        first.ensure(env=env, manifests=_REFS, dry_run=False)
    before = target_file.read_bytes()
    calls = list(admin.calls)

    again = _ensure_pub(target_file, admin, folders)
    results = [again.ensure(env=env, manifests=_REFS, dry_run=False) for env in ("dev", "test")]

    assert [r.created for r in results] == [(), ()]
    assert [r.values for r in results] == [(), ()]
    assert admin.calls == calls
    assert target_file.read_bytes() == before


def test_ensure_half_done_creates_only_the_missing_identity(tmp_path: Path) -> None:
    """Project and km's identity exist; the services identity was never made."""
    target_file = tmp_path / "targets" / "t.yaml"
    target_file.parent.mkdir()
    admin = _FakeAdmin(initialized=True)
    admin.projects["proj-1"] = {"dev", "test"}
    admin.identities["id-km"] = Identity(id="id-km", name="km", auth_methods=["token-auth"])
    admin.roles[("proj-1", "id-km")] = ["admin"]
    target_file.write_text(
        yaml.safe_dump(
            {
                "_publishers": {
                    "infisical": {
                        "project_id": "proj-1",
                        "token": "km-tok",
                        "admin_token": "admin-tok",
                        "org_id": "org-1",
                    }
                }
            }
        )
    )
    folders = {"dev": _FolderAdapter("dev", {"/jwt", "/jwt/account"})}
    pub = _ensure_pub(target_file, admin, folders)
    result = pub.ensure(env="dev", manifests=_REFS, dry_run=False)

    assert admin.calls == [
        "identity services",
        "token-auth id-services",
        "member id-services viewer",
        "token id-services",
    ]
    on_disk = yaml.safe_load(target_file.read_text())
    assert on_disk["_publishers"]["infisical"]["token"] == "km-tok"
    assert on_disk["infisical"] == {
        "secret_manager_project": "proj-1",
        "secret_manager_token": "tok-id-services-0",
    }
    assert ("infisical", "secret_manager_token") in result.values


def test_ensure_reuses_an_identity_whose_token_was_lost(tmp_path: Path) -> None:
    target_file = tmp_path / "targets" / "t.yaml"
    target_file.parent.mkdir()
    admin = _FakeAdmin(initialized=True)
    admin.projects["proj-1"] = {"dev"}
    for name, role in (("km", "admin"), ("services", "viewer")):
        admin.identities[f"id-{name}"] = Identity(
            id=f"id-{name}", name=name, auth_methods=["token-auth"]
        )
        admin.roles[("proj-1", f"id-{name}")] = [role]
    target_file.write_text(
        "_publishers:\n  infisical: {project_id: proj-1, token: km-tok, admin_token: a, org_id: o}\n"
    )
    pub = _ensure_pub(target_file, admin, config={"host": "http://x"})
    pub.ensure(env="dev", manifests=[], dry_run=False)
    assert admin.calls == ["token id-services"]
    state = yaml.safe_load(target_file.read_text())["_publishers"]["infisical"]
    assert state["services_token"] == "tok-id-services-0"


def test_ensure_on_a_bootstrapped_instance_without_a_target_file_fails(tmp_path: Path) -> None:
    target_file = tmp_path / "targets" / "t.yaml"
    admin = _FakeAdmin(initialized=True)
    pub = _ensure_pub(target_file, admin)
    with pytest.raises(RuntimeError) as err:
        pub.ensure(env="dev", manifests=_REFS, dry_run=False)
    assert "'t'" in str(err.value) and str(target_file) in str(err.value)
    assert "does not exist" in str(err.value)
    assert admin.calls == []
    assert not target_file.exists()


def test_ensure_on_an_empty_instance_with_an_old_target_file_fails(tmp_path: Path) -> None:
    """The instance was rebuilt: the file's tokens belong to the old one."""
    target_file = tmp_path / "targets" / "t.yaml"
    target_file.parent.mkdir()
    target_file.write_text(
        "_publishers:\n  infisical: {project_id: proj-1, token: km-tok, services_token: s}\n"
    )
    before = target_file.read_bytes()
    admin = _FakeAdmin()
    pub = _ensure_pub(target_file, admin, config={"host": "http://x"})
    with pytest.raises(RuntimeError, match="belongs to an earlier instance"):
        pub.ensure(env="dev", manifests=_REFS, dry_run=False)
    assert admin.calls == [] and target_file.read_bytes() == before


def test_ensure_without_bootstrap_config_names_the_missing_keys(tmp_path: Path) -> None:
    pub = _ensure_pub(tmp_path / "targets" / "t.yaml", _FakeAdmin(), config={"host": "http://x"})
    with pytest.raises(RuntimeError, match="config.admin_email and config.org_name"):
        pub.ensure(env="dev", manifests=_REFS, dry_run=False)


def test_ensure_dry_run_on_an_empty_instance_creates_and_writes_nothing(tmp_path: Path) -> None:
    target_file = tmp_path / "targets" / "t.yaml"
    admin = _FakeAdmin()
    pub = _ensure_pub(target_file, admin)
    dev = pub.ensure(env="dev", manifests=_REFS, dry_run=True)
    test = pub.ensure(env="test", manifests=_REFS, dry_run=True)
    assert admin.calls == [] and not admin.initialized
    assert not target_file.exists()
    assert dev.created[0].startswith("bootstrap instance admin admin@example.com")
    assert "project roost" in dev.created
    assert "env=dev folder /jwt/account" in dev.created
    assert "environment test" in test.created
    assert dev.values == ()


def test_ensure_creates_missing_environments_and_folders_top_down(tmp_path: Path) -> None:
    """A complete target file: no instance-level calls, only env and folders."""
    target_file = tmp_path / "targets" / "t.yaml"
    target_file.parent.mkdir()
    target_file.write_text(
        "_publishers:\n  infisical: {project_id: proj-1, token: km-tok, services_token: s}\n"
    )
    admin = _FakeAdmin(initialized=True)
    admin.projects["proj-1"] = {"dev"}
    adapter = _FolderAdapter("dev", {"/jwt"})
    pub = _ensure_pub(target_file, admin, {"dev": adapter, "test": adapter}, {"host": "http://x"})
    refs = [
        ManifestRef(
            path=Path("m.yaml"),
            secrets={"k": "v", "pub_a": "x"},
            section={
                "dev": {"k": "/jwt/account/private_key", "pub_*": "/oauth/google/"},
                "test": {"k": "/only/test/k"},
            },
        )
    ]
    result = pub.ensure(env="dev", manifests=refs, dry_run=False)
    assert adapter.created == ["/oauth", "/jwt/account", "/oauth/google"]
    assert result.failed == 0 and len(result.created) == 3
    assert admin.calls == []
    again = pub.ensure(env="dev", manifests=refs, dry_run=False)
    assert again.created == () and adapter.created == ["/oauth", "/jwt/account", "/oauth/google"]

    dry = pub.ensure(env="test", manifests=refs, dry_run=True)
    assert dry.created == (
        "environment test",
        "env=test folder /only",
        "env=test folder /only/test",
    )
    assert admin.calls == []
    pub.ensure(env="test", manifests=refs, dry_run=False)
    assert admin.calls == ["env test"]


def test_ensure_dry_run_of_a_new_environment_does_not_read_its_folders(tmp_path: Path) -> None:
    """Infisical answers 404 for folders of an env the project lacks: a dry
    run that would create the env plans every folder instead of listing."""
    target_file = tmp_path / "targets" / "t.yaml"
    target_file.parent.mkdir()
    target_file.write_text(
        "_publishers:\n  infisical: {project_id: proj-1, token: km-tok, services_token: s}\n"
    )
    admin = _FakeAdmin(initialized=True)
    admin.projects["proj-1"] = {"dev"}

    class _NoEnv(_FolderAdapter):
        def list_folders(self, path: str) -> list[dict[str, str]]:
            raise RuntimeError("Failed to list folders: 404 environment not found")

    pub = _ensure_pub(target_file, admin, {"test": _NoEnv("test", set())}, {"host": "http://x"})
    dry = pub.ensure(env="test", manifests=_REFS, dry_run=True)
    assert dry.failed == 0
    assert dry.created == (
        "environment test",
        "env=test folder /jwt",
        "env=test folder /jwt/account",
    )
    assert admin.calls == []


def test_ensure_with_root_paths_only_creates_no_folders(tmp_path: Path) -> None:
    target_file = tmp_path / "targets" / "t.yaml"
    target_file.parent.mkdir()
    target_file.write_text(
        "_publishers:\n  infisical: {project_id: proj-1, token: km-tok, services_token: s}\n"
    )
    admin = _FakeAdmin(initialized=True)
    admin.projects["proj-1"] = {"dev"}
    pub = _ensure_pub(target_file, admin, config={"host": "http://x"})
    refs = [ManifestRef(path=Path("m.yaml"), secrets={"k": "v"}, section={"dev": {"k": "/k"}})]
    assert pub.ensure(env="dev", manifests=refs, dry_run=False).created == ()


def test_provides_and_values_config() -> None:
    assert InfisicalPublisher(_ENSURE_CONFIG).provides() == {
        ("infisical", "secret_manager_project"),
        ("infisical", "secret_manager_token"),
    }
    assert InfisicalPublisher({"host": "h"}).provides() == set()
    with pytest.raises(ValueError, match="<manifest slug>:<secret>"):
        InfisicalPublisher({"host": "h", "values": {"project_id": "nocolon"}})
    with pytest.raises(ValueError, match="unknown key"):
        InfisicalPublisher({"host": "h", "values": {"admin_token": "a:b"}})


@pytest.mark.parametrize(
    "publishers, rejected",
    [
        (["infisical", "infisical"], [1]),
        (["infisical", "template_file", "infisical"], [2]),
        (["template_file", "template_file"], []),  # keeps no target state
        (["infisical"], []),
    ],
)
def test_duplicate_stateful_destinations_have_independent_target_values_or_are_rejected(
    tmp_path: Path, monkeypatch, publishers: list[str], rejected: list[int]
) -> None:
    import argparse

    from km.commands import _publish, _validate
    from km.config import load as load_config
    from km.project import load_project
    from km.publishers._discovery import build_destinations

    lines = ["project: demo", "environments: [dev]", "targets:", "  t:"]
    lines += ["    manifests: [m]", "    destinations:"]
    for i, name in enumerate(publishers):
        host = f", host: 'http://host-{i}'" if name == "infisical" else ", root: ./out"
        lines.append(f"      - {{publisher: {name}, envs: [dev]{host}}}")
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")
    (tmp_path / "manifests").mkdir()
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "r.md").write_text("# r\n")
    (tmp_path / "manifests" / "m.yaml").write_text(
        "service: m\nsecrets: {a: A}\nrotation:\n  runbook: r.md\n  groups:\n"
        "    g: {max_age_days: 0, producer: static, secrets: [{name: a}]}\n"
    )
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "t.yaml").write_text(
        "_publishers:\n  infisical: {project_id: project-on-0, token: token-from-0}\n"
    )
    # The first destination reads back clean: only the rejection turns validate red.
    monkeypatch.setattr(InfisicalPublisher, "collect", lambda self, **kw: CollectResult([]))
    cfg = load_config(tmp_path / "config.yaml")
    target = targets_mod.load(tmp_path, "targets", "t")

    loaded = build_destinations(cfg, load_project(tmp_path), target)
    assert [i for i, ld in enumerate(loaded) if ld.publisher is None] == rejected
    for i in rejected:
        assert "may list it once" in (loaded[i].error or "")

    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        target="t",
        envs=["dev"],
        all=True,
        only=None,
        execute=True,
        json=True,
        schema_only=False,
        verbose=False,
    )
    assert _validate.run(args) == (2 if rejected else 0)  # 2: a destination failed to load
    if rejected:
        assert _publish.run(args) == 2
