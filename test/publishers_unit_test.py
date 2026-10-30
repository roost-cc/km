"""Protocol-level tests for the publisher seam.

A trivial in-memory FakePublisher exercises every km command path
(`_publish.run`, `_validate.run`) via monkeypatched destinations —
proving the commands are publisher-agnostic and a second backing store
can be plugged in without touching command code.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from km import targets as targets_mod
from km.commands import _publish, _validate
from km.publishers import (
    CollectedEntry,
    CollectResult,
    EnsureResult,
    ManifestRef,
    Publisher,
    PublishResult,
)
from km.publishers._discovery import LoadedDestination


class _FakePublisher(Publisher):
    """Minimal Publisher implementation backed by an in-memory dict."""

    name = "fake"

    def __init__(self) -> None:
        # Skip Publisher.__init__'s config arg — instances are
        # constructed by the test, not by km.loader.
        # store[(env, address)] = value
        self.store: dict[tuple[str, str], str] = {}
        self.publish_calls: list[tuple[str, str, dict]] = []
        # Every ensure/publish/apply call, in order: (label:step, env, dry_run).
        self.calls: list[tuple[str, ...]] = []
        self.label = "fake"

    def validate(
        self,
        *,
        manifest_path: Path,
        section: Mapping[str, Any],
        secrets: Mapping[str, Any] | None = None,
    ) -> list[str]:
        if not isinstance(section, Mapping):
            return [f"{manifest_path.name}: section must be a mapping"]
        return []

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
        self.calls.append((f"{self.label}:publish", env, dry_run))
        env_map = section.get(env)
        if not isinstance(env_map, Mapping):
            return PublishResult(written=0, skipped=0, failed=0)
        ok = 0
        for local_name, address in env_map.items():
            if local_name not in secrets:
                continue
            if names is not None and local_name not in names:
                continue
            self.publish_calls.append(
                (env, address, {"value": secrets[local_name], "dry_run": dry_run})
            )
            if not dry_run:
                self.store[(env, address)] = str(secrets[local_name])
            ok += 1
        return PublishResult(written=ok, skipped=0, failed=0)

    def collect(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
    ) -> CollectResult:
        entries: list[CollectedEntry] = []
        claimed_addresses: set[str] = set()
        for ref in manifests:
            env_map = ref.section.get(env)
            if not isinstance(env_map, Mapping):
                continue
            for local_name, address in env_map.items():
                claimed_addresses.add(address)
                key = (env, address)
                manifest_value = ref.secrets.get(local_name)
                manifest_str = None if manifest_value is None else str(manifest_value)
                if key in self.store:
                    runtime = self.store[key]
                    if manifest_str is None or manifest_str == runtime:
                        entries.append(
                            CollectedEntry(
                                state="same",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=address,
                                value=runtime,
                            )
                        )
                    else:
                        entries.append(
                            CollectedEntry(
                                state="differs",
                                manifest_path=ref.path,
                                local_name=local_name,
                                store_address=address,
                                value=runtime,
                                manifest_value=manifest_str,
                            )
                        )
                else:
                    entries.append(
                        CollectedEntry(
                            state="manifest_only",
                            manifest_path=ref.path,
                            local_name=local_name,
                            store_address=address,
                            value=manifest_str or "",
                        )
                    )
        for (e, address), value in self.store.items():
            if e != env:
                continue
            if address in claimed_addresses:
                continue
            entries.append(
                CollectedEntry(
                    state="store_only",
                    manifest_path=None,
                    local_name=None,
                    store_address=address,
                    value=value,
                )
            )
        return CollectResult(entries=entries)

    def declared_envs(self, section: Any) -> set[str]:
        if not isinstance(section, Mapping):
            return set()
        return {k for k, v in section.items() if isinstance(k, str) and isinstance(v, Mapping)}

    def ensure(
        self,
        *,
        env: str,
        manifests: list[ManifestRef],
        dry_run: bool,
    ) -> EnsureResult:
        self.calls.append((f"{self.label}:ensure", env, dry_run))
        return EnsureResult()

    def apply(self, *, env: str, dry_run: bool) -> None:
        self.calls.append((f"{self.label}:apply", env, dry_run))


CONFIG = """environments: [dev, test]
targets:
  t:
    manifests: SLUGS
    destinations:
      - publisher: fake
        envs: [dev, test]
"""


def _make_project(tmp_path: Path, config: str = CONFIG, slugs: tuple[str, ...] = ("g",)) -> Path:
    """Create a project directory with config.yaml and an empty manifests/ dir;
    target ``t`` lists ``slugs``."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "config.yaml").write_text(config.replace("SLUGS", f"[{', '.join(slugs)}]"))
    (proj / "manifests").mkdir()
    (proj / "runbooks").mkdir()
    (proj / "runbooks" / "r.md").write_text("# stub\n")
    return proj


