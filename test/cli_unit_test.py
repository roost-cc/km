"""Unit tests for km.config config resolution and km.cli registration."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.cli import build_parser
from km.config import find_config


def test_find_config_walks_up_to_nearest(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "mid" / "inner"
    inner.mkdir(parents=True)
    outer_cfg = outer / "config.yaml"
    outer_cfg.write_text("{}")

    assert find_config(inner) == outer_cfg


def test_find_config_prefers_closest_match(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "mid" / "inner"
    inner.mkdir(parents=True)
    (outer / "config.yaml").write_text("{}")
    inner_cfg = inner / "config.yaml"
    inner_cfg.write_text("{}")

    assert find_config(inner) == inner_cfg


def test_shells_subcommand_no_longer_registered(capsys) -> None:
    """S-372: `km shells` was removed; argparse should reject it as an
    invalid choice (exit 2)."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["shells"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "shells" in err


def test_mfa_subcommand_registered() -> None:
    from km.commands import mfa

    args = build_parser().parse_args(["mfa", "x"])
    assert args.func is mfa.run
    assert args.slug == "x"
    assert args.no_copy is False
    assert build_parser().parse_args(["mfa", "x", "--no-copy"]).no_copy is True


def test_push_subcommand_is_gone(capsys) -> None:
    """F-54: `km push` was retired in favour of `km publish`."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["push"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_publish_parses_target_envs_and_selection() -> None:
    from km.commands import publish

    args = build_parser().parse_args(["publish", "local", "dev", "test", "--all"])
    assert args.func is publish.run
    assert (args.target, args.envs, args.all, args.execute) == (
        "local",
        ["dev", "test"],
        True,
        False,
    )
    args = build_parser().parse_args(["publish", "local", "--all", "--execute"])
    assert (args.envs, args.execute) == ([], True)
    args = build_parser().parse_args(
        ["publish", "local", "dev", "--only", "synapse:roost_bot", "--only", "jwt"]
    )
    assert (args.all, args.only, args.envs) == (False, ["synapse:roost_bot", "jwt"], ["dev"])


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["publish", "local"], "one of the arguments --all --only is required"),
        (["publish", "local", "dev"], "one of the arguments --all --only is required"),
        (["publish", "local", "--all", "--only", "jwt"], "not allowed with argument"),
        (["publish"], "target"),
    ],
)
def test_publish_without_exactly_one_selection_or_target_is_a_usage_error(
    capsys, argv, says
) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert says in capsys.readouterr().err


def test_validate_parses_target_and_envs(capsys) -> None:
    args = build_parser().parse_args(["validate", "local", "dev", "--json"])
    assert (args.target, args.envs, args.json) == ("local", ["dev"], True)
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["validate"])
    assert exc.value.code == 2


def _project(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text(
        "environments: [dev, test]\n"
        "targets:\n"
        "  local:\n"
        "    manifests: [app]\n"
        "    destinations:\n"
        "      - publisher: template_file\n"
        "        envs: [dev]\n"
        "        root: ./secrets\n"
    )
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "app.yaml").write_text("service: app\n")
    return tmp_path


@pytest.mark.parametrize("command", ["publish", "validate"])
def test_undeclared_target_or_env_is_a_usage_error(tmp_path, capsys, command) -> None:
    from km.cli import main

    proj = _project(tmp_path)
    extra = ["--all"] if command == "publish" else []
    with pytest.raises(SystemExit) as exc:
        main(["-C", str(proj), command, "staging", *extra])
    assert exc.value.code == 2
    assert "unknown target 'staging'; config.yaml declares: local" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["-C", str(proj), command, "local", "prod", *extra])
    assert exc.value.code == 2
    assert "unknown environment(s) prod; config.yaml declares: dev, test" in (
        capsys.readouterr().err
    )
