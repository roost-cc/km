"""Unit tests for `km rotate` (single-manifest dispatch + batch sweep)."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pytest
import yaml

from km.commands import _rotate
from km.commands._rotate import _due_groups
from km.config import Directories, LoadedConfig


def _cfg(tmp_path: Path) -> LoadedConfig:
    return LoadedConfig(
        path=tmp_path / "config.yaml",
        project_dir=tmp_path,
        directories=Directories(),
    )


def _make_args(**kw) -> argparse.Namespace:
    defaults: dict = {
        "config": None,
        "slug": None,
        "project": None,
        "env": None,
        "force": None,
        "dry_run": False,
        "revoke_old": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _make_project(tmp_path: Path) -> Path:
    """Create a project directory containing a minimal `config.yaml`."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "config.yaml").write_text("{}\n")
    (proj / "manifests").mkdir()
    return proj


def _config_path(proj: Path) -> Path:
    return proj / "config.yaml"


# --- _due_groups ---


def test_due_groups_yields_never_rotated() -> None:
    parsed = {
        "rotation": {
            "groups": {
                "g": {
                    "max_age_days": 30,
                    "last_rotated": None,
                    "producer": "ed25519",
                    "secrets": [{"name": "x"}],
                }
            }
        }
    }
    assert [name for name, _ in _due_groups(parsed)] == ["g"]


def test_due_groups_yields_when_overdue() -> None:
    long_ago = (dt.date.today() - dt.timedelta(days=100)).isoformat()
    parsed = {
        "rotation": {
            "groups": {
                "g": {
                    "max_age_days": 30,
                    "last_rotated": long_ago,
                    "producer": "ed25519",
                    "secrets": [{"name": "x"}],
                }
            }
        }
    }
    assert [name for name, _ in _due_groups(parsed)] == ["g"]


def test_due_groups_skips_when_fresh() -> None:
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    parsed = {
        "rotation": {
            "groups": {
                "g": {
                    "max_age_days": 90,
                    "last_rotated": yesterday,
                    "producer": "ed25519",
                    "secrets": [{"name": "x"}],
                }
            }
        }
    }
    assert list(_due_groups(parsed)) == []


def test_due_groups_skips_when_max_age_zero() -> None:
    parsed = {
        "rotation": {
            "groups": {
                "g": {
                    "max_age_days": 0,
                    "last_rotated": None,
                    "producer": "static",
                    "secrets": [{"name": "x"}],
                }
            }
        }
    }
    assert list(_due_groups(parsed)) == []


def test_due_groups_force_overrides_cadence() -> None:
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    parsed = {
        "rotation": {
            "groups": {
                "g": {
                    "max_age_days": 90,
                    "last_rotated": yesterday,
                    "producer": "ed25519",
                    "secrets": [{"name": "x"}],
                }
            }
        }
    }
    assert [name for name, _ in _due_groups(parsed, force="g")] == ["g"]


def test_due_groups_force_filters_to_named_group() -> None:
    parsed = {
        "rotation": {
            "groups": {
                "a": {
                    "max_age_days": 90,
                    "last_rotated": None,
                    "producer": "ed25519",
                    "secrets": [{"name": "x"}],
                },
                "b": {
                    "max_age_days": 90,
                    "last_rotated": None,
                    "producer": "ed25519",
                    "secrets": [{"name": "y"}],
                },
            }
        }
    }
    assert [name for name, _ in _due_groups(parsed, force="a")] == ["a"]


# --- _collect_results ---


def test_collect_results_happy_path(tmp_path: Path) -> None:
    from km.producers.contract import Secret

    out = _rotate._collect_results(
        [Secret(name="a", value="1"), Secret(name="b", value="2")],
        {"a", "b"},
        manifest_path=tmp_path / "m.yaml",
        gname="g",
    )
    assert out == {"a": "1", "b": "2"}


def test_collect_results_accepts_glob_pattern(tmp_path: Path) -> None:
    """Producers may declare allowed names via globs (e.g. ``account_*``)
    so a synthetic name returned by produce() is accepted."""
    from km.producers.contract import Secret

    out = _rotate._collect_results(
        [
            Secret(name="account_private_key", value="PRIV"),
            Secret(name="account_public_key", value="PUB"),
        ],
        {"account_*"},
        manifest_path=tmp_path / "m.yaml",
        gname="g",
    )
    assert out == {"account_private_key": "PRIV", "account_public_key": "PUB"}