def _write_manifest(proj: Path, name: str, body: str) -> Path:
    p = proj / "manifests" / name
    p.write_text(body)
    return p


def _patch_destinations(monkeypatch, module, *fakes: _FakePublisher) -> None:
    """Route the target's destinations, in order, to the given fakes."""

    def fake_build(cfg, project, target):
        return [
            LoadedDestination(destination=d, publisher=fakes[i])
            for i, d in enumerate(cfg.target(target.name).destinations)
        ]

    monkeypatch.setattr(module, "build_destinations", fake_build)


def _publish_args(
    proj: Path,
    *envs: str,
    execute: bool = True,
    target: str = "t",
    only: list[str] | None = None,
):
    return argparse.Namespace(
        config=str(proj / "config.yaml"),
        target=target,
        envs=list(envs),
        all=only is None,
        only=only,
        execute=execute,
    )


def _validate_args(proj: Path, *envs: str, schema_only: bool = False):
    return argparse.Namespace(
        config=str(proj / "config.yaml"),
        target="t",
        envs=list(envs),
        json=True,
        schema_only=schema_only,
        verbose=False,
    )


G_MANIFEST = """service: g
secrets:
  client_id: CID
publishers:
  fake:
    dev:
      client_id: /oauth/g/client_id
    test:
      client_id: /oauth/g/client_id
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      producer: interactive
      secrets:
        - name: client_id
"""


# --- protocol-level dataclass smoke tests ---


def test_collected_entry_is_frozen() -> None:
    e = CollectedEntry(
        state="same",
        manifest_path=Path("x.yaml"),
        local_name="k",
        store_address="/a/k",
        value="v",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.state = "differs"  # type: ignore[misc]


def test_publish_result_carries_counts() -> None:
    r = PublishResult(written=1, skipped=2, failed=3)
    assert (r.written, r.skipped, r.failed) == (1, 2, 3)


def test_collect_result_holds_entries_only() -> None:
    """`CollectResult` is a single-field carrier — no `suppressed` after S-432."""
    r = CollectResult(entries=[])
    assert r.entries == []
    assert not hasattr(r, "suppressed")


def test_base_ensure_and_apply_do_nothing() -> None:
    class _Bare(Publisher):
        name = "bare"

        def validate(self, **_kw):
            return []

        def publish(self, **_kw):
            return PublishResult(0, 0, 0)

        def collect(self, **_kw):
            return CollectResult(entries=[])

    bare = _Bare({})
    assert bare.ensure(env="dev", manifests=[], dry_run=False) == EnsureResult()
    assert bare.apply(env="dev", dry_run=False) is None


# --- _publish.run with a fake publisher ---


def test_publish_drives_fake_publisher_end_to_end(tmp_path, monkeypatch):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)

    rc = _publish.run(_publish_args(proj, "dev"))
    assert rc == 0
    assert fake.publish_calls == [("dev", "/oauth/g/client_id", {"value": "CID", "dry_run": False})]
    assert fake.store == {("dev", "/oauth/g/client_id"): "CID"}


def test_publish_runs_ensure_publish_apply_per_destination_in_order(tmp_path, monkeypatch):
    first, second = _FakePublisher(), _FakePublisher()
    _patch_destinations(monkeypatch, _publish, first, second)
    proj = _make_project(
        tmp_path,
        """environments: [dev, test]
targets:
  t:
    manifests: SLUGS
    destinations:
      - publisher: fake
        envs: [dev]
      - publisher: fake
        envs: [dev, test]
""",
    )
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    log: list[tuple[str, ...]] = []
    first.calls = log
    second.calls = log
    first.label, second.label = "first", "second"

    rc = _publish.run(_publish_args(proj))  # no envs: every env the target accepts
    assert rc == 0
    assert log == [
        ("first:ensure", "dev", False),
        ("first:publish", "dev", False),
        ("first:apply", "dev", False),
        ("second:ensure", "dev", False),
        ("second:publish", "dev", False),
        ("second:apply", "dev", False),
        ("second:ensure", "test", False),
        ("second:publish", "test", False),
        ("second:apply", "test", False),
    ]


