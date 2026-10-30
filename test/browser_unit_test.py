"""Unit tests for km.browser."""

from __future__ import annotations

import subprocess

import pytest

from km import browser


def test_open_url_launches_xdg_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    assert browser.open_url("https://example.test/login") is True
    assert calls == [["xdg-open", "https://example.test/login"]]


def test_open_url_without_xdg_open_warns_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def missing(argv, **kw):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(browser.subprocess, "Popen", missing)
    assert browser.open_url("https://example.test/login") is False
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "warn: xdg-open not found; open this URL manually: https://example.test/login\n"


def test_open_url_detaches_output(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs: dict = {}
    monkeypatch.setattr(browser.subprocess, "Popen", lambda argv, **kw: kwargs.update(kw))
    browser.open_url("https://example.test")
    assert kwargs == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
