"""Unit tests for YamlFilePublisher: JSONPath-addressed edits, apply and readback against
fixture yaml (S-963; the cases of the synapse publisher it replaced, S-953)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from km.commands import _publish as publish_cmd
from km.publishers import ManifestRef
from km.publishers.yaml_file import YamlFilePublisher

HOMESERVER_YAML = """# Synapse dev config
server_name: "roost.local"
registration_shared_secret: "old_shared"
macaroon_secret_key: "old_macaroon"
database:
  name: psycopg2
  args:
    user: synapse
    password: old_db
"""

APPSERVICE_YAML = """id: roost-bot
url: "http://bot:3000"
as_token: "old_as"
hs_token: "old_hs"
sender_localpart: roost-bot
"""

CONFIG_YAML = """environments: [dev]
targets:
  local:
    destinations:
      - publisher: template_file   # renders the env files
        envs: [dev]
      - publisher: postgresql
        envs: [dev]
        dev:
          password: 'old_admin'
"""

SECTION = {
    "dev": {
        "registration_shared_secret": "homeserver:$.registration_shared_secret",
        "macaroon_secret": "homeserver:$.macaroon_secret_key",
        "db_password": "homeserver:$.database.args.password",
        "as_token": "appservice:$.as_token",
        "hs_token": "appservice:$.hs_token",
    }
}

SECRETS = {
    "registration_shared_secret": "new_shared",
    "macaroon_secret": "new_macaroon",
    "db_password": "new_db",
    "as_token": "new_as",
    "hs_token": "new_hs",
}

FILTER = "config:$.targets.local.destinations[?@.publisher=='postgresql'].dev.password"


@pytest.fixture
def files(tmp_path: Path) -> Path:
    root = tmp_path / "secrets"
    (root / "synapse").mkdir(parents=True)
    (root / "synapse" / "homeserver.dev.yaml").write_text(HOMESERVER_YAML)
    (root / "synapse" / "roost-bot.dev.yaml").write_text(APPSERVICE_YAML)
    (root / "config.yaml").write_text(CONFIG_YAML)
    return root


def _pub(files: Path, apply: list[str] | None = None) -> YamlFilePublisher:
    config: dict = {
        "files": {
            "homeserver": str(files / "synapse" / "homeserver.{env}.yaml"),
            "appservice": str(files / "synapse" / "roost-bot.{env}.yaml"),
            "config": str(files / "config.yaml"),
        }
    }
    if apply is not None:
        config["apply"] = apply
    return YamlFilePublisher(config)


def _publish(pub: YamlFilePublisher, secrets=SECRETS, names=None, dry_run=False, section=SECTION):
    """Publish one manifest to dev, then apply (which writes) when nothing
    failed, as `km publish` does."""
    result = pub.publish(
        manifest_path=Path("synapse.yaml"),
        env="dev",
        secrets=secrets,
        section=section,
        dry_run=dry_run,
        names=names,
    )
    if result.written and not result.failed:
        pub.apply(env="dev", dry_run=dry_run)
    return result


def _collect(pub: YamlFilePublisher, secrets=SECRETS, section=SECTION) -> dict[str, str]:
    result = pub.collect(
        env="dev",
        manifests=[ManifestRef(path=Path("synapse.yaml"), secrets=secrets, section=section)],
    )
    return {e.local_name: e.state for e in result.entries}


# --- configuration and schema ---


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "`files` is required"),
        ({"files": {"h": "relative.yaml"}}, "must be absolute"),
        ({"files": {"a:b": "/x.yaml"}}, "without `:`"),
        ({"files": {"h": "/h.{target}.yaml"}}, "only the `{env}` placeholder"),
        ({"files": {"h": "/h.{.yaml"}}, "only the `{env}` placeholder"),
        ({"files": {"h": "/h"}, "apply": "restart"}, "`apply` must"),
        ({"files": {"h": "/h"}, "reload": ["x"]}, "unknown configuration key"),
    ],
)
def test_bad_configuration_is_an_error(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        YamlFilePublisher(config)


def test_apply_is_optional(files: Path) -> None:
    # apply writes the staged files, so km publish always calls it.
    assert _pub(files).applies()
    assert _pub(files, apply=["true"]).applies()
    _pub(files).apply(env="dev", dry_run=False)  # nothing staged, no command


def test_validate_accepts_the_section_and_rejects_bad_addresses(files: Path) -> None:
    pub = _pub(files)
    assert pub.validate(manifest_path=Path("m.yaml"), section=SECTION, secrets=SECRETS) == []
    errs = pub.validate(
        manifest_path=Path("m.yaml"),
        section={"dev": {"as_token": "appservice:as_token", "nope": "appservice:$.x"}},
        secrets={"as_token": "v"},
    )
    assert any("address must be `<key>:<query>`" in e for e in errs)
    assert any("'nope' has no matching secret" in e for e in errs)


def test_validate_reports_a_query_that_does_not_parse(files: Path) -> None:
    errs = _pub(files).validate(
        manifest_path=Path("m.yaml"),
        section={"dev": {"as_token": "appservice:$.as_token[?"}},
        secrets={"as_token": "v"},
    )
    assert len(errs) == 1
    assert "m.yaml" in errs[0] and "'as_token'" in errs[0]
    assert "query '$.as_token[?' does not parse" in errs[0]


def test_validate_reports_an_unknown_key(files: Path) -> None:
    errs = _pub(files).validate(
        manifest_path=Path("m.yaml"),
        section={"dev": {"as_token": "registration:$.as_token"}},
        secrets={"as_token": "v"},
    )
    assert len(errs) == 1 and "unknown file key 'registration'" in errs[0]


def test_schema_instance_checks_queries_but_not_keys() -> None:
    pub = YamlFilePublisher.schema_instance()
    section = {"dev": {"as_token": "registration:$.as_token", "hs_token": "x:$[?"}}
    errs = pub.validate(manifest_path=Path("m.yaml"), section=section, secrets=None)
    assert len(errs) == 1 and "does not parse" in errs[0]


# --- publish ---


def test_publish_edits_both_files_in_place(files: Path) -> None:
    pub = _pub(files)
    assert _publish(pub).written == 5
    home = (files / "synapse" / "homeserver.dev.yaml").read_text()
    app = (files / "synapse" / "roost-bot.dev.yaml").read_text()
    # A quoted value stays quoted.
    assert 'registration_shared_secret: "new_shared"' in home
    assert 'macaroon_secret_key: "new_macaroon"' in home
    assert "password: new_db" in home
    # The rest of the file survives, comments and quoting included.
    assert home.startswith("# Synapse dev config\n")
    assert 'server_name: "roost.local"' in home
    assert 'as_token: "new_as"' in app and 'hs_token: "new_hs"' in app
    assert 'url: "http://bot:3000"' in app


def test_publish_sets_the_node_a_filter_selects(files: Path) -> None:
    section = {"dev": {"admin": FILTER}}
    result = _publish(_pub(files), secrets={"admin": "new_admin"}, section=section)
    assert result.written == 1
    out = (files / "config.yaml").read_text()
    assert "password: 'new_admin'" in out
    assert "- publisher: template_file   # renders the env files" in out
    assert _collect(_pub(files), secrets={"admin": "new_admin"}, section=section) == {
        "admin": "same"
    }


def test_publish_writes_only_the_selected_names(files: Path) -> None:
    pub = _pub(files)
    assert _publish(pub, names={"as_token"}).written == 1
    app = (files / "synapse" / "roost-bot.dev.yaml").read_text()
    assert 'as_token: "new_as"' in app and 'hs_token: "old_hs"' in app
    assert (files / "synapse" / "homeserver.dev.yaml").read_text() == HOMESERVER_YAML


def test_dry_run_writes_nothing(files: Path, capsys) -> None:
    pub = _pub(files)
    assert _publish(pub, dry_run=True).written == 5
    assert (files / "synapse" / "homeserver.dev.yaml").read_text() == HOMESERVER_YAML
    assert "[dry-run]" in capsys.readouterr().err


def test_a_missing_field_fails_and_leaves_the_file(files: Path, capsys) -> None:
    (files / "synapse" / "roost-bot.dev.yaml").write_text("id: roost-bot\nas_token: x\n")
    result = _publish(_pub(files), names={"as_token", "hs_token"})
    assert (result.written, result.failed) == (0, 2)
    err = capsys.readouterr().err
    assert "synapse.yaml: secret 'hs_token': query '$.hs_token' selects 0 nodes" in err
    assert (files / "synapse" / "roost-bot.dev.yaml").read_text() == "id: roost-bot\nas_token: x\n"


def test_a_query_matching_two_nodes_fails_and_leaves_the_file(files: Path, capsys) -> None:
    before = (files / "config.yaml").read_text()
    section = {"dev": {"admin": "config:$.targets.local.destinations[*].envs[0]"}}
    result = _publish(_pub(files), secrets={"admin": "x"}, section=section)
    assert (result.written, result.failed) == (0, 1)
    assert "secret 'admin': query" in capsys.readouterr().err
    assert (files / "config.yaml").read_text() == before
    with pytest.raises(ValueError, match="selects 2 nodes"):
        _collect(_pub(files), secrets={"admin": "x"}, section=section)


def test_a_query_selecting_a_mapping_fails(files: Path, capsys) -> None:
    section = {"dev": {"db_password": "homeserver:$.database.args"}}
    result = _publish(_pub(files), section=section)
    assert (result.written, result.failed) == (0, 1)
    assert "not a scalar" in capsys.readouterr().err
    assert (files / "synapse" / "homeserver.dev.yaml").read_text() == HOMESERVER_YAML


def test_a_query_that_does_not_parse_fails_the_write(files: Path, capsys) -> None:
    result = _publish(_pub(files), section={"dev": {"as_token": "appservice:$[?"}})
    assert (result.written, result.failed) == (0, 1)
    assert (files / "synapse" / "roost-bot.dev.yaml").read_text() == APPSERVICE_YAML


def test_an_unknown_key_fails_the_secret(files: Path, capsys) -> None:
    result = _publish(_pub(files), section={"dev": {"as_token": "registration:$.as_token"}})
    assert (result.written, result.failed) == (0, 1)
    assert "unknown file key 'registration'" in capsys.readouterr().err
    with pytest.raises(ValueError, match="unknown file key 'registration'"):
        _collect(_pub(files), section={"dev": {"as_token": "registration:$.as_token"}})


def _bound(pub: YamlFilePublisher, *other_keys: str) -> YamlFilePublisher:
    """``pub`` bound to a config whose other target has a yaml_file
    destination naming ``other_keys``."""
    from km.config import Destination, Target

    own = Destination(publisher="yaml_file", envs=("dev",), config={"files": dict(pub._paths)})
    other = Destination(
        publisher="yaml_file", envs=("dev",), config={"files": {k: "/x" for k in other_keys}}
    )
    cfg = argparse.Namespace(
        targets={
            "local": Target(name="local", manifests=("m",), destinations=[own]),
            "km": Target(name="km", manifests=("m",), destinations=[other]),
        }
    )
    pub.bind_config(cfg)
    return pub


def test_a_key_another_destination_names_is_skipped(files: Path) -> None:
    """postgresql-admin writes `config:` through the km target; local's
    yaml_file destination (homeserver, appservice) leaves it alone."""
    pub = YamlFilePublisher(
        {"files": {"homeserver": str(files / "synapse" / "homeserver.{env}.yaml")}}
    )
    _bound(pub, "config")
    section = {"dev": {"admin": FILTER, "db_password": "homeserver:$.database.args.password"}}
    secrets = {"admin": "new_admin", "db_password": "new_db"}
    assert pub.validate(manifest_path=Path("m.yaml"), section=section, secrets=secrets) == []
    result = _publish(pub, secrets=secrets, section=section)
    assert (result.written, result.failed, result.skipped) == (1, 0, 0)
    assert "password: 'old_admin'" in (files / "config.yaml").read_text()
    assert _collect(pub, secrets=secrets, section=section) == {"db_password": "same"}


def test_a_key_no_destination_names_is_an_error_once_bound(files: Path, capsys) -> None:
    pub = _bound(_pub(files), "config")
    section = {"dev": {"as_token": "registration:$.as_token"}}
    errs = pub.validate(manifest_path=Path("m.yaml"), section=section, secrets={"as_token": "v"})
    assert len(errs) == 1 and "unknown file key 'registration'" in errs[0]
    assert "no yaml_file destination names it" in errs[0]
    assert _publish(pub, section=section).failed == 1
    with pytest.raises(ValueError, match="unknown file key 'registration'"):
        _collect(pub, section=section)
    # The schema instance checks keys too once bound.
    schema = YamlFilePublisher.schema_instance()
    schema.bind_config(argparse.Namespace(targets={}))
    errs = schema.validate(manifest_path=Path("m.yaml"), section=section, secrets=None)
    assert len(errs) == 1 and "unknown file key 'registration'" in errs[0]


def test_a_missing_file_fails_and_creates_nothing(files: Path, capsys) -> None:
    (files / "synapse" / "roost-bot.dev.yaml").unlink()
    result = _publish(_pub(files), names={"as_token"})
    assert (result.written, result.failed) == (0, 1)
    assert not (files / "synapse" / "roost-bot.dev.yaml").exists()


def test_a_failed_write_leaves_the_file_whole(files: Path, monkeypatch, capsys) -> None:
    """A reader must never see a half-written file."""
    from km.publishers import yaml_file as yaml_file_mod

    def _dump_then_fail(yaml, data, stream) -> None:
        stream.write("server_name: ")
        raise OSError("disk full")

    monkeypatch.setattr(yaml_file_mod, "_dump", _dump_then_fail)
    with pytest.raises(RuntimeError, match="nothing written: disk full"):
        _publish(_pub(files), names={"macaroon_secret"})
    assert (files / "synapse" / "homeserver.dev.yaml").read_text() == HOMESERVER_YAML
    assert sorted(p.name for p in (files / "synapse").iterdir()) == [
        "homeserver.dev.yaml",
        "roost-bot.dev.yaml",
    ]


def test_empty_secret_is_skipped(files: Path) -> None:
    result = _publish(_pub(files), secrets={**SECRETS, "hs_token": ""}, names={"hs_token"})
    assert (result.written, result.skipped) == (0, 1)


# --- apply ---


def test_apply_runs_the_command(files: Path, tmp_path: Path) -> None:
    marker = tmp_path / "reloaded"
    pub = _pub(files, apply=["sh", "-c", f"echo dev >> {marker}"])
    pub.apply(env="dev", dry_run=True)
    assert not marker.exists()
    pub.apply(env="dev", dry_run=False)
    assert marker.read_text() == "dev\n"


def test_apply_failure_raises(files: Path) -> None:
    with pytest.raises(RuntimeError, match="apply command failed"):
        _pub(files, apply=["sh", "-c", "exit 2"]).apply(env="dev", dry_run=False)


def test_publish_applies_once_per_env_after_the_writes(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "manifests").mkdir(parents=True)
    (proj / "runbooks").mkdir()
    (proj / "runbooks" / "r.md").write_text("# stub\n")
    out = tmp_path / "out"
    out.mkdir()
    for env in ("dev", "test"):
        (out / f"s.{env}.yaml").write_text("a: old\nb: old\n")
    log = tmp_path / "apply.log"
    (proj / "config.yaml").write_text(
        f"""environments: [dev, test]