def test_a_publish_that_raises_fails_its_destination_and_the_rest_still_run(
    tmp_path, monkeypatch, capsys
):
    class _Raising(_FakePublisher):
        def publish(self, **kw):
            self.calls.append((f"{self.label}:publish", kw["env"], kw["dry_run"]))
            raise KeyError("envv")

    first, second = _Raising(), _FakePublisher()
    _patch_destinations(monkeypatch, _publish, first, second)
    proj = _make_project(
        tmp_path,
        """environments: [dev, test]
targets:
  t:
    manifests: SLUGS
    destinations:
      - publisher: fake
        envs: [dev]
      - publisher: fake
        envs: [dev]
""",
    )
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    log: list[tuple[str, ...]] = []
    first.calls = log
    second.calls = log
    first.label, second.label = "first", "second"

    assert _publish.run(_publish_args(proj, "dev")) == 1
    assert log == [
        ("first:ensure", "dev", False),
        ("first:publish", "dev", False),
        ("second:ensure", "dev", False),
        ("second:publish", "dev", False),
        ("second:apply", "dev", False),
    ]
    assert "ERROR publish failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "outcomes, applied",
    [
        # manifest file → what its publish does
        ({"g.yaml": PublishResult(1, 0, 1)}, False),  # mixed success and failure
        ({"g.yaml": PublishResult(1, 0, 0), "h.yaml": KeyError("x")}, False),  # later raises
        ({"g.yaml": PublishResult(1, 0, 0), "h.yaml": PublishResult(0, 0, 1)}, False),
        ({"g.yaml": PublishResult(0, 0, 1), "h.yaml": PublishResult(1, 0, 0)}, False),
        ({"g.yaml": PublishResult(1, 0, 0), "h.yaml": PublishResult(1, 0, 0)}, True),
    ],
)
def test_apply_is_skipped_when_any_publish_for_the_destination_env_fails(
    tmp_path, monkeypatch, capsys, outcomes, applied
):
    class _Scripted(_FakePublisher):
        def publish(self, **kw):
            outcome = outcomes[kw["manifest_path"].name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    fake = _Scripted()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=tuple(Path(n).stem for n in outcomes))
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    if "h.yaml" in outcomes:
        _write_manifest(proj, "h.yaml", G_MANIFEST.replace("service: g", "service: h"))

    assert _publish.run(_publish_args(proj, "dev")) == (0 if applied else 1)
    assert (("fake:apply", "dev", False) in fake.calls) is applied
    if not applied:
        assert "apply skipped" in capsys.readouterr().err


class _ValueMintingFake(_FakePublisher):
    """Stands in for Infisical: its ensure records a per-target value
    (as a fresh project's services token) the first time it runs."""

    def provides(self) -> set[tuple[str, str]]:
        return {("c", "tok")}

    def ensure(self, *, env, manifests, dry_run):
        super().ensure(env=env, manifests=manifests, dry_run=dry_run)
        if dry_run or (self.target_values.manifests.get("c") or {}).get("tok"):
            return EnsureResult()
        targets_mod.update(self.target_values, manifests={"c": {"tok": "minted-by-ensure"}})
        return EnsureResult(created=("identity",), values=(("c", "tok"),))


C_MANIFEST = """service: c
secrets:
  other: O
  tok: ''
publishers:
  fake:
    dev:
      tok: /c/tok
      other: /c/other
rotation:
  runbook: r.md
  groups:
    clients:
      max_age_days: 0
      producer: static
      secrets:
        - name: tok
"""


def test_publish_republishes_earlier_destinations_with_values_ensure_recorded(
    tmp_path, monkeypatch
):
    renderer, store = _FakePublisher(), _ValueMintingFake()
    log: list[tuple[str, ...]] = []
    renderer.calls = store.calls = log
    renderer.label, store.label = "files", "store"

    def fake_build(cfg, project, target):
        store.target_values = target
        return [
            LoadedDestination(destination=d, publisher=p)
            for d, p in zip(cfg.target(target.name).destinations, (renderer, store), strict=True)
        ]

    monkeypatch.setattr(_publish, "build_destinations", fake_build)
    proj = _make_project(
        tmp_path,
        """environments: [dev]
targets:
  t:
    manifests: SLUGS
    destinations:
      - publisher: fake
        envs: [dev]
      - publisher: fake
        envs: [dev]
""",
        slugs=("c",),
    )
    _write_manifest(proj, "c.yaml", C_MANIFEST)

    # The empty static `tok` is not a mint error: the store's ensure provides it.
    assert _publish.run(_publish_args(proj)) == 0
    assert log == [
        ("files:ensure", "dev", False),
        ("files:publish", "dev", False),
        ("files:apply", "dev", False),
        ("store:ensure", "dev", False),
        ("store:publish", "dev", False),
        ("store:apply", "dev", False),
        # Only the destination before the store, only the new name.
        ("files:publish", "dev", False),
        ("files:apply", "dev", False),
    ]
    assert renderer.publish_calls[-1] == (
        "dev",
        "/c/tok",
        {"value": "minted-by-ensure", "dry_run": False},
    )
    assert store.store[("dev", "/c/tok")] == "minted-by-ensure"

    # Second run: the value comes from the target file, nothing is re-published.
    log.clear()
    assert _publish.run(_publish_args(proj)) == 0
    assert [c[0] for c in log].count("files:publish") == 1


