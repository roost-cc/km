"""Unit tests for `km mfa`."""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

import pytest

from km import otp
from km.commands import _mfa
from km.config import Directories, LoadedConfig
from km.project import Project

SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = LoadedConfig(
        path=tmp_path / "config.yaml",
        project_dir=tmp_path,
        directories=Directories(),
    )
    monkeypatch.setattr(_mfa, "resolve_config_path", lambda explicit: str(cfg.path))
    monkeypatch.setattr(_mfa, "load_config", lambda path: cfg)
    monkeypatch.setattr(_mfa, "resolve_project", lambda: Project(dir=tmp_path))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    return manifests


def _args(slug: str = "aws-sso", no_copy: bool = False) -> argparse.Namespace:
    return argparse.Namespace(config=None, slug=slug, no_copy=no_copy)


def _login(body: str) -> str:
    """A manifest whose inline ``login:`` is ``body`` (flow-style yaml)."""
    return f"service: x\nlogin: {body}\n"


class _Recorder:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list = []

    def __call__(self, value):
        self.calls.append(value)
        return self.result


def _run(args, *, times=(10.0,), copy=None, sleep=None):
    it = iter(times)
    return _mfa.run(
        args,
        now=lambda: next(it),
        sleep=sleep or _Recorder(),
        copy=copy or _Recorder(),
    )


def test_happy_path_prints_only_code_and_copies(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{id: me, mfa: {SECRET}}}"))
    copy = _Recorder()
    # T=55 shares the RFC 6238 T=59 window, with 5s left (no wait).
    assert _run(_args(), times=(55.0,), copy=copy) == 0
    out, err = capsys.readouterr()
    assert out == "287082\n"
    assert copy.calls == ["287082"]
    assert err == "aws-sso: 5s left, copied to clipboard\n"
    assert SECRET not in out + err


def test_happy_path_status_line(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    assert _run(_args(), times=(10.0,)) == 0
    out, err = capsys.readouterr()
    assert out == otp.totp(SECRET, at=10.0) + "\n"
    assert err == "aws-sso: 20s left, copied to clipboard\n"


def test_from_reads_the_referenced_login(project: Path, capsys) -> None:
    (project / "aws-root.yaml").write_text(_login(f"{{id: me, mfa: {SECRET}}}"))
    (project / "aws-api.yaml").write_text(_login("{from: aws-root}"))
    assert _run(_args("aws-api"), times=(10.0,)) == 0
    out, err = capsys.readouterr()
    assert out == otp.totp(SECRET, at=10.0) + "\n"
    assert err == "aws-api: 20s left, copied to clipboard\n"


def test_no_copy_skips_clipboard(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    copy = _Recorder()
    assert _run(_args(no_copy=True), copy=copy) == 0
    out, err = capsys.readouterr()
    assert out == otp.totp(SECRET, at=10.0) + "\n"
    assert copy.calls == []
    assert err == "aws-sso: 20s left, copy skipped (--no-copy)\n"


def test_failed_copy_still_exits_zero(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    assert _run(_args(), copy=_Recorder(result=False)) == 0
    out, err = capsys.readouterr()
    assert out == otp.totp(SECRET, at=10.0) + "\n"
    assert "not copied" in err


def _populate(project: Path) -> None:
    (project / "b.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    (project / "a.yaml").write_text(_login(f"{{mfa: '{SECRET.lower()}'}}"))
    (project / "via.yaml").write_text(_login("{from: b}"))
    (project / "nomfa.yaml").write_text(_login("{id: x}"))
    (project / "empty.yaml").write_text(_login("{mfa: ''}"))
    (project / "nologin.yaml").write_text(f"service: x\nnotes: {{mfa: {SECRET}}}\n")
    (project / "badlogin.yaml").write_text(_login(f"{{mfa: {SECRET}, extra: y}}"))
    (project / "list.yaml").write_text("- 1\n")
    (project / "broken.yaml").write_text("login: [\n")


@pytest.mark.parametrize("slug", ["nope", "nomfa", "empty", "nologin"])
def test_unknown_or_mfa_less_slug_lists_mfa_logins(project: Path, capsys, slug: str) -> None:
    _populate(project)
    assert _run(_args(slug)) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"error: no login {slug!r}; logins with mfa: a, b, via\n"
    assert SECRET not in err and SECRET.lower() not in err


def test_unknown_slug_with_none(project: Path, capsys) -> None:
    assert _run(_args("nope")) == 1
    assert "logins with mfa: none" in capsys.readouterr().err


def test_missing_manifests_dir_lists_none(project: Path, capsys) -> None:
    project.rmdir()
    assert _run(_args()) == 1
    assert capsys.readouterr().err == "error: no login 'aws-sso'; logins with mfa: none\n"


@pytest.mark.parametrize("name", ["../aws-sso", ".hidden", "sub/aws-sso", "_template"])
def test_slug_cannot_escape(project: Path, capsys, name: str) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    (project / ".hidden.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    (project / "_template.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    (project / "sub").mkdir()
    (project / "sub" / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    assert _run(_args(name)) == 1
    assert "no login" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("- a\n- b\n", "not a YAML mapping"),
        ("login: [\n", "yaml parse error"),
        (_login("{mfa: 123456}"), "`login.mfa` must be a string"),
        (_login("{mfa: x, password: y}"), "unknown key(s) password"),
        ("account: {mfa: x}\n", "`account:` was renamed to `login:`"),
        (_login("{mfa: '!!not-base32!!'}"), "not valid base32"),
    ],
)
def test_bad_manifest_exits_one(project: Path, capsys, body: str, fragment: str) -> None:
    (project / "aws-sso.yaml").write_text(body)
    copy = _Recorder()
    assert _run(_args(), copy=copy) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert fragment in err
    assert "!!not-base32!!" not in err
    assert copy.calls == []


def test_waits_for_next_window_near_expiry(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    sleep = _Recorder()
    assert _run(_args(), times=(27.0, 30.0), sleep=sleep) == 0
    out, err = capsys.readouterr()
    assert sleep.calls == [3]
    assert out == otp.totp(SECRET, at=30.0) + "\n"
    assert out != otp.totp(SECRET, at=27.0) + "\n"
    assert "waiting 3s for the next code" in err
    assert "aws-sso: 30s left" in err


def test_no_wait_with_time_left(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_login(f"{{mfa: {SECRET}}}"))
    sleep = _Recorder()
    assert _run(_args(), times=(10.0,), sleep=sleep) == 0
    assert sleep.calls == []
    assert "waiting" not in capsys.readouterr().err
