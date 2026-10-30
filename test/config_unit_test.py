"""Unit tests for `config.yaml` parsing: environments, targets, retired keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.config import Destination, load


def _load(tmp_path: Path, body: str, manifests: tuple[str, ...] = ("app", "svc")):
    (tmp_path / "manifests").mkdir(exist_ok=True)
    for slug in manifests:
        (tmp_path / "manifests" / f"{slug}.yaml").write_text("service: x\n")
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return load(p)


CONFIG = """environments: [dev, test]
targets:
  local:
    manifests: [svc, app]
    destinations:
      - publisher: template_file
        envs: [dev]
        root: ./secrets
      - publisher: infisical
        host: http://localhost:8888
        envs: [dev, test]
      - publisher: postgresql
        envs: [dev, test]
        dev: {host: localhost, database: postgres, user: roost}
        test: {host: localhost, database: postgres, user: roost}
      - producer: aws_iam
        credentials_file: ./aws/credentials
  remote:
    manifests: [app]
    destinations:
      - publisher: infisical
        host: $KM_TEST_HOST
        envs: [test]
"""


def test_targets_and_environments_parse_in_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KM_TEST_HOST", "http://192.0.2.1:8888")
    cfg = _load(tmp_path, CONFIG)
    assert cfg.environments == ["dev", "test"]
    assert cfg.targets["local"].manifests == ("svc", "app")
    assert cfg.targets["remote"].manifests == ("app",)
    assert [d.publisher for d in cfg.targets["local"].destinations] == [
        "template_file",
        "infisical",
        "postgresql",
    ]
    infisical = cfg.targets["local"].destinations[1]
    assert infisical == Destination(
        publisher="infisical",
        envs=("dev", "test"),
        config={"host": "http://localhost:8888"},
        index=1,
    )
    # Every destination carries its whole configuration; nothing inherited.
    assert cfg.targets["local"].destinations[0].config == {
        "root": str((tmp_path / "secrets").resolve())
    }
    assert set(cfg.targets["local"].destinations[2].config) == {"dev", "test"}
    # $VAR expands.
    assert cfg.targets["remote"].destinations[0].config == {"host": "http://192.0.2.1:8888"}
    # A `producer:` entry is producer configuration, not a destination;
    # relative paths resolve against the config dir.
    assert cfg.producers == {
        "aws_iam": {"credentials_file": str((tmp_path / "aws/credentials").resolve())}
    }


@pytest.mark.parametrize("key", ["publishers", "producers"])
def test_retired_sections_are_a_load_error_naming_the_new_keys(tmp_path, capsys, key) -> None:
    with pytest.raises(SystemExit) as e:
        _load(tmp_path, f"{key}:\n  infisical:\n    host: x\n")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"`{key}:` is no longer accepted" in err
    assert "`environments:`" in err and "`targets:`" in err


def test_directories_accounts_is_retired(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as e:
        _load(tmp_path, "directories:\n  accounts: accounts\n")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "`directories.accounts` is retired" in err
    assert "`login:`" in err


def test_directories_have_no_accounts(tmp_path) -> None:
    cfg = _load(tmp_path, "directories:\n  manifests: m\n")
    assert cfg.directories.manifests == "m"
    assert not hasattr(cfg.directories, "accounts")
    assert not hasattr(cfg.directories, "files")


def test_directories_files_is_retired(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as e:
        _load(tmp_path, "directories:\n  files: files\n")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "`directories.files` is retired" in err
    assert "`root:`" in err


def _template_file(extra: str) -> str:
    return (
        "environments: [dev]\ntargets:\n  t:\n    manifests: [app]\n    destinations:\n"
        "      - publisher: template_file\n        envs: [dev]\n" + extra
    )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("", "`root` is required"),
        ("        root: secrets\n", "`root` must be absolute"),
        (
            "        transport: [sh, -c]\n        remote_dir: /r\n        root: /s\n",
            "`root` is a local destination's",
        ),
        ("        transport: [sh, -c]\n", "`transport` needs `remote_dir`"),
    ],
)
def test_a_template_file_destination_without_a_valid_root_fails_the_load(
    tmp_path, capsys, extra: str, message: str
) -> None:
    with pytest.raises(SystemExit) as e:
        _load(tmp_path, _template_file(extra))
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "targets.t.destinations[0]" in err and message in err


def test_a_template_file_root_is_absolute_or_config_relative(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KM_TEST_ROOT", "/srv/secrets")
    cfg = _load(tmp_path, _template_file("        root: $KM_TEST_ROOT\n"))
    assert cfg.targets["t"].destinations[0].config == {"root": "/srv/secrets"}
    cfg = _load(tmp_path, _template_file("        root: ../secrets\n"))
    assert cfg.targets["t"].destinations[0].config == {
        "root": str((tmp_path.parent / "secrets").resolve())
    }
    remote = _template_file("        transport: [sh, -c]\n        remote_dir: /r\n")
    assert _load(tmp_path, remote).targets["t"].destinations[0].config["remote_dir"] == "/r"


def test_relative_paths_resolve_in_nested_mappings(tmp_path) -> None:
    body = (
        "environments: [dev]\ntargets:\n  t:\n    manifests: [app]\n    destinations:\n"
        "      - publisher: yaml_file\n        envs: [dev]\n"
        "        files: {config: ./config.yaml, abs: /etc/x.yaml}\n"
    )
    cfg = _load(tmp_path, body)
    assert cfg.targets["t"].destinations[0].config["files"] == {
        "config": str((tmp_path / "config.yaml").resolve()),
        "abs": "/etc/x.yaml",
    }


def test_undefined_variable_fails_only_the_target_that_references_it(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("KM_TEST_HOST", raising=False)
    cfg = _load(tmp_path, CONFIG)
    assert [d.publisher for d in cfg.target("local").destinations] == [
        "template_file",
        "infisical",
        "postgresql",
    ]
    with pytest.raises(SystemExit) as e:
        cfg.target("remote")
    assert e.value.code == 2
    assert "target 'remote': undefined env var(s): $KM_TEST_HOST" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cfg.select_envs("remote", [])
    with pytest.raises(SystemExit):
        cfg.target("staging")
    assert "declares: local, remote" in capsys.readouterr().err


def test_undefined_variable_outside_targets_is_a_hard_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KM_TEST_DIR", raising=False)
    with pytest.raises(SystemExit):
        _load(tmp_path, "directories:\n  manifests: $KM_TEST_DIR\n")


def _target(destinations: str, manifests: str = "manifests: [app]\n") -> str:
    """``environments: [dev]`` and one target ``t``."""
    head = f"    {manifests}" if manifests else ""
    return f"environments: [dev]\ntargets:\n  t:\n{head}    destinations:\n{destinations}"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            _target("      - publisher: x\n        envs: [prod]\n"),
            "targets.t.destinations[0].envs: prod not in `environments:`",
        ),
        (_target("      - publisher: x\n"), ".envs is required"),
        (_target("      - envs: [dev]\n"), ".publisher is required"),
        (_target("      []\n"), "targets.t.destinations must be a non-empty list"),
        (
            "targets:\n  t:\n    manifests: [app]\n    destinations:\n"
            "      - publisher: x\n        envs: [dev]\n",
            "needs `environments:`",
        ),
        ("environments: dev\n", "must be a list"),
        # A target lists the manifests it publishes, by slug.
        (
            _target("      - publisher: x\n        envs: [dev]\n", manifests=""),
            "targets.t.manifests is required (non-empty list of slugs)",
        ),
        (
            _target("      - publisher: x\n        envs: [dev]\n", manifests="manifests: []\n"),
            "targets.t.manifests is required",
        ),
        (
            _target(
                "      - publisher: x\n        envs: [dev]\n",
                manifests="manifests: [app, nope]\n",
            ),
            "targets.t.manifests: nope names no manifest (have: app, svc)",
        ),
        (
            _target(
                "      - publisher: x\n        envs: [dev]\n",
                manifests="manifests: [app, app]\n",
            ),
            "targets.t.manifests lists a slug twice",
        ),
        (
            "environments: [dev]\ntargets:\n  t:\n    manifests: [app]\n    publishers: []\n",
            "targets.t: unknown key(s) publishers",
        ),
        (
            "environments: [dev]\ntargets:\n  t: oops\n",
            "targets.t must be a mapping `{manifests: [<slug>…], destinations: [...]}`",
        ),
    ],
)
def test_malformed_targets_are_load_errors(tmp_path, capsys, body, message) -> None:
    with pytest.raises(SystemExit):
        _load(tmp_path, body)
    assert message in capsys.readouterr().err


def test_a_bare_list_target_names_the_new_shape(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as e:
        _load(
            tmp_path, "environments: [dev]\ntargets:\n  t:\n    - publisher: x\n      envs: [dev]\n"
        )
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "targets.t is a list; a target is now a mapping" in err
    assert "`{manifests: [<slug>…], destinations: [...]}`" in err


def test_a_slug_is_a_file_directly_under_manifests(tmp_path, capsys) -> None:
    """A nested file has no slug, and the template is not a manifest."""
    (tmp_path / "manifests" / "accounts").mkdir(parents=True)
    (tmp_path / "manifests" / "accounts" / "apple.yaml").write_text("service: x\n")
    (tmp_path / "manifests" / "_template.yaml").write_text("service: x\n")
    for slug in ("accounts/apple", "apple", "_template"):
        with pytest.raises(SystemExit) as e:
            _load(
                tmp_path,
                _target("      - publisher: x\n        envs: [dev]\n", f"manifests: [{slug}]\n"),
            )
        assert e.value.code == 2
        assert f"{slug} names no manifest" in capsys.readouterr().err


def test_producer_entries_sit_among_the_destinations(tmp_path) -> None:
    cfg = _load(
        tmp_path,
        _target(
            "      - producer: aws_iam\n        aws_profile: p\n      - publisher: x\n        envs: [dev]\n"
        ),
    )
    assert [d.label for d in cfg.targets["t"].destinations] == ["0:x"]
    assert cfg.producers == {"aws_iam": {"aws_profile": "p"}}


def test_unknown_target_is_a_usage_error_naming_the_declared_ones(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("KM_TEST_HOST", "h")
    cfg = _load(tmp_path, CONFIG)
    with pytest.raises(SystemExit) as e:
        cfg.target("staging")
    assert e.value.code == 2
    assert (
        "unknown target 'staging'; config.yaml declares: local, remote" in capsys.readouterr().err
    )


def test_select_envs(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KM_TEST_HOST", "h")
    cfg = _load(tmp_path, CONFIG)
    assert cfg.select_envs("local", []) == ["dev", "test"]
    assert cfg.select_envs("remote", []) == ["test"]
    assert cfg.select_envs("local", ["test", "dev", "test"]) == ["test", "dev"]
    with pytest.raises(SystemExit):
        cfg.select_envs("local", ["prod"])
    assert "unknown environment(s) prod; config.yaml declares: dev, test" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cfg.select_envs("remote", ["dev"])
    assert "target 'remote' accepts no destination for environment(s) dev" in (
        capsys.readouterr().err
    )


def test_empty_config_has_no_targets(tmp_path) -> None:
    cfg = _load(tmp_path, "{}\n")
    assert cfg.environments == [] and cfg.targets == {} and cfg.producers == {}