def test_collect_results_rejects_non_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _rotate._collect_results(
        ["bad"],  # not a Secret
        {"a"},
        manifest_path=tmp_path / "m.yaml",
        gname="g",
    )
    assert out is None
    assert "non-Secret" in capsys.readouterr().err


def test_collect_results_rejects_unknown_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from km.producers.contract import Secret

    out = _rotate._collect_results(
        [Secret(name="rogue", value="x")],
        {"a"},
        manifest_path=tmp_path / "m.yaml",
        gname="g",
    )
    assert out is None
    assert "not declared" in capsys.readouterr().err


# --- the slug positional ---


def test_an_unknown_slug_is_a_usage_error_listing_the_slugs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = _make_project(tmp_path)
    (proj / "manifests" / "google.yaml").write_text("service: google\n")
    (proj / "manifests" / "jwt.yaml").write_text("service: jwt\n")
    (proj / "manifests" / "_template.yaml").write_text("service: example\n")
    for slug in ("nope", "_template", "manifests/google.yaml"):
        with pytest.raises(SystemExit) as e:
            _rotate.run(_make_args(slug=slug, config=str(_config_path(proj))))
        assert e.value.code == 2
        assert f"no manifest {slug!r} (have: google, jwt)" in capsys.readouterr().err


def test_a_nested_file_is_not_a_slug(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    proj = _make_project(tmp_path)
    (proj / "manifests" / "accounts").mkdir()
    (proj / "manifests" / "accounts" / "apple.yaml").write_text("service: apple\n")
    for slug in ("apple", "accounts/apple"):
        with pytest.raises(SystemExit) as e:
            _rotate.run(_make_args(slug=slug, config=str(_config_path(proj))))
        assert e.value.code == 2


def test_the_slug_resolves_from_anywhere_through_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _make_project(tmp_path)
    (proj / "manifests" / "s.yaml").write_text(
        "service: s\nsecrets:\n  k: OLD\nrotation:\n  runbook: r.md\n  groups:\n"
        "    g:\n      max_age_days: 90\n      producer: static\n      secrets:\n"
        "        - name: k\n"
    )
    monkeypatch.chdir(tmp_path)
    rc = _rotate.run(_make_args(slug="s", config=str(_config_path(proj)), force="g"))
    assert rc == 0


# --- _run_single ---


def test_run_single_login_without_rotation_exits_2(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "aws-root.yaml"
    manifest.write_text("service: aws\nlogin:\n  id: root\n  mfa: SECRET\n")
    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj))))
    assert rc == 2


def test_run_single_static_producer_passthrough(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "static.yaml"
    manifest.write_text(
        "service: thing\n"
        "secrets:\n"
        "  api_key: KEEP_ME\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 90\n"
        "      last_rotated: null\n"
        "      producer: static\n"
        "      secrets:\n"
        "        - name: api_key\n"
    )
    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj)), force="g"))
    assert rc == 0
    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"]["api_key"] == "KEEP_ME"
    assert data["rotation"]["groups"]["g"]["last_rotated"] == dt.date.today().isoformat()


def test_run_single_interactive_producer_writes_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "acme.yaml"
    manifest.write_text(
        "service: acme\n"
        "login:\n"
        "  id: ops@acme.example\n"
        "secrets:\n"
        "  api_key: OLD\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    acme:\n"
        "      max_age_days: 365\n"
        "      last_rotated: null\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: api_key\n"
    )

    inputs = iter(["NEW_KEY"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj))))
    assert rc == 0
    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"]["api_key"] == "NEW_KEY"


_LOGIN_ONLY = (
    "service: apple\n"
    "login:\n"
    "  url: https://appleid.apple.com/\n"
    "  id: me@example.com\n"
    "notes:\n"
    "  backup_codes: [1, 2]\n"
    "secrets:\n"
    "  password: OLD\n"
    "rotation:\n"
    "  runbook: r.md\n"
    "  groups:\n"
    "    password:\n"
    "      max_age_days: 365\n"
    "      last_rotated: null\n"
    "      producer: interactive\n"
    "      secrets:\n"
    "        - name: password\n"
)


