"""Unit tests for the login menu and `km login`."""

from __future__ import annotations

import argparse
import base64
import os
import termios
import threading
from pathlib import Path

import pytest

from km import login_menu, otp
from km.commands import _login
from km.config import Directories, LoadedConfig
from km.login_menu import MenuExit, NotATTY, run_login_menu
from km.project import Project

SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")
ACCOUNT = {
    "id": "me@example.com",
    "password": "hunter2",
    "mfa": SECRET,
    "url": "https://example.com/login",
}


class _Recorder:
    def __init__(self, result=True) -> None:
        self.result = result
        self.calls: list = []

    def __call__(self, *args):
        self.calls.append(args[0] if args else None)
        return self.result


class _Clipboard:
    """A fake clipboard that records copies and clears."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.text: str | None = None
        self.copies: list[str] = []
        self.clears = 0

    def copy(self, value: str) -> bool:
        if not self.accepts:
            return False
        self.copies.append(value)
        self.text = value
        return True

    def clear(self) -> bool:
        self.clears += 1
        self.text = ""
        return True


def _menu(
    keys,
    account=ACCOUNT,
    *,
    clip=None,
    times=(10.0,),
    open_browser=True,
    isatty=True,
    **kw,
):
    clip = clip or _Clipboard()
    key_it = iter(keys)
    time_it = iter(times)
    opened = _Recorder()
    sleep = _Recorder()
    result = run_login_menu(
        account,
        "aws-sso",
        open_browser=open_browser,
        read_key=lambda: next(key_it),
        copy=clip.copy,
        open_url=opened,
        now=lambda: next(time_it),
        sleep=sleep,
        isatty=lambda: isatty,
        **kw,
    )
    return result, clip, opened, sleep


def _assert_no_secret(text: str) -> None:
    assert ACCOUNT["password"] not in text
    assert SECRET not in text


# --- the menu --------------------------------------------------------------


def test_menu_lists_keys_and_opens_url(capsys) -> None:
    result, _, opened, _ = _menu(["x"])
    assert result is MenuExit.DONE
    assert opened.calls == ["https://example.com/login"]
    err = capsys.readouterr().err
    assert err == (
        "aws-sso — press a key:\n"
        "  u  copy username\n"
        "  p  copy password\n"
        "  t  copy MFA token\n"
        "  x  exit\n"
    )


def test_u_p_t_copy_on_demand_any_number_of_times(capsys) -> None:
    result, clip, _, _ = _menu(["u", "p", "u", "t", "t", "x"], times=(10.0, 40.0))
    assert result is MenuExit.DONE
    code1 = otp.totp(SECRET, at=10.0)
    code2 = otp.totp(SECRET, at=40.0)
    assert clip.copies == ["me@example.com", "hunter2", "me@example.com", code1, code2]
    err = capsys.readouterr().err
    assert "username copied\npassword copied\nusername copied\n" in err
    assert "MFA token copied (20s left)\nMFA token copied (20s left)\n" in err
    _assert_no_secret(err)
    assert code1 not in err and "me@example.com" not in err


def test_t_waits_for_the_next_window(capsys) -> None:
    # T=57 leaves 3s: wait, then compute the code at T=60.
    _, clip, _, sleep = _menu(["t", "x"], times=(57.0, 60.0))
    assert sleep.calls == [3]
    assert clip.copies == [otp.totp(SECRET, at=60.0)]
    err = capsys.readouterr().err
    assert "waiting 3s for the next code\nMFA token copied (30s left)\n" in err


def test_hidden_keys_are_not_listed_and_do_nothing(capsys) -> None:
    account = {"password": "hunter2"}
    _, clip, _, _ = _menu(["u", "t", "p", "x"], account=account)
    assert clip.copies == ["hunter2"]
    err = capsys.readouterr().err
    assert "copy username" not in err and "copy MFA token" not in err
    assert "  p  copy password\n" in err


def test_unknown_keys_are_ignored(capsys) -> None:
    result, clip, _, _ = _menu(["a", "\n", "\x1b[A", "U", " ", "x"])
    assert result is MenuExit.DONE
    assert clip.copies == []
    err = capsys.readouterr().err
    assert err.endswith("  x  exit\n")


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("x", MenuExit.DONE),
        ("q", MenuExit.DONE),
        ("\x1b", MenuExit.DONE),
        ("\x03", MenuExit.ABORT),
        ("\x04", MenuExit.ABORT),
    ],
)
def test_exit_keys(key: str, expected: MenuExit) -> None:
    result, _, _, _ = _menu([key])
    assert result is expected


def test_keyboard_interrupt_is_abort() -> None:
    def interrupt():
        raise KeyboardInterrupt

    result = run_login_menu(
        ACCOUNT,
        "aws-sso",
        open_browser=False,
        read_key=interrupt,
        isatty=lambda: True,
    )
    assert result is MenuExit.ABORT


def test_exit_label(capsys) -> None:
    _menu(["x"], exit_label="continue to new values")
    assert "  x  continue to new values\n" in capsys.readouterr().err


def test_failed_copy_names_field_never_value(capsys) -> None:
    clip = _Clipboard(accepts=False)
    _menu(["u", "p", "t", "x"], clip=clip)
    err = capsys.readouterr().err
    assert "username not copied (no clipboard available)\n" in err
    assert "password not copied (no clipboard available)\n" in err
    assert "MFA token not copied (no clipboard available)\n" in err
    _assert_no_secret(err)
    assert otp.totp(SECRET, at=10.0) not in err


def test_no_url_still_shows_menu(capsys) -> None:
    account = {k: v for k, v in ACCOUNT.items() if k != "url"}
    result, _, opened, _ = _menu(["x"], account=account)
    assert result is MenuExit.DONE
    assert opened.calls == []
    err = capsys.readouterr().err
    assert err.startswith("no login url; open the site yourself\naws-sso — press a key:\n")


def test_login_url_is_not_read(capsys) -> None:
    # `login_url` was the accounts/ field; the effective login has `url`.
    account = {"id": "me", "login_url": "https://example.com/login"}
    _, _, opened, _ = _menu(["x"], account=account)
    assert opened.calls == []
    assert "no login url" in capsys.readouterr().err


def test_no_open_skips_browser(capsys) -> None:
    _, _, opened, _ = _menu(["x"], open_browser=False)
    assert opened.calls == []
    assert "no login url" not in capsys.readouterr().err


def test_not_a_tty_raises_before_opening() -> None:
    opened = _Recorder()
    with pytest.raises(NotATTY):
        run_login_menu(
            ACCOUNT,
            "aws-sso",
            open_browser=True,
            open_url=opened,
            isatty=lambda: False,
        )
    assert opened.calls == []


def test_invalid_mfa_reported_on_t_and_menu_stays_up(capsys) -> None:
    account = {"password": "hunter2", "mfa": "not!base32"}
    result, clip, _, _ = _menu(["t", "p", "x"], account=account)
    assert result is MenuExit.DONE
    assert clip.copies == ["hunter2"]
    err = capsys.readouterr().err
    assert "error: aws-sso: mfa secret is not valid base32\n" in err
    assert "not!base32" not in err


# --- clearing the clipboard ----------------------------------------------


def test_menu_clears_only_at_process_exit(exit_hooks: list) -> None:
    _, clip, _, _ = _menu(["p", "t", "x"])
    assert clip.clears == 0
    assert exit_hooks == [login_menu.clear_clipboard]


def test_exit_hook_is_registered_once(exit_hooks: list) -> None:
    _menu(["x"])
    _menu(["x"])
    assert exit_hooks == [login_menu.clear_clipboard]


def test_clear_clipboard_at_exit_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each registration yields mid-call, so without the lock every thread
    # passes the check before any of them sets the flag.
    registered: list = []

    def register(callback) -> None:
        threading.Event().wait(0.05)
        registered.append(callback)

    monkeypatch.setattr(login_menu, "_at_exit", register)
    threads = [threading.Thread(target=login_menu.clear_clipboard_at_exit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert registered == [login_menu.clear_clipboard]


def test_failed_exit_hook_registration_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list = []
    outcomes = [RuntimeError("register failed"), None]

    def register(callback) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        registered.append(callback)

    monkeypatch.setattr(login_menu, "_at_exit", register)
    with pytest.raises(RuntimeError):
        login_menu.clear_clipboard_at_exit()
    login_menu.clear_clipboard_at_exit()
    login_menu.clear_clipboard_at_exit()
    assert registered == [login_menu.clear_clipboard]


def test_not_a_tty_registers_no_exit_hook(exit_hooks: list) -> None:
    with pytest.raises(NotATTY):
        _menu(["x"], isatty=False)
    assert exit_hooks == []


def test_clear_clipboard_is_unconditional(capsys) -> None:
    clip = _Clipboard()
    login_menu.clear_clipboard(clip.clear)
    assert clip.clears == 1
    assert capsys.readouterr().err == "clipboard cleared\n"


def test_clear_clipboard_that_fails_says_so(capsys) -> None:
    login_menu.clear_clipboard(lambda: False)
    assert capsys.readouterr().err == "clipboard not cleared (no clipboard available)\n"


def test_clear_clipboard_that_raises_does_not_propagate(capsys) -> None:
    def boom() -> bool:
        raise OSError("gone")

    login_menu.clear_clipboard(boom)
    assert capsys.readouterr().err == "clipboard not cleared (no clipboard available)\n"


# --- read_key --------------------------------------------------------------


@pytest.fixture
def pty_pair():
    master, slave = os.openpty()
    yield master, slave
    os.close(master)
    os.close(slave)


def _type_later(master: int, data: bytes) -> None:
    """Send ``data`` once read_key has put the pty in cbreak, as a keypress would."""
    threading.Timer(0.1, os.write, (master, data)).start()


def test_read_key_reads_one_key_without_enter(pty_pair) -> None:
    master, slave = pty_pair
    before = termios.tcgetattr(slave)
    os.write(master, b"p")  # typed ahead, in canonical mode: still read at once
    assert login_menu.read_key(slave) == "p"
    # With ISIG on, the line discipline would turn Ctrl-C into a signal.
    _type_later(master, b"\x03")
    assert login_menu.read_key(slave) == "\x03"
    assert termios.tcgetattr(slave) == before


def test_read_key_returns_escape_sequence_whole(pty_pair) -> None:
    master, slave = pty_pair
    os.write(master, b"\x1b[A")
    assert login_menu.read_key(slave) == "\x1b[A"
    os.write(master, b"\x1b")
    assert login_menu.read_key(slave) == "\x1b"


def test_read_key_restores_mode_on_exception(pty_pair, monkeypatch) -> None:
    _, slave = pty_pair
    before = termios.tcgetattr(slave)

    def boom(fd, n):
        raise OSError("read failed")

    monkeypatch.setattr(login_menu.os, "read", boom)
    with pytest.raises(OSError):
        login_menu.read_key(slave)
    assert termios.tcgetattr(slave) == before


# --- km login --------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = LoadedConfig(
        path=tmp_path / "config.yaml",
        project_dir=tmp_path,
        directories=Directories(),
    )
    monkeypatch.setattr(_login, "resolve_config_path", lambda explicit: str(cfg.path))
    monkeypatch.setattr(_login, "load_config", lambda path: cfg)
    monkeypatch.setattr(_login, "resolve_project", lambda: Project(dir=tmp_path))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    return manifests


def _args(slug="aws-sso", no_open=False) -> argparse.Namespace:
    return argparse.Namespace(config=None, slug=slug, no_open=no_open)


def _manifest(login: str, password: str | None = None) -> str:
    """A manifest with ``login: <login>`` (flow yaml) and an optional password."""
    body = f"service: x\nlogin: {login}\n"
    if password is not None:
        body += f"secrets:\n  password: {password}\n"
    return body


def _login_run(args, keys=("x",), *, isatty=True, clip=None):
    clip = clip or _Clipboard()
    opened = _Recorder()
    it = iter(keys)
    rc = _login.run(
        args,
        read_key=lambda: next(it),
        copy=clip.copy,
        open_url=opened,
        now=lambda: 10.0,
        sleep=_Recorder(),
        isatty=lambda: isatty,
    )
    return rc, clip, opened


def test_km_login_happy_path(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(
        _manifest(f"{{id: me, mfa: {SECRET}, url: 'https://example.com/'}}", "hunter2")
    )
    rc, clip, opened = _login_run(_args(), keys=("u", "p", "t", "x"))
    assert rc == 0
    assert opened.calls == ["https://example.com/"]
    assert clip.copies == ["me", "hunter2", otp.totp(SECRET, at=10.0)]
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("aws-sso — press a key:\n")
    _assert_no_secret(err)


def test_km_login_from_uses_the_referenced_login_and_its_password(project: Path) -> None:
    (project / "apple-user.yaml").write_text(
        _manifest("{id: me, url: 'https://appleid.apple.com/'}", "hunter2")
    )
    (project / "apple-api.yaml").write_text(
        "service: x\nlogin: {from: apple-user}\nsecrets:\n  password: not-this-one\n"
    )
    rc, clip, opened = _login_run(_args("apple-api"), keys=("u", "p", "x"))
    assert rc == 0
    assert opened.calls == ["https://appleid.apple.com/"]
    assert clip.copies == ["me", "hunter2"]


def test_km_login_password_must_be_a_string(project: Path, capsys) -> None:
    # A non-string password is not part of the effective login.
    (project / "aws-sso.yaml").write_text(_manifest("{url: 'https://example.com/'}", "123"))
    rc, _, opened = _login_run(_args())
    assert rc == 1
    assert opened.calls == []
    assert "no non-empty string" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["x", "\x03", "\x04"])
def test_km_login_exits_zero_for_done_and_abort(project: Path, key: str) -> None:
    (project / "aws-sso.yaml").write_text(_manifest("{id: me}", "hunter2"))
    rc, _, _ = _login_run(_args(), keys=(key,))
    assert rc == 0


def test_km_login_no_open(project: Path) -> None:
    (project / "aws-sso.yaml").write_text(_manifest("{id: me, url: 'https://example.com/'}"))
    rc, _, opened = _login_run(_args(no_open=True))
    assert rc == 0
    assert opened.calls == []


def test_km_login_clears_at_exit(project: Path, exit_hooks: list) -> None:
    (project / "aws-sso.yaml").write_text(_manifest("{id: me}", "hunter2"))
    rc, _, _ = _login_run(_args(), keys=("p", "x"))
    assert rc == 0
    assert exit_hooks == [login_menu.clear_clipboard]


def test_km_login_no_usable_fields(project: Path, capsys) -> None:
    path = project / "aws-sso.yaml"
    path.write_text(_manifest("{url: 'https://example.com/', id: ''}", "''"))
    rc, _, opened = _login_run(_args())
    assert rc == 1
    assert opened.calls == []
    assert capsys.readouterr().err == (
        f"error: {path.resolve()}: no non-empty string `login.id`, `login.mfa` or "
        "`secrets.password`\n"
    )


def test_km_login_not_a_tty(project: Path, capsys) -> None:
    (project / "aws-sso.yaml").write_text(_manifest("{id: me, url: 'https://example.com/'}"))
    rc, _, opened = _login_run(_args(), isatty=False)
    assert rc == 1
    assert opened.calls == []
    assert capsys.readouterr().err == "error: km login needs an interactive terminal\n"


def _populate(project: Path) -> None:
    (project / "a.yaml").write_text(_manifest("{id: me}"))
    (project / "b.yaml").write_text(_manifest("{from: a}"))
    (project / "c.yaml").write_text(_manifest("{url: 'https://example.com/'}"))
    (project / "jwt.yaml").write_text("service: jwt\nsecrets:\n  key: hunter2\n")
    (project / "notes-only.yaml").write_text("service: x\nnotes: {id: me}\n")
    (project / "bad.yaml").write_text(_manifest("{id: me, hint: x}"))
    (project / "sequence.yaml").write_text("- item\n")
    (project / "broken.yaml").write_text("x: [\n")
    (project / "empty.yaml").write_text("")
    (project / "_template.yaml").write_text(_manifest("{id: me}"))


@pytest.mark.parametrize("slug", ["nope", "jwt", "notes-only"])
def test_km_login_unknown_or_login_less_slug_lists_logins(project: Path, slug: str, capsys) -> None:
    _populate(project)
    rc, _, _ = _login_run(_args(slug))
    assert rc == 1
    err = capsys.readouterr().err
    assert err == f"error: no login {slug!r}; logins: a, b, c\n"
    assert "hunter2" not in err


@pytest.mark.parametrize("slug", ["../x", "a\\b", ".hidden", "_template"])
def test_km_login_rejects_unsafe_names(project: Path, slug: str, capsys) -> None:
    (project / "_template.yaml").write_text(_manifest("{id: me}"))
    rc, _, _ = _login_run(_args(slug))
    assert rc == 1
    assert capsys.readouterr().err == f"error: no login {slug!r}; logins: none\n"


def test_km_login_missing_manifests_dir(project: Path, capsys) -> None:
    project.rmdir()
    rc, _, _ = _login_run(_args())
    assert rc == 1
    assert capsys.readouterr().err == "error: no login 'aws-sso'; logins: none\n"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("account: {id: me}\n", "`account:` was renamed to `login:`"),
        (_manifest("{id: me, hint: hunter2}"), "unknown key(s) hint"),
        (_manifest("{from: nope}"), "`login.from` names no manifest ('nope')"),
    ],
)
def test_km_login_malformed_login_names_keys_not_values(
    project: Path, body: str, message: str, capsys
) -> None:
    path = project / "aws-sso.yaml"
    path.write_text(body)
    rc, _, opened = _login_run(_args())
    assert rc == 1
    assert opened.calls == []
    err = capsys.readouterr().err
    assert err.startswith(f"error: {path.resolve()}: ")
    assert message in err
    assert "hunter2" not in err


def test_km_login_not_a_mapping(project: Path, capsys) -> None:
    path = project / "aws-sso.yaml"
    path.write_text("- a\n- b\n")
    rc, _, _ = _login_run(_args())
    assert rc == 1
    assert capsys.readouterr().err == f"error: {path.resolve()}: not a YAML mapping\n"


def test_km_login_yaml_parse_error(project: Path, capsys) -> None:
    path = project / "aws-sso.yaml"
    path.write_text("a: [\n")
    rc, _, _ = _login_run(_args())
    assert rc == 1
    assert capsys.readouterr().err == f"error: {path.resolve()}: yaml parse error\n"


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("injected"),
        PermissionError("injected"),
        IsADirectoryError("injected"),
        OSError("injected"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["missing", "permission", "directory", "os-error", "not-utf8"],
)
def test_km_login_read_failure_is_a_controlled_error(
    project: Path, exc: Exception, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    path = project / "aws-sso.yaml"
    path.write_text(_manifest("{id: me}", "hunter2"))
    real = Path.read_text

    def fail(self, *args, **kwargs):
        if self == path.resolve():
            raise exc
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail)
    rc, _, opened = _login_run(_args())
    assert rc == 1
    assert opened.calls == []
    assert capsys.readouterr().err == f"error: {path.resolve()}: cannot read file\n"


def test_cli_registers_login() -> None:
    from km.cli import build_parser

    args = build_parser().parse_args(["login", "aws-sso", "--no-open"])
    assert args.slug == "aws-sso"
    assert args.no_open
