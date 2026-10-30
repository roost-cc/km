"""Unit tests for km.manifest_io."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
import yaml

from km.manifest_io import effective_login, load_manifest, login_errors, write_secrets

SAMPLE_MANIFEST = """# top-level comment
service: acme
secrets:
  api_key: OLD_KEY  # inline comment
  api_secret: OLD_SECRET
infisical:
  dev:
    api_key: /acme/api_key
    api_secret: /acme/api_secret
rotation:
  runbook: runbooks/r.md
  groups:
    acme-creds:
      max_age_days: 90
      last_rotated: null
      producer: interactive
      secrets:
        - name: api_key
        - name: api_secret
"""


def test_write_secrets_updates_values_and_last_rotated(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(SAMPLE_MANIFEST)

    write_secrets(
        manifest,
        "acme-creds",
        {"api_key": "NEW_KEY", "api_secret": "NEW_SECRET"},
    )

    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"]["api_key"] == "NEW_KEY"
    assert data["secrets"]["api_secret"] == "NEW_SECRET"
    assert data["rotation"]["groups"]["acme-creds"]["last_rotated"] == dt.date.today().isoformat()


def test_write_secrets_preserves_top_level_comment(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(SAMPLE_MANIFEST)
    write_secrets(manifest, "acme-creds", {"api_key": "NEW"})
    text = manifest.read_text()
    assert "# top-level comment" in text


def test_write_secrets_preserves_inline_comment(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(SAMPLE_MANIFEST)
    write_secrets(manifest, "acme-creds", {"api_key": "NEW"})
    text = manifest.read_text()
    assert "# inline comment" in text


def test_write_secrets_creates_secrets_block_if_absent(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(
        "service: acme\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 90\n"
        "      last_rotated: null\n"
        "      producer: ed25519\n"
        "      secrets:\n"
        "        - name: api_key\n"
    )
    write_secrets(manifest, "g", {"api_key": "VALUE"})
    data = yaml.safe_load(manifest.read_text())
    assert data["secrets"] == {"api_key": "VALUE"}


def test_write_secrets_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(SAMPLE_MANIFEST)
    write_secrets(manifest, "acme-creds", {"api_key": "NEW"})
    assert not (tmp_path / "acme.yaml.tmp").exists()


def test_write_secrets_cleans_up_tmp_on_dump_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(SAMPLE_MANIFEST)

    import km.manifest_io as mio

    real_round_trip = mio._round_trip_yaml

    def boom_yaml() -> object:
        rt = real_round_trip()

        def dump(*_a, **_k):
            raise RuntimeError("disk full")

        rt.dump = dump  # type: ignore[assignment]
        return rt

    monkeypatch.setattr(mio, "_round_trip_yaml", boom_yaml)

    with pytest.raises(RuntimeError, match="disk full"):
        write_secrets(manifest, "acme-creds", {"api_key": "NEW"})
    assert not (tmp_path / "acme.yaml.tmp").exists()
    # Original manifest is untouched.
    assert "OLD_KEY" in manifest.read_text()


def test_write_secrets_rejects_missing_rotation_block(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text("service: acme\nsecrets:\n  api_key: OLD\n")
    with pytest.raises(ValueError, match="missing `rotation:` block"):
        write_secrets(manifest, "g", {"api_key": "NEW"})


def test_write_secrets_rejects_unknown_group(tmp_path: Path) -> None:
    manifest = tmp_path / "acme.yaml"
    manifest.write_text(SAMPLE_MANIFEST)
    with pytest.raises(ValueError, match="rotation.groups\\['nope'\\] is missing"):
        write_secrets(manifest, "nope", {"api_key": "NEW"})


# ---------- login: and notes: (F-56 D2) ----------


def _manifests(tmp_path: Path, **files: str) -> Path:
    """A manifests dir holding ``<slug>.yaml`` for each keyword (``_`` → ``-``)."""
    d = tmp_path / "manifests"
    d.mkdir(exist_ok=True)
    for slug, body in files.items():
        (d / f"{slug.replace('_', '-')}.yaml").write_text(body)
    return d


def _parsed(body: str) -> dict:
    return yaml.safe_load(body)


def test_load_manifest_takes_no_accounts_dir(tmp_path: Path) -> None:
    d = _manifests(tmp_path, x="service: x\n")
    with pytest.raises(TypeError):
        load_manifest(d / "x.yaml", accounts_dir=tmp_path)  # type: ignore[call-arg]


def test_load_manifest_leaves_login_and_notes_unexpanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `$` may legitimately be part of a login field or a note.
    monkeypatch.delenv("FOO", raising=False)
    d = _manifests(
        tmp_path,
        x="service: x\nlogin:\n  id: 'has $FOO inside'\nnotes:\n  hint: '${FOO}'\n",
    )
    data = load_manifest(d / "x.yaml")
    assert data is not None
    assert data["login"] == {"id": "has $FOO inside"}
    assert data["notes"] == {"hint": "${FOO}"}


def test_no_login_is_none_and_valid(tmp_path: Path) -> None:
    d = _manifests(tmp_path)
    parsed = _parsed("service: x\nsecrets:\n  password: pw\n")
    assert login_errors(parsed, d) == []
    assert effective_login(parsed, d) is None


def test_inline_login_plus_own_password(tmp_path: Path) -> None:
    d = _manifests(tmp_path)
    parsed = _parsed(
        "login:\n  url: https://x/\n  id: me\n  mfa: ABC\nsecrets:\n  password: pw\n  other: o\n"
    )
    assert effective_login(parsed, d) == {
        "url": "https://x/",
        "id": "me",
        "mfa": "ABC",
        "password": "pw",
    }


@pytest.mark.parametrize("login", ["{url: https://x/}", "{id: me}", "{mfa: ABC}"])
def test_inline_login_any_subset(tmp_path: Path, login: str) -> None:
    d = _manifests(tmp_path)
    parsed = _parsed(f"login: {login}\n")
    assert login_errors(parsed, d) == []
    assert effective_login(parsed, d) == parsed["login"]


@pytest.mark.parametrize("password", ["123", "~", "[a]", "{a: b}"])
def test_non_string_password_is_left_out(tmp_path: Path, password: str) -> None:
    d = _manifests(tmp_path)
    parsed = _parsed(f"login: {{id: me}}\nsecrets:\n  password: {password}\n")
    assert effective_login(parsed, d) == {"id": "me"}


def test_from_reads_the_referenced_login_and_its_password(tmp_path: Path) -> None:
    d = _manifests(
        tmp_path,
        apple_user="login:\n  url: https://appleid/\n  id: me\nsecrets:\n  password: theirs\n",
    )
    parsed = _parsed("login:\n  from: apple-user\nsecrets:\n  password: mine\n")
    assert login_errors(parsed, d) == []
    assert effective_login(parsed, d) == {
        "url": "https://appleid/",
        "id": "me",
        "password": "theirs",
    }


def test_from_referenced_without_password_has_none(tmp_path: Path) -> None:
    d = _manifests(tmp_path, apple_user="login: {id: me}\n")
    parsed = _parsed("login: {from: apple-user}\nsecrets:\n  password: mine\n")
    assert effective_login(parsed, d) == {"id": "me"}


def test_from_reads_the_referenced_manifest_unexpanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAR", raising=False)
    d = _manifests(
        tmp_path,
        apple_user="service: $BAR\nlogin: {id: '$BAR'}\nsecrets:\n  password: 'x$BAR'\n",
    )
    assert effective_login(_parsed("login: {from: apple-user}\n"), d) == {
        "id": "$BAR",
        "password": "x$BAR",
    }


def test_notes_may_be_empty_and_hold_anything(tmp_path: Path) -> None:
    d = _manifests(tmp_path)
    assert login_errors(_parsed("notes: {}\n"), d) == []
    parsed = _parsed("login: {id: me}\nnotes:\n  backup_codes: [1, 2]\n  sso: {a: b}\n")
    assert login_errors(parsed, d) == []
    # km reads nothing from notes:
    assert effective_login(parsed, d) == {"id": "me"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("account: {id: me}\n", "`account:` was renamed to `login:`"),
        ("notes: [a]\n", "`notes:` must be a mapping"),
        ("notes: text\n", "`notes:` must be a mapping"),
        ("login: me\n", "`login:` must be a mapping"),
        ("login: ~\n", "`login:` must be a mapping"),
        ("login: {}\n", "`login: {}` is empty"),
        ("login: {id: me, password: pw}\n", "unknown key(s) password"),
        ("login: {login_url: https://x/}\n", "unknown key(s) login_url"),
        ("login: {id: 42}\n", "`login.id` must be a string"),
        ("login: {mfa: ~}\n", "`login.mfa` must be a string"),
        ("login: {url: [a]}\n", "`login.url` must be a string"),
        ("login: {from: apple-user, id: me}\n", "`login.from` stands alone (also has: id)"),
        ("login: {from: nope}\n", "`login.from` names no manifest ('nope')"),
        ("login: {from: 42}\n", "`login.from` names no manifest (42)"),
        ("login: {from: ../apple-user}\n", "`login.from` names no manifest"),
        ("login: {from: nested}\n", "manifest 'nested' has `login.from` itself"),
        ("login: {from: plain}\n", "manifest 'plain' has no `login:` mapping"),
        ("login: {from: malformed}\n", "manifest 'malformed' has a malformed `login:`"),
    ],
)
def test_login_rejections(tmp_path: Path, body: str, message: str) -> None:
    d = _manifests(
        tmp_path,
        apple_user="login: {id: me}\n",
        nested="login: {from: apple-user}\n",
        plain="service: plain\n",
        malformed="login: {id: me, hint: x}\n",
    )
    parsed = _parsed(body)
    errors = login_errors(parsed, d)
    assert any(message in e for e in errors), errors
    with pytest.raises(ValueError, match=re.escape(message)):
        effective_login(parsed, d)


def test_from_never_names_the_manifest_template(tmp_path: Path) -> None:
    # `_template.yaml` sits in manifests/ but is not a manifest, so no
    # login may come from it.
    d = _manifests(tmp_path)
    (d / "_template.yaml").write_text("login: {id: me}\n")
    parsed = _parsed("login: {from: _template}\n")
    assert login_errors(parsed, d) == ["`login.from` names no manifest ('_template')"]


def test_rejections_never_name_values(tmp_path: Path) -> None:
    d = _manifests(tmp_path)
    parsed = _parsed("login: {id: hunter2, password: hunter2, mfa: 42}\naccount: {x: hunter2}\n")
    errors = login_errors(parsed, d)
    assert errors
    assert not any("hunter2" in e for e in errors)