targets:
  local:
    manifests: [a, b]
    destinations:
      - publisher: yaml_file
        envs: [dev, test]
        files:
          s: {out}/s.{{env}}.yaml
        apply: [sh, -c, 'cat {out}/s.dev.yaml {out}/s.test.yaml >> {log}; echo --- >> {log}']
"""
    )
    for name in ("a", "b"):
        (proj / "manifests" / f"{name}.yaml").write_text(
            f"""service: {name}
secrets:
  {name}: new
publishers:
  yaml_file:
    dev:
      {name}: s:$.{name}
    test:
      {name}: s:$.{name}
rotation:
  runbook: r.md
  groups:
    {name}:
      max_age_days: 3650
      last_rotated: "2026-01-01"
      producer: static
      secrets:
        - name: {name}
"""
        )
    code = publish_cmd.run(
        argparse.Namespace(
            config=str(proj / "config.yaml"),
            target="local",
            envs=["dev", "test"],
            all=True,
            only=None,
            execute=True,
        )
    )
    assert code == 0
    runs = log.read_text().split("---\n")[:-1]
    assert len(runs) == 2  # once per env
    # dev's apply ran after both manifests' writes to dev's file.
    assert runs[0].startswith("a: new\nb: new\n")
    assert runs[1] == "a: new\nb: new\na: new\nb: new\n"


# --- all or nothing ---


def _two_files(tmp_path: Path) -> tuple[YamlFilePublisher, Path, Path]:
    one, two = tmp_path / "one.yaml", tmp_path / "two.yaml"
    one.write_text("value: old-one\n")
    two.write_text("value: old-two\n")
    return YamlFilePublisher({"files": {"one": str(one), "two": str(two)}}), one, two


def _publish_to(pub: YamlFilePublisher, section: dict, secrets: dict, name: str = "m.yaml"):
    return pub.publish(
        manifest_path=Path(name), env="dev", secrets=secrets, section=section, dry_run=False
    )


def test_publish_is_atomic_across_all_files_and_manifest_calls(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from km.publishers import yaml_file as yaml_file_mod

    both = {"dev": {"first": "one:$.value", "second": "two:$.value"}}
    values = {"first": "new-one", "second": "new-two"}

    # publish writes nothing; apply writes every file.
    pub, one, two = _two_files(tmp_path)
    assert _publish_to(pub, both, values).written == 2
    assert (one.read_text(), two.read_text()) == ("value: old-one\n", "value: old-two\n")
    pub.apply(env="dev", dry_run=False)
    assert (one.read_text(), two.read_text()) == ("value: new-one\n", "value: new-two\n")

    # A later manifest's failure discards the earlier one's update, and
    # every later publish to the env writes nothing.
    pub, one, two = _two_files(tmp_path)
    assert _publish_to(pub, {"dev": {"first": "one:$.value"}}, values).failed == 0
    bad = _publish_to(pub, {"dev": {"second": "two:$.missing"}}, values, "bad.yaml")
    assert (bad.written, bad.failed) == (0, 1)
    later = _publish_to(pub, {"dev": {"second": "two:$.value"}}, values, "later.yaml")
    assert (later.written, later.skipped) == (0, 1)
    pub.apply(env="dev", dry_run=False)
    assert (one.read_text(), two.read_text()) == ("value: old-one\n", "value: old-two\n")

    # The second file's replace fails: the first is put back.
    pub, one, two = _two_files(tmp_path)
    _publish_to(pub, both, values)
    real_replace = yaml_file_mod.os.replace
    calls = []

    def fail_second(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("injected")
        return real_replace(src, dst)

    monkeypatch.setattr(yaml_file_mod.os, "replace", fail_second)
    with pytest.raises(RuntimeError, match=r"nothing written \(originals put back\): injected"):
        pub.apply(env="dev", dry_run=False)
    assert (one.read_text(), two.read_text()) == ("value: old-one\n", "value: old-two\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["one.yaml", "two.yaml"]


def test_publish_rollback_failure_reports_indeterminate_state(tmp_path: Path, monkeypatch) -> None:
    from km.publishers import yaml_file as yaml_file_mod

    pub, one, two = _two_files(tmp_path)
    _publish_to(
        pub,
        {"dev": {"first": "one:$.value", "second": "two:$.value"}},
        {"first": "new-one", "second": "new-two"},
    )
    real_replace = yaml_file_mod.os.replace
    calls = []

    def fail_after_first(src, dst):
        calls.append(dst)
        if len(calls) >= 2:
            raise OSError(f"injected {len(calls)}")
        return real_replace(src, dst)

    monkeypatch.setattr(yaml_file_mod.os, "replace", fail_after_first)
    with pytest.raises(RuntimeError) as ei:
        pub.apply(env="dev", dry_run=False)
    msg = str(ei.value)
    assert "state indeterminate" in msg
    assert f"putting back the originals failed for {one} (injected 3)" in msg
    assert (one.read_text(), two.read_text()) == ("value: new-one\n", "value: old-two\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["one.yaml", "two.yaml"]


def test_concurrent_publish_preserves_disjoint_yaml_updates(tmp_path: Path, monkeypatch) -> None:
    """Two runs that both read the file before either writes keep both
    updates: each re-reads under the directory lock."""
    import threading
    import time

    from km.publishers import yaml_file as yaml_file_mod

    shared = tmp_path / "shared.yaml"
    shared.write_text("a: old-a\nb: old-b\n")
    real_replace = yaml_file_mod.os.replace

    def slow_replace(src, dst):
        time.sleep(0.2)  # without the lock, the other run reads the old file meanwhile
        return real_replace(src, dst)

    monkeypatch.setattr(yaml_file_mod.os, "replace", slow_replace)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(name: str) -> None:
        pub = YamlFilePublisher({"files": {"shared": str(shared)}})
        try:
            _publish_to(pub, {"dev": {name: f"shared:$.{name}"}}, {name: f"new-{name}"})
            start.wait()
            pub.apply(env="dev", dry_run=False)
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert shared.read_text() == "a: new-a\nb: new-b\n"


_ITEMS = "items:\n  - {name: a, alias: y, secret: old-1}\n  - {name: b, alias: x, secret: old-2}\n"
_BY_NAME_AND_ALIAS = {
    "by_name": "shared:$.items[?@.name=='a'].secret",
    "by_alias": "shared:$.items[?@.alias=='x'].secret",
}


@pytest.mark.parametrize(
    ("section", "concurrent", "error"),
    [
        # Two queries that selected different nodes now meet on one.
        (
            _BY_NAME_AND_ALIAS,
            "items:\n  - {name: a, alias: x, secret: old-1}\n  - {name: b, alias: y, secret: old-2}\n",
            "also sets",
        ),
        # A query that selected one node now selects two.
        (
            {"by_alias": "shared:$.items[?@.alias=='x'].secret"},
            "items:\n  - {name: a, alias: x, secret: old-1}\n  - {name: b, alias: x, secret: old-2}\n",
            "selects 2 nodes",
        ),
        # A query's node is gone.
        (
            {"by_alias": "shared:$.items[?@.alias=='x'].secret"},
            "items:\n  - {name: a, alias: y, secret: old-1}\n",
            "selects 0 nodes",
        ),
        # A query's node is no longer a scalar.
        (
            {"by_name": "shared:$.items[?@.name=='a'].secret"},
            "items:\n  - {name: a, alias: y, secret: {k: v}}\n",
            "not a scalar",
        ),
    ],
)
def test_concurrent_publish_rejects_addresses_that_converge_on_one_node(
    tmp_path: Path, section: dict, concurrent: str, error: str
) -> None:
    """apply re-checks under the lock every rule publish checked against
    the file; a file changed in between fails the env's update, and no
    file is written (the other file of the env included)."""
    shared, other = tmp_path / "shared.yaml", tmp_path / "other.yaml"
    shared.write_text(_ITEMS)
    other.write_text("value: old\n")
    pub = YamlFilePublisher({"files": {"shared": str(shared), "other": str(other)}})
    secrets = {name: f"new-{name}" for name in section} | {"o": "new"}
    result = _publish_to(pub, {"dev": section | {"o": "other:$.value"}}, secrets)
    assert result.failed == 0
    shared.write_text(concurrent)  # another run's write, between publish and apply
    with pytest.raises(RuntimeError, match=rf"nothing written: .*{error}"):
        pub.apply(env="dev", dry_run=False)
    assert (shared.read_text(), other.read_text()) == (concurrent, "value: old\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.yaml", "shared.yaml"]


def _atomic_project(tmp_path: Path, bad_address: str) -> tuple[Path, Path, Path]:
    proj = tmp_path / "proj"
    (proj / "manifests").mkdir(parents=True)
    (proj / "runbooks").mkdir()
    (proj / "runbooks" / "r.md").write_text("# stub\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "s.dev.yaml").write_text("a: old\nb: old\nm: {x: 1}\n")
    (out / "t.dev.yaml").write_text("a: old\n")
    (out / "broken.dev.yaml").write_text("a: [unclosed\n")
    marker = tmp_path / "applied"
    (proj / "config.yaml").write_text(
        f"""environments: [dev]