def test_concurrent_ensure_creates_and_records_each_external_resource_once(tmp_path, monkeypatch):
    """Two `km publish` runs on one target, each with its own stale view of
    the target file: their ensures take turns, so the second finds what
    the first created and recorded instead of creating another."""
    import contextlib
    import threading

    instance: list[str] = []  # what the external store holds
    both_checked = threading.Barrier(2)

    class _CheckThenCreate(_FakePublisher):
        def ensure(self, *, env, manifests, dry_run):
            if (self.target_values.manifests.get("c") or {}).get("tok"):
                return EnsureResult()
            # Without the lock the other run reaches this point too: wait for it.
            with contextlib.suppress(threading.BrokenBarrierError):
                both_checked.wait(timeout=0.5)
            tok = f"created-{len(instance) + 1}"
            instance.append(tok)
            targets_mod.update(self.target_values, manifests={"c": {"tok": tok}})
            return EnsureResult(created=("resource",), values=(("c", "tok"),))

        def provides(self):
            return {("c", "tok")}

    stores: list[_FakePublisher] = []

    def fake_build(cfg, project, target):
        store = _CheckThenCreate()
        store.target_values = target
        stores.append(store)
        return [
            LoadedDestination(destination=cfg.target(target.name).destinations[0], publisher=store)
        ]

    monkeypatch.setattr(_publish, "build_destinations", fake_build)
    proj = _make_project(tmp_path, slugs=("c",))
    _write_manifest(proj, "c.yaml", C_MANIFEST)
    codes: list[int] = []
    runs = [
        threading.Thread(target=lambda: codes.append(_publish.run(_publish_args(proj, "dev"))))
        for _ in range(2)
    ]
    for t in runs:
        t.start()
    for t in runs:
        t.join()

    assert codes == [0, 0]
    assert instance == ["created-1"]
    recorded = targets_mod.load(proj, "targets", "t").manifests["c"]["tok"]
    assert recorded == "created-1"
    # Each run published the one recorded value, the waiting one included.
    assert [s.store[("dev", "/c/tok")] for s in stores] == ["created-1", "created-1"]


def test_publish_dry_run_writes_nothing(tmp_path, monkeypatch):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)

    rc = _publish.run(_publish_args(proj, execute=False))
    assert rc == 0
    assert fake.store == {}
    assert {c[2]["dry_run"] for c in fake.publish_calls} == {True}


def test_publish_overlays_per_target_values(tmp_path, monkeypatch):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST.replace("client_id: CID", "client_id: ''"))
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text("g:\n  client_id: PER-TARGET\n")

    rc = _publish.run(_publish_args(proj, "dev"))
    assert rc == 0
    assert fake.store == {("dev", "/oauth/g/client_id"): "PER-TARGET"}
    # The overlay never lands in the manifest.
    assert "PER-TARGET" not in (proj / "manifests" / "g.yaml").read_text()


MINT_MANIFEST = """service: m
secrets:
  token: ''
  key: ''
publishers:
  fake:
    dev:
      token: /m/token
      key: /m/key
rotation:
  runbook: r.md
  groups:
    tokens:
      max_age_days: 30
      last_rotated: null
      producer: passphrase
      secrets:
        - name: token
    keys:
      max_age_days: 30
      last_rotated: null
      producer: ed25519
      secrets:
        - name: key
"""


def test_publish_mints_empty_generated_secrets_into_the_manifest(tmp_path, monkeypatch):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=("m",))
    path = _write_manifest(proj, "m.yaml", MINT_MANIFEST)

    rc = _publish.run(_publish_args(proj, "dev"))
    assert rc == 0
    data = yaml.safe_load(path.read_text())
    assert len(data["secrets"]["token"]) == 32
    assert data["secrets"]["key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert data["rotation"]["groups"]["tokens"]["last_rotated"] is not None
    assert fake.store[("dev", "/m/token")] == data["secrets"]["token"]
    assert fake.store[("dev", "/m/key")] == data["secrets"]["key"]


def test_publish_dry_run_mints_nothing_into_the_manifest(tmp_path, monkeypatch):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=("m",))
    path = _write_manifest(proj, "m.yaml", MINT_MANIFEST)

    rc = _publish.run(_publish_args(proj, "dev", execute=False))
    assert rc == 0
    assert path.read_text() == MINT_MANIFEST
    assert len(fake.publish_calls) == 2  # still lists what it would write