def _record_menu(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from km.login_menu import MenuExit
    from km.producers import interactive as interactive_producer

    seen: list[dict] = []

    def menu(login, label, **kwargs):
        seen.append(dict(login))
        return MenuExit.DONE

    monkeypatch.setattr(interactive_producer, "run_login_menu", menu)
    return seen


def test_run_single_login_only_manifest_writes_only_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A login-only manifest has no publishers and no target lists it
    # (F-56 D2); rotating it writes the manifest and nothing else.
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "apple-user.yaml"
    manifest.write_text(_LOGIN_ONLY)
    before = {p.relative_to(proj) for p in proj.rglob("*")}
    seen = _record_menu(monkeypatch)
    inputs = iter(["NEW_PW"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj))))
    assert rc == 0
    assert seen == [
        {"url": "https://appleid.apple.com/", "id": "me@example.com", "password": "OLD"}
    ]
    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"]["password"] == "NEW_PW"
    assert data["notes"] == {"backup_codes": [1, 2]}
    assert {p.relative_to(proj) for p in proj.rglob("*")} == before
    assert not (proj / "accounts").exists()


def test_run_single_login_from_shows_the_referenced_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _make_project(tmp_path)
    (proj / "manifests" / "apple-user.yaml").write_text(_LOGIN_ONLY)
    manifest = proj / "manifests" / "apple-api.yaml"
    manifest.write_text(
        "service: apple\n"
        "login: {from: apple-user}\n"
        "secrets:\n"
        "  api_key: OLD\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    key:\n"
        "      max_age_days: 365\n"
        "      last_rotated: null\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: api_key\n"
    )
    seen = _record_menu(monkeypatch)
    inputs = iter(["NEW_KEY"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj))))
    assert rc == 0
    assert seen == [
        {"url": "https://appleid.apple.com/", "id": "me@example.com", "password": "OLD"}
    ]
    assert yaml.safe_load(manifest.read_text())["secrets"]["api_key"] == "NEW_KEY"


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("account:\n  id: me\n", "`account:` was renamed to `login:`"),
        ("login:\n  from: nope\n", "`login.from` names no manifest"),
    ],
)
def test_run_single_malformed_login_fails_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block: str, message: str
) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "x.yaml"
    body = (
        "service: x\n" + block + "secrets:\n  k: v\nrotation:\n  runbook: r.md\n"
        "  groups:\n    g:\n      max_age_days: 90\n      producer: static\n"
        "      secrets:\n        - name: k\n"
    )
    manifest.write_text(body)
    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj)), force="g"))
    assert rc == 1
    assert message in capsys.readouterr().err
    assert manifest.read_text() == body


def test_run_single_ed25519_producer_writes_private_key(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "jwt.yaml"
    manifest.write_text(
        "service: jwt\n"
        "secrets:\n"
        "  account_private_key: null\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    account-private-key:\n"
        "      max_age_days: 90\n"
        "      last_rotated: null\n"
        "      producer: ed25519\n"
        "      secrets:\n"
        "        - name: account_private_key\n"
        "          format: ed25519\n"
    )
    rc = _rotate.run(
        _make_args(
            slug=manifest.stem,
            config=str(_config_path(proj)),
            force="account-private-key",
        )
    )
    assert rc == 0
    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"]["account_private_key"].startswith("-----BEGIN PRIVATE KEY-----")


def test_run_single_dry_run_does_not_write(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "static.yaml"
    manifest.write_text(
        "service: thing\n"
        "secrets:\n"
        "  api_key: ORIGINAL\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 90\n"
        "      last_rotated: null\n"
        "      producer: uuid\n"
        "      secrets:\n"
        "        - name: api_key\n"
        "          format: uuid\n"
    )
    rc = _rotate.run(
        _make_args(
            slug=manifest.stem,
            config=str(_config_path(proj)),
            force="g",
            dry_run=True,
        )
    )
    assert rc == 0
    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"]["api_key"] == "ORIGINAL"


def test_run_single_force_unknown_group_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "x.yaml"
    manifest.write_text(
        "service: x\n"
        "secrets:\n"
        "  k: v\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    real-group:\n"
        "      max_age_days: 90\n"
        "      last_rotated: null\n"
        "      producer: static\n"
        "      secrets:\n"
        "        - name: k\n"
    )
    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj)), force="typo"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "--force value 'typo' matches no rotation group" in err


def test_run_single_unknown_producer_fails(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    manifest = proj / "manifests" / "x.yaml"
    manifest.write_text(
        "service: x\n"
        "secrets:\n"
        "  k: v\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 90\n"
        "      last_rotated: null\n"
        "      producer: nope\n"
        "      secrets:\n"
        "        - name: k\n"
    )
    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj)), force="g"))
    assert rc == 1


# --- _run_batch ---