targets:
  local:
    manifests: [a, b]
    destinations:
      - publisher: yaml_file
        envs: [dev]
        files:
          s: {out}/s.{{env}}.yaml
          t: {out}/t.{{env}}.yaml
          gone: {out}/gone.{{env}}.yaml
          broken: {out}/broken.{{env}}.yaml
        apply: [sh, -c, 'touch {marker}']
"""
    )
    group = (
        "rotation:\n  runbook: r.md\n  groups:\n    g:\n      max_age_days: 3650\n"
        '      last_rotated: "2026-01-01"\n      producer: static\n      secrets:\n'
    )
    (proj / "manifests" / "a.yaml").write_text(
        "service: a\nsecrets:\n  a: new\n  a2: new\npublishers:\n  yaml_file:\n    dev:\n"
        "      a: s:$.a\n      a2: t:$.a\n" + group + "        - name: a\n        - name: a2\n"
    )
    (proj / "manifests" / "b.yaml").write_text(
        "service: b\nsecrets:\n  b: new\npublishers:\n  yaml_file:\n    dev:\n"
        f'      b: "{bad_address}"\n' + group + "        - name: b\n"
    )
    return proj, out, marker


@pytest.mark.parametrize("execute", [True, False])
@pytest.mark.parametrize(
    "bad_address",
    [
        "s:$.missing",  # selects no node
        "s:$[?@ == 'old']",  # selects two nodes
        "s:$.m",  # selects a mapping
        "s:$.a",  # the node a.yaml sets
        "s:$[?",  # does not parse
        "nope:$.b",  # no destination names the key
        "gone:$.b",  # the file is missing
        "broken:$.a",  # the file is not yaml
    ],
)
def test_yaml_file_destination_update_is_atomic_across_manifests(
    tmp_path: Path, bad_address: str, execute: bool, capsys
) -> None:
    """b.yaml (published after a.yaml) fails: a.yaml's writes to both
    files are not made, apply does not run, the run fails, dry run too."""
    proj, out, marker = _atomic_project(tmp_path, bad_address)
    code = publish_cmd.run(
        argparse.Namespace(
            config=str(proj / "config.yaml"),
            target="local",
            envs=["dev"],
            all=True,
            only=None,
            execute=execute,
        )
    )
    assert code == 1, capsys.readouterr().err
    assert (out / "s.dev.yaml").read_text() == "a: old\nb: old\nm: {x: 1}\n"
    assert (out / "t.dev.yaml").read_text() == "a: old\n"
    assert not marker.exists()
    assert sorted(p.name for p in out.iterdir()) == ["broken.dev.yaml", "s.dev.yaml", "t.dev.yaml"]


def test_yaml_file_destination_update_commits_when_every_manifest_succeeds(
    tmp_path: Path,
) -> None:
    proj, out, marker = _atomic_project(tmp_path, "s:$.b")
    code = publish_cmd.run(
        argparse.Namespace(
            config=str(proj / "config.yaml"),
            target="local",
            envs=["dev"],
            all=True,
            only=None,
            execute=True,
        )
    )
    assert code == 0
    assert (out / "s.dev.yaml").read_text() == "a: new\nb: new\nm: {x: 1}\n"
    assert (out / "t.dev.yaml").read_text() == "a: new\n"
    assert marker.exists()


# --- collect ---


def test_collect_reads_every_field_back(files: Path) -> None:
    pub = _pub(files)
    assert set(_collect(pub).values()) == {"differs"}
    _publish(pub)
    assert set(_collect(pub).values()) == {"same"}


def test_collect_reports_a_missing_file_or_node_as_manifest_only(files: Path) -> None:
    pub = _pub(files)
    _publish(pub)
    (files / "synapse" / "roost-bot.dev.yaml").unlink()
    (files / "synapse" / "homeserver.dev.yaml").write_text(
        "registration_shared_secret: new_shared\n"
    )
    assert _collect(pub) == {
        "registration_shared_secret": "same",
        "macaroon_secret": "manifest_only",
        "db_password": "manifest_only",
        "as_token": "manifest_only",
        "hs_token": "manifest_only",
    }


def test_collect_pairs_store_and_manifest_values_on_differs(files: Path) -> None:
    result = _pub(files).collect(
        env="dev",
        manifests=[ManifestRef(path=Path("synapse.yaml"), secrets=SECRETS, section=SECTION)],
    )
    row = next(e for e in result.entries if e.local_name == "as_token")
    assert (row.value, row.manifest_value) == ("old_as", "new_as")
    assert row.store_address == f"{files}/synapse/roost-bot.dev.yaml:$.as_token"