@pytest.mark.parametrize("producer", ["static", "interactive", "sendgrid"])
def test_publish_fails_on_empty_third_party_secret_before_any_write(
    tmp_path, monkeypatch, capsys, producer
):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=("g", "m", "v"))
    _write_manifest(proj, "g.yaml", G_MANIFEST)  # publishable, sorts first
    mint_path = _write_manifest(proj, "m.yaml", MINT_MANIFEST)
    _write_manifest(
        proj,
        "v.yaml",
        f"""service: v
secrets:
  api_key: ''
publishers:
  fake:
    dev:
      api_key: /v/api_key
rotation:
  runbook: r.md
  groups:
    v:
      max_age_days: 30
      producer: {producer}
      secrets:
        - name: api_key
""",
    )

    rc = _publish.run(_publish_args(proj, "dev"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "manifests/v.yaml: secret 'api_key' is empty" in err
    assert fake.calls == [] and fake.store == {}
    # Not even the generated secrets are minted.
    assert mint_path.read_text() == MINT_MANIFEST


def test_publish_does_not_mint_an_overlaid_name(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=("m",))
    path = _write_manifest(proj, "m.yaml", MINT_MANIFEST)
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text("m:\n  token: ''\n")

    rc = _publish.run(_publish_args(proj, "dev"))
    assert rc == 1
    assert "secret 'token' is empty in the target's values file" in capsys.readouterr().err
    assert path.read_text() == MINT_MANIFEST


def test_publish_ignores_manifests_the_target_does_not_publish(tmp_path, monkeypatch):
    """An empty secret only matters where the target would write it."""
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=("o",))
    _write_manifest(
        proj,
        "o.yaml",
        MINT_MANIFEST.replace("  fake:\n", "  other:\n").replace(
            "producer: passphrase", "producer: static"
        ),
    )
    assert _publish.run(_publish_args(proj, "dev")) == 0


# --- km publish --only: selection, minting and apply-only-when-touched (S-959) ---

TWO_DEST_CONFIG = """environments: [dev, test]
targets:
  t:
    manifests: SLUGS
    destinations:
      - publisher: fake
        envs: [dev]
      - publisher: other
        envs: [dev]
"""

# Two groups; `jwt` goes to the first destination only, `db` to both.
S_MANIFEST = """service: s
secrets:
  jwt_key: JWT
  db_password: DB
publishers:
  fake:
    dev:
      jwt_key: /s/jwt_key
      db_password: /s/db_password
  other:
    dev:
      db_password: /pg/s
rotation:
  runbook: r.md
  groups:
    jwt:
      max_age_days: 30
      producer: interactive
      secrets:
        - name: jwt_key
    db:
      max_age_days: 30
      producer: interactive
      secrets:
        - name: db_password
"""


def _two_destinations(tmp_path, monkeypatch):
    first, second = _FakePublisher(), _FakePublisher()
    first.label, second.label = "fake", "other"
    log: list[tuple[str, ...]] = []
    first.calls = second.calls = log
    _patch_destinations(monkeypatch, _publish, first, second)
    proj = _make_project(tmp_path, TWO_DEST_CONFIG, slugs=("s", "g"))
    _write_manifest(proj, "s.yaml", S_MANIFEST)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    return proj, first, second, log


def test_only_group_writes_its_addresses_and_applies_only_where_it_wrote(tmp_path, monkeypatch):
    proj, first, second, log = _two_destinations(tmp_path, monkeypatch)

    assert _publish.run(_publish_args(proj, "dev", only=["s:jwt"])) == 0
    assert first.store == {("dev", "/s/jwt_key"): "JWT"}
    assert second.store == {}
    # The second destination publishes `s` but none of the selection: no apply.
    assert ("fake:apply", "dev", False) in log
    assert ("other:apply", "dev", False) not in log
    assert log.count(("fake:apply", "dev", False)) == 1


def test_only_secret_name_and_bare_slug(tmp_path, monkeypatch):
    proj, first, second, log = _two_destinations(tmp_path, monkeypatch)

    assert _publish.run(_publish_args(proj, "dev", only=["s:db_password"])) == 0
    assert first.store == {("dev", "/s/db_password"): "DB"}
    assert second.store == {("dev", "/pg/s"): "DB"}
    assert {c[0] for c in log if c[0].endswith(":apply")} == {"fake:apply", "other:apply"}

    first.store.clear()
    second.store.clear()
    assert _publish.run(_publish_args(proj, "dev", only=["s"])) == 0
    assert set(first.store) == {("dev", "/s/jwt_key"), ("dev", "/s/db_password")}
    # `g` was not selected.
    assert ("dev", "/oauth/g/client_id") not in first.store


def test_only_repeats(tmp_path, monkeypatch):
    proj, first, _second, _log = _two_destinations(tmp_path, monkeypatch)

    assert _publish.run(_publish_args(proj, "dev", only=["s:jwt", "g"])) == 0
    assert set(first.store) == {("dev", "/s/jwt_key"), ("dev", "/oauth/g/client_id")}


def test_all_applies_every_destination_with_secrets(tmp_path, monkeypatch):
    proj, _first, _second, log = _two_destinations(tmp_path, monkeypatch)

    assert _publish.run(_publish_args(proj, "dev")) == 0
    assert [c[0] for c in log if c[0].endswith(":apply")] == ["fake:apply", "other:apply"]


def test_no_apply_when_nothing_was_written(tmp_path, monkeypatch):
    """A destination whose secrets are all skipped (empty) is not applied."""
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    fake.publish = lambda **_kw: PublishResult(written=0, skipped=1, failed=0)  # type: ignore[method-assign]

    assert _publish.run(_publish_args(proj, "dev")) == 0
    assert [c for c in fake.calls if c[0].endswith(":apply")] == []


def test_dry_run_lists_the_applies_that_would_run(tmp_path, monkeypatch, capsys):
    proj, first, second, log = _two_destinations(tmp_path, monkeypatch)

    assert _publish.run(_publish_args(proj, "dev", execute=False, only=["s:jwt"])) == 0
    err = capsys.readouterr().err
    assert "[dry-run] would apply [" in err
    assert err.count("would apply") == 1
    assert first.store == {} and second.store == {}
    assert ("fake:apply", "dev", True) in log


@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ("nope", "no manifest 'nope' (have: g, s)"),
        ("s:nope", "'nope' is neither a rotation group nor a secret of 's'"),
        ("s:", "expected <slug> or <slug>:<group-or-secret>"),
    ],
)
def test_unknown_selection_is_a_usage_error(tmp_path, monkeypatch, capsys, selector, message):
    proj, first, _second, log = _two_destinations(tmp_path, monkeypatch)

    assert _publish.run(_publish_args(proj, "dev", only=[selector])) == 2
    err = capsys.readouterr().err
    assert message in err
    if selector == "s:nope":
        assert "groups: db, jwt; secrets: db_password, jwt_key" in err
    assert log == [] and first.store == {}


