"""Precedence of km's starting directory: -C/--dir, then $KM_PROJECT_DIR, then the cwd."""

from __future__ import annotations

from pathlib import Path

import pytest

from km import location
from km.config import find_config
from km.project import find_project_for_cwd


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    location.set_cli_dir(None)
    monkeypatch.delenv(location.ENV_VAR, raising=False)
    yield
    location.set_cli_dir(None)


def _project(root: Path, name: str) -> Path:
    d = root / name
    (d / "manifests").mkdir(parents=True)
    (d / "config.yaml").write_text("{}\n")
    return d


def test_cwd_when_nothing_is_set(tmp_path, monkeypatch):
    proj = _project(tmp_path, "cwdproj")
    monkeypatch.chdir(proj / "manifests")
    assert find_config() == proj / "config.yaml"
    assert find_project_for_cwd().dir == proj.resolve()


def test_empty_env_falls_through_to_cwd(tmp_path, monkeypatch):
    proj = _project(tmp_path, "cwdproj")
    monkeypatch.chdir(proj)
    monkeypatch.setenv(location.ENV_VAR, "   ")
    assert find_config() == proj / "config.yaml"


def test_env_beats_cwd(tmp_path, monkeypatch):
    envproj = _project(tmp_path, "envproj")
    monkeypatch.chdir(tmp_path)  # no config.yaml here or above inside tmp_path
    monkeypatch.setenv(location.ENV_VAR, str(envproj))
    assert find_config() == envproj.resolve() / "config.yaml"
    assert find_project_for_cwd().dir == envproj.resolve()


def test_env_subdirectory_walks_up(tmp_path, monkeypatch):
    envproj = _project(tmp_path, "envproj")
    monkeypatch.setenv(location.ENV_VAR, str(envproj / "manifests"))
    assert find_project_for_cwd().dir == envproj.resolve()


def test_cli_dir_beats_env(tmp_path, monkeypatch):
    envproj = _project(tmp_path, "envproj")
    cliproj = _project(tmp_path, "cliproj")
    monkeypatch.setenv(location.ENV_VAR, str(envproj))
    location.set_cli_dir(str(cliproj))
    assert find_config() == cliproj.resolve() / "config.yaml"
    assert find_project_for_cwd().dir == cliproj.resolve()


def test_explicit_start_beats_everything(tmp_path, monkeypatch):
    envproj = _project(tmp_path, "envproj")
    hinted = _project(tmp_path, "hinted")
    monkeypatch.setenv(location.ENV_VAR, str(envproj))
    location.set_cli_dir(str(envproj))
    assert find_config(hinted / "manifests") == hinted.resolve() / "config.yaml"


def test_env_not_a_directory_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(location.ENV_VAR, str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as e:
        find_config()
    assert e.value.code == 2
    assert "$KM_PROJECT_DIR" in capsys.readouterr().err


def test_cli_dir_not_a_directory_is_an_error(tmp_path, capsys):
    location.set_cli_dir(str(tmp_path / "missing"))
    with pytest.raises(SystemExit):
        find_config()
    assert "--dir" in capsys.readouterr().err


def test_cli_parses_dir_option():
    from km.cli import build_parser

    args = build_parser().parse_args(["-C", "/x", "validate", "local"])
    assert args.project_dir == "/x"
    args = build_parser().parse_args(["--dir", "/y", "validate", "local"])
    assert args.project_dir == "/y"
