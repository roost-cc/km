"""Unit tests for km.producers.interactive."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from km.login_menu import MenuExit, NotATTY
from km.producers import interactive as interactive_producer
from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    ProducerContractError,
    Secret,
)
from km.producers.interactive import Interactive


def _entry(name: str, manifest: Path, group: str = "g") -> ManifestEntry:
    return ManifestEntry(name=name, format=None, group_name=group, manifest_path=manifest)


def _stub_input(answers: list[str]):
    answers_iter = iter(answers)

    def _input(prompt: str = "") -> str:
        return next(answers_iter)

    return _input


def _ctx(
    *,
    manifest: Mapping[str, Any] | None = None,
    current_secrets: Mapping[str, str] | None = None,
    login: Mapping[str, Any] | None = None,
) -> ProduceContext:
    return ProduceContext(
        manifest=manifest or {},
        current_secrets=current_secrets or {},
        group={},
        login=login,
    )


def test_paste_back_collects_values(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")
    entries = [_entry("api_key", manifest), _entry("api_secret", manifest)]
    monkeypatch.setattr("builtins.input", _stub_input(["NEW_KEY", "NEW_SECRET"]))
    out = Interactive({}).produce(entries, _ctx(current_secrets={"api_key": "OLD"}))
    assert out == [
        Secret(name="api_key", value="NEW_KEY"),
        Secret(name="api_secret", value="NEW_SECRET"),
    ]


def test_empty_value_aborts_group(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")
    entries = [_entry("api_key", manifest)]
    monkeypatch.setattr("builtins.input", _stub_input([""]))
    with pytest.raises(ProducerContractError, match="empty value"):
        Interactive({}).produce(entries, _ctx())


def test_no_entries_returns_empty(tmp_path: Path) -> None:
    out = Interactive({}).produce([], _ctx())
    assert out == []


_LOGIN = {
    "url": "https://example.com/login",
    "id": "ops@acme.example",
    "password": "SECRET",
}


def test_login_menu_skipped_without_url(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")

    calls: list[dict] = []
    monkeypatch.setattr(
        interactive_producer,
        "run_login_menu",
        lambda login, *a, **kw: calls.append(login),
    )
    monkeypatch.setattr("builtins.input", _stub_input(["NEW"]))

    out = Interactive({}).produce(
        [_entry("api_key", manifest)],
        _ctx(login={"id": "ops@acme.example"}),
    )
    assert out == [Secret(name="api_key", value="NEW")]
    assert calls == []


def test_login_menu_skipped_for_blank_url(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")

    calls: list[dict] = []
    monkeypatch.setattr(
        interactive_producer,
        "run_login_menu",
        lambda login, *a, **kw: calls.append(login),
    )
    monkeypatch.setattr("builtins.input", _stub_input(["NEW"]))

    out = Interactive({}).produce(
        [_entry("api_key", manifest)],
        _ctx(login={"id": "ops@acme.example", "url": "  "}),
    )
    assert out == [Secret(name="api_key", value="NEW")]
    assert calls == []


def test_login_menu_runs_when_url_present(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")

    calls: list[tuple] = []

    def _menu(*args, **kwargs):
        calls.append((args, kwargs))
        return MenuExit.DONE

    monkeypatch.setattr(interactive_producer, "run_login_menu", _menu)
    monkeypatch.setattr("builtins.input", _stub_input(["NEW_KEY"]))

    out = Interactive({}).produce([_entry("api_key", manifest)], _ctx(login=_LOGIN))
    assert calls == [
        (
            (_LOGIN, "acme"),
            {
                "open_browser": True,
                "exit_label": "continue to new values",
            },
        )
    ]
    assert out == [Secret(name="api_key", value="NEW_KEY")]


def test_login_menu_ignores_the_manifest_blocks(tmp_path: Path, monkeypatch) -> None:
    # The menu reads the effective login (ctx.login), never `account:` /
    # `login:` / `login_url` off the raw manifest.
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")
    calls: list[dict] = []
    monkeypatch.setattr(
        interactive_producer,
        "run_login_menu",
        lambda login, *a, **kw: calls.append(login),
    )
    monkeypatch.setattr("builtins.input", _stub_input(["NEW"]))
    raw = {
        "account": {"login_url": "https://example.com/login"},
        "login": {"url": "https://example.com/login"},
    }
    Interactive({}).produce([_entry("api_key", manifest)], _ctx(manifest=raw))
    assert calls == []


def test_login_menu_never_prints_a_value_that_failed_to_copy(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run the real menu with no clipboard: the old login step printed the
    # value when a copy failed, password included.
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")
    opened: list[str] = []
    keys = iter(["u", "p", "x"])
    real_menu = interactive_producer.run_login_menu

    def _menu(*args, **kwargs):
        return real_menu(
            *args,
            **kwargs,
            read_key=lambda: next(keys),
            copy=lambda v: False,
            open_url=lambda u: opened.append(u) or True,
            isatty=lambda: True,
        )

    monkeypatch.setattr(interactive_producer, "run_login_menu", _menu)
    monkeypatch.setattr("builtins.input", _stub_input(["NEW_KEY"]))

    out = Interactive({}).produce([_entry("api_key", manifest)], _ctx(login=_LOGIN))
    assert opened == ["https://example.com/login"]
    assert out == [Secret(name="api_key", value="NEW_KEY")]
    captured = capsys.readouterr()
    assert "password not copied" in captured.err
    assert "SECRET" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("menu_outcome", "message"),
    [
        (MenuExit.ABORT, "login aborted"),
        (NotATTY("stdin is not a terminal"), "needs an interactive terminal"),
    ],
)
def test_login_menu_abort_or_no_tty_aborts_group(
    tmp_path: Path, monkeypatch, menu_outcome, message: str
) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\n")

    def _menu(*args, **kwargs):
        if isinstance(menu_outcome, Exception):
            raise menu_outcome
        return menu_outcome

    def _no_input(prompt: str = "") -> str:
        raise AssertionError("paste-back must not run after the login aborts")

    monkeypatch.setattr(interactive_producer, "run_login_menu", _menu)
    monkeypatch.setattr("builtins.input", _no_input)

    with pytest.raises(ProducerContractError, match=message):
        Interactive({}).produce([_entry("api_key", manifest)], _ctx(login=_LOGIN))


def test_print_runbook_resolves_via_project_dir(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Mirror the real layout: project dir contains config.yaml +
    # runbooks/<rb>.md, manifest under <project>/vendors/.
    project = tmp_path / "roost"
    (project / "vendors").mkdir(parents=True)
    (project / "config.yaml").write_text("{}\n")
    runbooks = project / "runbooks"
    runbooks.mkdir()
    (runbooks / "rotate-vendor-api-key.md").write_text(
        "## Per-cred sections\n\n### twilio\n\nDo the twilio dance.\n"
    )
    manifest = project / "vendors" / "twilio.yaml"
    manifest.write_text("service: twilio\n")

    monkeypatch.setattr("builtins.input", _stub_input(["NEW_KEY"]))

    Interactive({}).produce(
        [_entry("api_key", manifest, group="twilio")],
        _ctx(
            manifest={
                "rotation": {
                    "runbook": "runbooks/rotate-vendor-api-key.md#twilio",
                }
            },
            current_secrets={"api_key": "OLD"},
        ),
    )
    out = capsys.readouterr().out
    assert "Do the twilio dance." in out
    assert "runbook section #twilio" in out


def test_print_runbook_warns_when_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "x.yaml"
    manifest.write_text("service: x\n")
    interactive_producer._print_runbook(manifest, "runbooks/nope.md")
    assert "warn: runbook not found" in capsys.readouterr().err


def test_extract_anchor_section_basic() -> None:
    md = """## Per-cred sections

### aws-root

Steps for aws-root.
- Click here.

### aws-sso

Different stuff.
"""
    out = interactive_producer._extract_anchor_section(md, "aws-root")
    assert out is not None
    assert "Steps for aws-root." in out
    assert "aws-sso" not in out


def test_extract_anchor_section_case_insensitive() -> None:
    md = "### AWS-Root\n\nbody\n"
    assert "body" in (interactive_producer._extract_anchor_section(md, "aws-root") or "")


def test_extract_anchor_section_missing() -> None:
    assert interactive_producer._extract_anchor_section("### other\n\nbody\n", "missing") is None