def test_minting_considers_only_the_selection(tmp_path, monkeypatch):
    """`--only m:tokens` with an empty `v:api_key` (unmintable) and an empty
    `m:key` (mintable, unselected) neither fails nor mints them."""
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path, slugs=("m", "v"))
    path = _write_manifest(proj, "m.yaml", MINT_MANIFEST)
    _write_manifest(
        proj,
        "v.yaml",
        """service: v
secrets:
  api_key: ''
publishers:
  fake:
    dev:
      api_key: /v/api_key
rotation:
  runbook: r.md
  groups:
    v:
      max_age_days: 30
      producer: static
      secrets:
        - name: api_key
""",
    )

    assert _publish.run(_publish_args(proj, "dev", only=["m:tokens"])) == 0
    data = yaml.safe_load(path.read_text())
    assert len(data["secrets"]["token"]) == 32
    assert data["secrets"]["key"] == ""
    assert set(fake.store) == {("dev", "/m/token")}


def test_minting_still_fails_on_a_selected_unmintable_secret(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST.replace("client_id: CID", "client_id: ''"))

    assert _publish.run(_publish_args(proj, "dev", only=["g:g"])) == 1
    assert "manifests/g.yaml: secret 'client_id' is empty" in capsys.readouterr().err
    assert fake.calls == []


# --- _validate.run with a fake publisher ---


def test_validate_drives_fake_publisher_end_to_end(tmp_path, monkeypatch, capsys):
    """Plug the inline `_FakePublisher` into ``_validate.run`` and confirm
    publication classification:

    - one manifest declaration matching the in-memory store value → no issue
    - one manifest declaration with no store-side row → ``missing``
    """
    fake = _FakePublisher()
    fake.store[("dev", "/oauth/g/client_id")] = "CID"  # matches manifest → same
    _patch_destinations(monkeypatch, _validate, fake)

    proj = _make_project(tmp_path)
    _write_manifest(
        proj,
        "g.yaml",
        """service: g
secrets:
  client_id: CID
  client_secret: SECRET_NOT_PUSHED_YET
publishers:
  fake:
    dev:
      client_id: /oauth/g/client_id
      client_secret: /oauth/g/client_secret
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      producer: interactive
      last_rotated: 2099-01-01
      secrets:
        - name: client_id
        - name: client_secret
""",
    )

    rc = _validate.run(_validate_args(proj, "dev"))
    # `missing` invalidates the system → rc 1.
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert (payload["target"], payload["envs"]) == ("t", ["dev"])
    pub = payload["publication"]
    assert pub["ok"] is False
    assert len(pub["missing"]) == 1
    assert pub["missing"][0]["store_address"] == "/oauth/g/client_secret"
    assert pub["missing"][0]["publisher"] == "fake"
    assert pub["differs"] == []
    assert pub["store_only"] == []
    assert pub["reach_errors"] == []


