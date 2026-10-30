"""Unit tests for `km validate` schema checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.commands._validate import _validate_file
from km.config import Directories, LoadedConfig
from km.project import Project
from km.publishers.infisical import InfisicalPublisher


def _cfg(project_dir: Path) -> LoadedConfig:
    return LoadedConfig(
        path=project_dir / "config.yaml",
        project_dir=project_dir,
        directories=Directories(),
    )


# The schema gate checks sections with the target's publisher instances;
# infisical needs a destination's `host` to construct. validate() itself
# never opens an HTTP client.
_TARGET_PUBLISHERS = {"infisical": InfisicalPublisher({"host": "http://x"})}


def _project(tmp_path: Path) -> tuple[Project, LoadedConfig]:
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "config.yaml").write_text("{}\n")
    runbooks = proj_dir / "runbooks"
    runbooks.mkdir()
    (runbooks / "r.md").write_text("# stub\n")
    project = Project(dir=proj_dir.resolve())
    return project, _cfg(proj_dir.resolve())


def _write(proj: Project, name: str, body: str) -> Path:
    p = proj.dir / name
    p.write_text(body)
    return p


def _errs(p: Path, project: Project, cfg: LoadedConfig) -> list[str]:
    return _validate_file(p, project, cfg=cfg, publishers=_TARGET_PUBLISHERS)[0]


def test_login_only_manifest_is_valid(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "aws-root.yaml",
        "service: aws\n"
        "login:\n"
        "  url: https://signin.aws.amazon.com/console\n"
        "  id: root@example.com\n"
        "  mfa: SECRET\n"
        "notes: {}\n",
    )
    assert _errs(p, proj, cfg) == []


def test_login_with_password_and_rotation_is_valid(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "apple-user.yaml",
        "service: apple\n"
        "login: {id: me}\n"
        "notes:\n"
        "  backup_codes: [1, 2]\n"
        "secrets:\n"
        "  password: HUNTER2\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    password:\n"
        "      max_age_days: 0\n"
        "      producer: static\n"
        "      secrets:\n"
        "        - name: password\n",
    )
    assert _errs(p, proj, cfg) == []


def _manifests(proj: Project, **files: str) -> Path:
    d = proj.dir / "manifests"
    d.mkdir(exist_ok=True)
    for slug, body in files.items():
        (d / f"{slug.replace('_', '-')}.yaml").write_text(body)
    return d


def test_login_from_a_manifest_with_a_login_is_valid(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    d = _manifests(
        proj,
        apple_user="service: apple\nlogin: {id: me}\n",
        apple_api="service: apple\nlogin: {from: apple-user}\n",
    )
    assert _errs(d / "apple-api.yaml", proj, cfg) == []


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("account:\n  id: me\n", "`account:` was renamed to `login:`"),
        ("login: {}\n", "`login: {}` is empty"),
        ("login: [a]\n", "`login:` must be a mapping"),
        ("login: {id: me, hint: HUNTER2}\n", "unknown key(s) hint"),
        ("login: {mfa: 123}\n", "`login.mfa` must be a string"),
        ("login: {from: apple-user, url: x}\n", "`login.from` stands alone"),
        ("login: {from: nope}\n", "`login.from` names no manifest ('nope')"),
        ("login: {from: chained}\n", "manifest 'chained' has `login.from` itself"),
        ("notes: [HUNTER2]\n", "`notes:` must be a mapping"),
    ],
)
def test_login_and_notes_schema_errors_name_the_manifest(
    tmp_path: Path, block: str, message: str
) -> None:
    proj, cfg = _project(tmp_path)
    d = _manifests(
        proj,
        apple_user="service: apple\nlogin: {id: me}\n",
        chained="service: apple\nlogin: {from: apple-user}\n",
        bad="service: bad\n" + block,
    )
    errs = _errs(d / "bad.yaml", proj, cfg)
    assert [e for e in errs if message in e] == [
        e for e in errs if e.startswith("manifests/bad.yaml: ")
    ]
    assert any(message in e for e in errs), errs
    assert not any("HUNTER2" in e for e in errs)


_LISTED = (
    "service: published\nsecrets:\n  x: value\nrotation:\n  runbook: r.md\n"
    "  groups:\n    g:\n      max_age_days: 0\n      producer: static\n"
    "      secrets:\n        - name: x\n"
)


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("", None),
        ("account:\n  id: me\n", "`account:` was renamed to `login:`"),
        ("login: {}\n", "`login: {}` is empty"),
        ("login: [a]\n", "`login:` must be a mapping"),
        ("login: {id: me, hint: HUNTER2}\n", "unknown key(s) hint"),
        ("login: {mfa: 123}\n", "`login.mfa` must be a string"),
        ("login: {from: apple-user, url: x}\n", "`login.from` stands alone"),
        ("login: {from: nope}\n", "`login.from` names no manifest ('nope')"),
        ("login: {from: chained}\n", "manifest 'chained' has `login.from` itself"),
        ("login: {from: no-login}\n", "manifest 'no-login' has no `login:` mapping"),
        ("notes: [HUNTER2]\n", "`notes:` must be a mapping"),
        ("- a\n", "top-level must be a mapping"),
    ],
)
def test_validate_checks_login_schema_for_every_manifest(
    tmp_path: Path, monkeypatch, capsys, block: str, message: str | None
) -> None:
    """A manifest no target lists still has its `login:`/`notes:` checked
    by `km validate <target>`, whichever target."""
    import argparse

    from km.commands import _validate

    proj, _cfg_ = _project(tmp_path)
    _manifests(
        proj,
        published=_LISTED,
        apple_user="service: apple\nlogin: {id: me}\n",
        chained="service: apple\nlogin: {from: apple-user}\n",
        no_login="service: none\n",
        unlisted=block if block.startswith("-") else "service: unlisted\n" + block,
    )
    (proj.dir / "config.yaml").write_text(
        "environments: [dev]\ntargets:\n  local:\n    manifests: [published]\n"
        "    destinations:\n      - publisher: template_file\n        envs: [dev]\n"
        f"        root: {tmp_path / 'out'}\n"
    )
    monkeypatch.chdir(proj.dir)
    args = argparse.Namespace(
        config=str(proj.dir / "config.yaml"),
        target="local",
        envs=[],
        schema_only=True,
        json=False,
        verbose=False,
    )
    rc = _validate.run(args)
    out = capsys.readouterr().out
    if message is None:
        assert rc == 0, out
        return
    assert rc == 1, out
    assert any(
        line.strip().startswith("manifests/unlisted.yaml: ") and message in line
        for line in out.splitlines()
    ), out
    assert "HUNTER2" not in out


def test_km_account_publisher_is_unknown(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "secrets:\n"
        "  password: P\n"
        "publishers:\n"
        "  km_account:\n"
        "    password: password\n"
        "rotation:\n"
        "  runbook: r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 0\n"
        "      producer: static\n"
        "      secrets:\n"
        "        - name: password\n",
    )
    errs = _errs(p, proj, cfg)
    assert any(
        "publisher 'km_account'" in e and "no publisher 'km_account' found" in e for e in errs
    )


def test_login_only_with_secrets_and_no_rotation_is_invalid(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "bad.yaml",
        "service: bad\nsecrets:\n  k: v\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("login-only manifests must omit" in e for e in errs)


def test_uniform_rotation_with_known_producer_is_valid(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "ok.yaml",
        "service: ok\n"
        "secrets:\n"
        "  k: v\n"
        "publishers:\n"
        "  infisical:\n"
        "    dev:\n"
        "      k: /ok/k\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      last_rotated: null\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: k\n",
    )
    assert _errs(p, proj, cfg) == []


def test_unknown_producer_is_rejected(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "secrets:\n"
        "  k: v\n"
        "publishers:\n"
        "  infisical:\n"
        "    dev:\n"
        "      k: /x/k\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      last_rotated: null\n"
        "      producer: nope\n"
        "      secrets:\n"
        "        - name: k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("'nope'" in e for e in errs)


def test_partial_rotation_block_no_groups_is_rejected(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "secrets:\n"
        "  k: v\n"
        "publishers:\n"
        "  infisical:\n"
        "    dev:\n"
        "      k: /x/k\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("must be a non-empty mapping" in e for e in errs)


def test_partial_rotation_block_empty_group_is_rejected(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "secrets:\n"
        "  k: v\n"
        "publishers:\n"
        "  infisical:\n"
        "    dev:\n"
        "      k: /x/k\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("non-empty mapping" in e for e in errs)


def test_rotation_without_secrets_or_publishers_is_rejected(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      last_rotated: null\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("interactive rotation has nothing to rotate" in e for e in errs)


# --- S-372: --drift flag removal ---


def test_unknown_publisher_is_rejected(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\npublishers:\n  vault:\n    dev:\n      k: /x/k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("'vault'" in e for e in errs)


def test_publisher_validate_catches_bad_path(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "secrets:\n"
        "  k: v\n"
        "publishers:\n"
        "  infisical:\n"
        "    dev:\n"
        "      k: not-an-absolute-path\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("must be a string starting with '/'" in e for e in errs)


# --- S-378: template_file publisher validation ---


def _setup_env_template(tmp_path: Path) -> tuple[Project, LoadedConfig]:
    """Create a project with a template under templates/ and a files/ dir."""
    proj, cfg = _project(tmp_path)
    templates = proj.dir / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "infisical.tmpl.env").write_text(
        "DB_CONNECTION_URI=postgresql://infisical:${password}@postgres:5432/infisical\n"
    )
    (proj.dir / "files").mkdir(exist_ok=True)
    return proj, cfg


def test_template_file_publisher_validate_accepts_well_formed_section(tmp_path: Path) -> None:
    proj, cfg = _setup_env_template(tmp_path)
    p = _write(
        proj,
        "postgresql.yaml",
        "service: postgresql\n"
        "secrets:\n"
        "  infisical_password: pw\n"
        "publishers:\n"
        "  template_file:\n"
        "    - template: infisical.tmpl.env\n"
        "      output: infisical.{env}.env\n"
        "      envs:\n"
        "        dev:\n"
        "          infisical_password: password\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: infisical_password\n",
    )
    assert _errs(p, proj, cfg) == []


def test_template_file_publisher_validate_rejects_missing_template(tmp_path: Path) -> None:
    proj, cfg = _setup_env_template(tmp_path)
    p = _write(
        proj,
        "postgresql.yaml",
        "service: postgresql\n"
        "secrets:\n"
        "  k: v\n"
        "publishers:\n"
        "  template_file:\n"
        "    - template: missing.tmpl.env\n"
        "      output: missing.{env}.env\n"
        "      envs:\n"
        "        dev:\n"
        "          k: foo\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("file does not exist" in e for e in errs)


def test_template_file_publisher_validate_rejects_template_var_mismatch(tmp_path: Path) -> None:
    proj, cfg = _setup_env_template(tmp_path)
    p = _write(
        proj,
        "postgresql.yaml",
        "service: postgresql\n"
        "secrets:\n"
        "  infisical_password: pw\n"
        "publishers:\n"
        "  template_file:\n"
        "    - template: infisical.tmpl.env\n"
        "      output: infisical.{env}.env\n"
        "      envs:\n"
        "        dev:\n"
        "          infisical_password: not_password\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: infisical_password\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("${password}" in e for e in errs)


def test_template_file_unknown_publisher_in_publishers_block(tmp_path: Path) -> None:
    proj, cfg = _setup_env_template(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\npublishers:\n  not_a_publisher:\n    dev:\n      k: /x/k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("'not_a_publisher'" in e for e in errs)


def test_synapse_is_an_unknown_publisher(tmp_path: Path) -> None:
    """`yaml_file` replaced `synapse` (S-963)."""
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\npublishers:\n  synapse:\n    dev:\n      k: homeserver:k\n",
    )
    errs = _errs(p, proj, cfg)
    assert any("publisher 'synapse': no publisher 'synapse' found" in e for e in errs)


def test_yaml_file_query_parse_error_is_a_schema_error(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "x.yaml",
        "service: x\n"
        "secrets: {k: v}\n"
        "publishers:\n"
        "  yaml_file:\n"
        "    dev:\n"
        '      k: "config:$.targets[?@.x=="\n',
    )
    errs = [e for e in _errs(p, proj, cfg) if "does not parse" in e]
    assert len(errs) == 1, errs
    assert "x.yaml" in errs[0] and "['k']" in errs[0] and "config:" not in errs[0]


def test_rotation_cross_reference_resolves_via_template_file(tmp_path: Path) -> None:
    """System-style manifest declares secrets used by template_file's per-env body —
    cross-reference should accept it without a "neither in secrets nor publishers" error."""
    proj, cfg = _setup_env_template(tmp_path)
    p = _write(
        proj,
        "system.yaml",
        "service: system\n"
        "secrets:\n"
        "  infisical_password: pw\n"
        "publishers:\n"
        "  template_file:\n"
        "    - template: infisical.tmpl.env\n"
        "      output: infisical.{env}.env\n"
        "      envs:\n"
        "        dev:\n"
        "          infisical_password: password\n"
        "rotation:\n"
        "  runbook: runbooks/r.md\n"
        "  groups:\n"
        "    g:\n"
        "      max_age_days: 30\n"
        "      producer: interactive\n"
        "      secrets:\n"
        "        - name: infisical_password\n",
    )
    errs = _errs(p, proj, cfg)
    assert not any("is in neither secrets nor publishers" in e for e in errs), errs


def test_drift_flag_rejected_at_argparse(capsys) -> None:
    """`--drift` was retired in S-372; argparse rejects it with exit 2."""
    from km.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["validate", "local", "--drift"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "--drift" in err


# --- _validate_file return-shape contract (S-432) ---


def test_validate_file_returns_tuple_with_parsed_on_success(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(
        proj,
        "ok.yaml",
        "service: ok\nlogin:\n  id: v\n",
    )
    errs, parsed = _validate_file(p, proj, cfg=cfg)
    assert errs == []
    assert parsed is not None
    assert parsed["service"] == "ok"


def test_validate_file_returns_none_for_non_mapping_top_level(tmp_path: Path) -> None:
    proj, cfg = _project(tmp_path)
    p = _write(proj, "bad.yaml", "- a\n- b\n")  # list at top-level, not mapping
    errs, parsed = _validate_file(p, proj, cfg=cfg)
    assert errs
    assert parsed is None
