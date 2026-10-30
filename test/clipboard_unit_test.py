"""Unit tests for km.clipboard clear() and copy()."""

from __future__ import annotations

import base64
import subprocess

import pytest

from km import clipboard

SECRET = "hunter2-páss"


@pytest.fixture(autouse=True)
def plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TMUX", "KITTY_WINDOW_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


class _Run:
    """Stands in for subprocess.run: records argv and returns or raises ``result``."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if isinstance(self.result, BaseException):
            raise self.result
        return subprocess.CompletedProcess(argv, 0, stdout=self.result, stderr=b"")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- clear() --------------------------------------------------------------


class _Tty:
    def __init__(self) -> None:
        self.written = ""

    def __call__(self, path, mode, encoding=None):
        assert path == "/dev/tty"
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def write(self, s: str) -> None:
        self.written += s

    def flush(self) -> None:
        pass


def test_clear_under_tmux_sends_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    tty = _Tty()
    monkeypatch.setattr(clipboard, "open", tty, raising=False)
    run = _Run(b"")
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert clipboard.clear() is True
    assert tty.written == "\033Ptmux;\033\033]52;c;\a\033\\"
    assert run.calls == []


def test_clear_under_kitty_sends_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    tty = _Tty()
    monkeypatch.setattr(clipboard, "open", tty, raising=False)
    assert clipboard.clear() is True
    assert tty.written == "\033]52;c;\a"


def test_clear_osc52_without_tty_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")

    def no_tty(*a, **kw):
        raise OSError("no tty")

    monkeypatch.setattr(clipboard, "open", no_tty, raising=False)
    assert clipboard.clear() is False


def test_clear_falls_back_to_wl_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(b"")
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert clipboard.clear() is True
    argv, kw = run.calls[0]
    assert argv == ["wl-copy", "--clear"]
    assert kw["timeout"] == clipboard._WL_TIMEOUT


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("wl-copy"),
        subprocess.TimeoutExpired(["wl-copy"], 1.0),
        subprocess.CalledProcessError(1, ["wl-copy"]),
    ],
    ids=["missing-tool", "timeout", "error-exit"],
)
def test_clear_wl_copy_failure_returns_false(monkeypatch: pytest.MonkeyPatch, failure) -> None:
    monkeypatch.setattr(clipboard.subprocess, "run", _Run(failure))
    assert clipboard.clear() is False


# --- copy() keeps the value off the command line ----------------------------


def test_copy_wl_copy_passes_value_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(b"")
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert clipboard.copy(SECRET) is True
    argv, kw = run.calls[0]
    assert argv == ["wl-copy"]
    assert kw["input"] == SECRET.encode("utf-8")


def test_copy_under_kitty_writes_osc52(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-kitty")
    tty = _Tty()
    monkeypatch.setattr(clipboard, "open", tty, raising=False)
    assert clipboard.copy(SECRET) is True
    assert tty.written == f"\033]52;c;{_b64(SECRET)}\a"