def test_run_batch_skips_interactive_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _make_project(tmp_path)
    # Login-only manifest without rotation: silently skipped.
    (proj / "manifests" / "login-only.yaml").write_text("service: aws\nlogin:\n  id: root\n")
    # Interactive: silently skipped in batch.
    (proj / "manifests" / "interactive.yaml").write_text(
        "service: acme\n"
        "secrets:\n"
        "  api_key: OLD\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 365\n"
        "      last_rotated: null\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: api_key\n"
    )
    # uuid: minted.
    (proj / "manifests" / "auto.yaml").write_text(
        "service: thing\n"
        "secrets:\n"
        "  k: null\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      last_rotated: null\n"
        "      producer: uuid\n"
        "      secrets:\n"
        "        - name: k\n"
        "          format: uuid\n"
    )

    def _no_input(prompt=""):
        raise AssertionError("interactive should not run in batch mode")

    monkeypatch.setattr("builtins.input", _no_input)
    monkeypatch.chdir(proj)
    # Batch rotation reads only manifests/*.yaml: a due group anywhere else
    # in the project (a nested file, a stray yaml at the root) is left alone.
    stray = (
        "service: stray\nsecrets:\n  k: null\nrotation:\n  runbook: r.md\n  groups:\n"
        "    g:\n      max_age_days: 30\n      producer: uuid\n      secrets:\n"
        "        - name: k\n          format: uuid\n"
    )
    (proj / "manifests" / "accounts").mkdir()
    (proj / "manifests" / "accounts" / "nested.yaml").write_text(stray)
    (proj / "stray.yaml").write_text(stray)

    rc = _rotate.run(_make_args(config=str(_config_path(proj))))
    assert rc == 0
    auto = yaml.safe_load((proj / "manifests" / "auto.yaml").read_text())
    assert auto["secrets"]["k"]  # minted
    interactive = yaml.safe_load((proj / "manifests" / "interactive.yaml").read_text())
    assert interactive["secrets"]["api_key"] == "OLD"  # untouched
    assert (proj / "manifests" / "accounts" / "nested.yaml").read_text() == stray
    assert (proj / "stray.yaml").read_text() == stray


def test_run_batch_force_bypasses_producer_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An operator-named group should run in batch mode regardless of
    # producer — `--force` overrides the interactive/static skip.
    proj = _make_project(tmp_path)
    (proj / "manifests" / "interactive.yaml").write_text(
        "service: acme\n"
        "secrets:\n"
        "  api_key: OLD\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    forced-group:\n"
        "      max_age_days: 365\n"
        "      last_rotated: null\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: api_key\n"
    )

    inputs = iter(["NEW_KEY"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.chdir(proj)

    rc = _rotate.run(_make_args(config=str(_config_path(proj)), force="forced-group"))
    assert rc == 0
    out = yaml.safe_load((proj / "manifests" / "interactive.yaml").read_text())
    assert out["secrets"]["api_key"] == "NEW_KEY"


def test_rotate_calls_no_publisher(tmp_path: Path, monkeypatch, capsys) -> None:
    """F-54 Decision Q2: rotation changes the manifest only. Even a producer
    that removes names (formerly propagated to every publisher) leaves the
    stores alone; `km publish` is the only command that writes to one."""
    from km import loader

    def _explode(*_a, **_kw):
        raise AssertionError("km rotate must not load a publisher")

    monkeypatch.setattr(loader, "load_publisher", _explode)
    monkeypatch.setattr(loader, "publisher_class", _explode)

    proj = _make_project(tmp_path)
    (proj / "producers").mkdir()
    (proj / "producers" / "pruning.py").write_text(
        "from km.producers.contract import Producer, ProduceResult, Secret\n"
        "class Pruning(Producer):\n"
        "    def produce(self, entries, ctx):\n"
        "        return ProduceResult(secrets=(Secret('key_new', 'NEW'),), remove=('key_old',))\n"
    )
    manifest = proj / "manifests" / "keys.yaml"
    manifest.write_text(
        "service: keys\n"
        "secrets:\n"
        "  key_old: OLD\n"
        "publishers:\n"
        "  infisical:\n"
        "    dev:\n"
        "      'key_*': /keys/\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    keys:\n"
        "      max_age_days: 30\n"
        "      producer: pruning\n"
        "      secrets:\n"
        "        - name: key_new\n"
    )
    rc = _rotate.run(_make_args(slug=manifest.stem, config=str(_config_path(proj)), force="keys"))
    assert rc == 0
    assert yaml.safe_load(manifest.read_text())["secrets"] == {"key_new": "NEW"}
    assert "km publish <target>" in capsys.readouterr().out