def test_validate_store_only_is_red(tmp_path, monkeypatch, capsys):
    """A secret added to the store by hand fails validate as `store_only`."""
    fake = _FakePublisher()
    fake.store[("dev", "/oauth/g/client_id")] = "CID"
    fake.store[("test", "/oauth/g/client_id")] = "CID"
    _patch_destinations(monkeypatch, _validate, fake)
    proj = _make_project(tmp_path)
    _write_manifest(
        proj,
        "g.yaml",
        G_MANIFEST.replace(
            "      producer: interactive\n",
            "      producer: interactive\n      last_rotated: 2099-01-01\n",
        ),
    )

    assert _validate.run(_validate_args(proj)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True

    fake.store[("test", "/by/hand")] = "stray"
    assert _validate.run(_validate_args(proj)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["publication"]["store_only"] == [
        {"env": "test", "publisher": "fake", "store_address": "/by/hand"}
    ]
    assert payload["valid"] is False


def test_validate_reads_back_with_the_per_target_overlay(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    fake.store[("dev", "/oauth/g/client_id")] = "PER-TARGET"
    _patch_destinations(monkeypatch, _validate, fake)
    proj = _make_project(tmp_path)
    _write_manifest(
        proj,
        "g.yaml",
        G_MANIFEST.replace(
            "      producer: interactive\n",
            "      producer: interactive\n      last_rotated: 2099-01-01\n",
        ),
    )
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text("g:\n  client_id: PER-TARGET\n")

    assert _validate.run(_validate_args(proj, "dev")) == 0
    assert json.loads(capsys.readouterr().out)["publication"]["differs"] == []


UNDECLARED_TARGET_VALUES = "g:\n  client_id: X\n  clientid: TYPO\nnope:\n  k: v\n"


def test_validate_flags_target_values_no_manifest_declares(tmp_path, monkeypatch, capsys):
    """Decision Q1: a per-target value is declared in its manifest; a typo
    or a stale slug in targets/<t>.yaml would otherwise overlay nothing."""
    _patch_destinations(monkeypatch, _validate, _FakePublisher())
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text(UNDECLARED_TARGET_VALUES)

    assert _validate.run(_validate_args(proj, schema_only=True)) == 1
    errors = json.loads(capsys.readouterr().out)["schema"]["errors"]
    assert errors == [
        {
            "manifest": "targets/t.yaml",
            "errors": [
                "targets/t.yaml: g.clientid is declared by no `secrets:` entry of manifest "
                "'g'; declare it there (empty value, `static` producer)",
                "targets/t.yaml: 'nope' names no manifest",
            ],
        }
    ]


def test_publish_refuses_target_values_no_manifest_declares(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text(UNDECLARED_TARGET_VALUES)

    assert _publish.run(_publish_args(proj, "dev")) == 1
    err = capsys.readouterr().err
    assert "g.clientid is declared by no `secrets:` entry" in err
    assert "'nope' names no manifest" in err
    assert fake.calls == [] and fake.store == {}


def test_validate_schema_only_skips_publication(tmp_path, monkeypatch, capsys):
    """`--schema-only` never collects from a destination."""

    class _NoCollect(_FakePublisher):
        def collect(self, **_kw):
            raise AssertionError("publication gate should not run under --schema-only")

    _patch_destinations(monkeypatch, _validate, _NoCollect())
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)

    rc = _validate.run(_validate_args(proj, schema_only=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"]["ok"] is True


# --- a target publishes and validates only the manifests it lists (F-56 D3) ---

UNLISTED_MANIFEST = """service: u
secrets:
  api_key: ''
publishers:
  fake:
    dev:
      api_key: /u/api_key
rotation:
  runbook: r.md
  groups:
    u:
      max_age_days: 30
      producer: static
      secrets:
        - name: api_key
"""


def test_publish_loads_only_the_listed_manifests(tmp_path, monkeypatch):
    """An unlisted manifest is neither loaded nor published, even one that
    would fail the run (an empty secret only a human can fill)."""
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    _write_manifest(proj, "u.yaml", UNLISTED_MANIFEST)
    _write_manifest(proj, "broken.yaml", "secrets: [not, a, mapping]\n")

    assert _publish.run(_publish_args(proj, "dev")) == 0
    assert fake.store == {("dev", "/oauth/g/client_id"): "CID"}


def test_only_an_unlisted_slug_is_a_usage_error(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    _write_manifest(proj, "u.yaml", UNLISTED_MANIFEST)

    assert _publish.run(_publish_args(proj, "dev", only=["u:api_key"])) == 2
    err = capsys.readouterr().err
    assert "target 't' does not publish manifest 'u' (its `manifests:` lists: g)" in err
    assert fake.calls == []


def test_target_values_for_an_unlisted_manifest_are_an_error(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    _patch_destinations(monkeypatch, _publish, fake)
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    _write_manifest(proj, "u.yaml", UNLISTED_MANIFEST)
    (proj / "targets").mkdir()
    (proj / "targets" / "t.yaml").write_text("u:\n  api_key: X\n")

    assert _publish.run(_publish_args(proj, "dev")) == 1
    assert (
        "targets/t.yaml: 'u' is a manifest target 't' does not list in its `manifests:`"
        in capsys.readouterr().err
    )
    assert fake.calls == []


def test_validate_checks_only_the_listed_manifests(tmp_path, monkeypatch, capsys):
    fake = _FakePublisher()
    fake.store[("dev", "/oauth/g/client_id")] = "CID"
    _patch_destinations(monkeypatch, _validate, fake)
    proj = _make_project(tmp_path)
    _write_manifest(
        proj,
        "g.yaml",
        G_MANIFEST.replace(
            "      producer: interactive\n",
            "      producer: interactive\n      last_rotated: 2099-01-01\n",
        ),
    )
    _write_manifest(proj, "u.yaml", UNLISTED_MANIFEST)  # never rotated, never published
    _write_manifest(proj, "broken.yaml", "secrets: [not, a, mapping]\n")

    assert _validate.run(_validate_args(proj, "dev")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True and payload["files_checked"] == 1


def test_validate_names_every_nested_manifest_file(tmp_path, monkeypatch, capsys):
    """F-56 D1: a manifest is manifests/<slug>.yaml; a file below a
    subdirectory has no slug. The template file is ignored as before."""
    _patch_destinations(monkeypatch, _validate, _FakePublisher())
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    (proj / "manifests" / "accounts" / "deep").mkdir(parents=True)
    _write_manifest(proj, "accounts/apple.yaml", "service: apple\n")
    _write_manifest(proj, "accounts/deep/aws.yaml", "service: aws\n")
    _write_manifest(proj, "accounts/_template.yaml", "service: example\n")
    _write_manifest(proj, "accounts/notes.md", "not yaml\n")

    assert _validate.run(_validate_args(proj, schema_only=True)) == 1
    errors = json.loads(capsys.readouterr().out)["schema"]["errors"]
    assert errors == [
        {
            "manifest": "manifests/",
            "errors": [
                "manifests/accounts/apple.yaml: manifests sit directly under manifests/ "
                "(the slug is the file stem); move it to manifests/<slug>.yaml",
                "manifests/accounts/deep/aws.yaml: manifests sit directly under manifests/ "
                "(the slug is the file stem); move it to manifests/<slug>.yaml",
            ],
        }
    ]


def test_validate_fails_while_accounts_dir_exists(tmp_path, monkeypatch, capsys):
    """F-56 D2: accounts/ is retired; a leftover one fails validate,
    reported before the per-manifest checks."""
    _patch_destinations(monkeypatch, _validate, _FakePublisher())
    proj = _make_project(tmp_path)
    _write_manifest(proj, "g.yaml", G_MANIFEST)
    assert _validate.run(_validate_args(proj, schema_only=True)) == 0
    capsys.readouterr()

    (proj / "accounts").mkdir()
    assert _validate.run(_validate_args(proj, schema_only=True)) == 1
    errors = json.loads(capsys.readouterr().out)["schema"]["errors"]
    assert errors[0]["manifest"] == "accounts/"
    assert "accounts directory is retired" in errors[0]["errors"][0]


def test_a_target_naming_km_account_fails_to_load_it(tmp_path, capsys):
    proj = _make_project(
        tmp_path,
        "environments: [dev]\n"
        "targets:\n"
        "  t:\n"
        "    manifests: SLUGS\n"
        "    destinations:\n"
        "      - publisher: km_account\n"
        "        envs: [dev]\n",
    )
    _write_manifest(proj, "g.yaml", "service: g\n")
    assert _validate.run(_validate_args(proj, "dev")) == 2
    out = json.loads(capsys.readouterr().out)
    assert any("no publisher 'km_account' found" in e for e in out["publication"]["reach_errors"])
