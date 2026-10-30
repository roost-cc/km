"""Unit tests for TemplateFilePublisher (validate / publish / collect)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from km.publishers import ManifestRef
from km.publishers.template_file import TemplateFilePublisher


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a fake project root with templates/, and a secrets/ root beside it."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "templates" / "infisical.tmpl.env").write_text(
        "DB_CONNECTION_URI=postgresql://infisical:${password}@postgres:5432/infisical\n"
    )
    return tmp_path


def _pub(repo: Path) -> TemplateFilePublisher:
    pub = TemplateFilePublisher({"root": str(repo / "secrets")})
    pub.templates_root = (repo / "templates").resolve()
    return pub


def _section(template: str = "infisical.tmpl.env") -> list[dict]:
    return [
        {
            "template": template,
            "output": "infisical.{env}.env",
            "envs": {
                "dev": {"infisical_password": "password"},
            },
        }
    ]


# --- validate ---


def test_validate_rejects_non_list_section(repo: Path) -> None:
    pub = _pub(repo)
    errs = pub.validate(manifest_path=Path("x.yaml"), section={})
    assert any("must be a non-empty list of specs" in e for e in errs)


def test_validate_rejects_empty_list_section(repo: Path) -> None:
    pub = _pub(repo)
    errs = pub.validate(manifest_path=Path("x.yaml"), section=[])
    assert any("must be a non-empty list of specs" in e for e in errs)


def test_validate_rejects_missing_template_file(repo: Path) -> None:
    pub = _pub(repo)
    errs = pub.validate(
        manifest_path=Path("x.yaml"),
        section=_section(template="missing.tmpl.env"),
    )
    assert any("file does not exist" in e for e in errs)


def test_validate_rejects_template_outside_repo_root(repo: Path) -> None:
    pub = _pub(repo)
    errs = pub.validate(
        manifest_path=Path("x.yaml"),
        section=_section(template="../escape.tmpl.env"),
    )
    assert any("escapes templates_root" in e for e in errs)


def test_validate_rejects_output_outside_repo_root(repo: Path) -> None:
    pub = _pub(repo)
    section = [
        {
            "template": "infisical.tmpl.env",
            "output": "../etc/foo.{env}.env",
            "envs": {"dev": {"infisical_password": "password"}},
        }
    ]
    errs = pub.validate(manifest_path=Path("x.yaml"), section=section)
    assert any(".output" in e and "escapes the destination's root" in e for e in errs)


def test_validate_rejects_absolute_output(repo: Path) -> None:
    """An output lands under the destination's root; an absolute one would
    put a rendered file wherever the manifest says."""
    pub = _pub(repo)
    section = [
        {
            "template": "infisical.tmpl.env",
            "output": "/tmp/foo.{env}.env",
            "envs": {"dev": {"infisical_password": "password"}},
        }
    ]
    errs = pub.validate(manifest_path=Path("x.yaml"), section=section)
    assert any(".output" in e and "relative to the destination's root" in e for e in errs)


def test_schema_instance_validates_without_a_destination(repo: Path) -> None:
    pub = TemplateFilePublisher.schema_instance()
    pub.templates_root = (repo / "templates").resolve()
    assert pub.validate(manifest_path=Path("x.yaml"), section=_section()) == []
    assert not pub.remote and not pub.applies()


def test_validate_rejects_local_name_missing_from_secrets(repo: Path) -> None:
    """Vendor-style: when secrets: is non-empty, every template_file
    local_name must resolve to a key in secrets:. Mirrors the
    InfisicalPublisher's cross-reference message."""
    pub = _pub(repo)
    errs = pub.validate(
        manifest_path=Path("x.yaml"),
        section=_section(),
        secrets={"some_other_secret": "v"},
    )
    assert any(
        "template_file entry for 'infisical_password' has no matching secret" in e for e in errs
    )


def test_validate_skips_secrets_check_for_system_style_manifest(
    repo: Path,
) -> None:
    """Empty secrets: = system-style manifest (values minted at rotation).
    The publisher must not flag missing local_names — that's the rotation
    cross-reference walk's job in _validate.py."""
    pub = _pub(repo)
    errs = pub.validate(
        manifest_path=Path("x.yaml"),
        section=_section(),
        secrets={},
    )
    assert errs == []


def test_validate_rejects_template_var_mismatch_in_env_body(repo: Path) -> None:
    pub = _pub(repo)
    section = [
        {
            "template": "infisical.tmpl.env",
            "output": "infisical.{env}.env",
            "envs": {
                "dev": {"infisical_password": "wrong_var"},
            },
        }
    ]
    errs = pub.validate(manifest_path=Path("x.yaml"), section=section)
    # Missing template ${password}, plus extra binding to 'wrong_var'.
    assert any("template var ${password} has no entry" in e for e in errs)
    assert any("'wrong_var' which is not present" in e for e in errs)


def test_validate_rejects_duplicate_template_var_in_env_body(repo: Path) -> None:
    (repo / "templates" / "two.tmpl.env").write_text("A=${x}\nB=${y}\n")
    pub = _pub(repo)
    section = [
        {
            "template": "two.tmpl.env",
            "output": "two.{env}.env",
            "envs": {
                "dev": {"a": "x", "b": "x"},  # both bind to ${x}
            },
        }
    ]
    errs = pub.validate(manifest_path=Path("x.yaml"), section=section)
    assert any("template var 'x' is bound to" in e for e in errs)


def test_validate_accepts_well_formed_section(repo: Path) -> None:
    pub = _pub(repo)
    assert pub.validate(manifest_path=Path("x.yaml"), section=_section()) == []


# --- publish ---


def test_publish_renders_atomic(repo: Path, tmp_path: Path) -> None:
    pub = _pub(repo)
    result = pub.publish(
        manifest_path=tmp_path / "postgresql.yaml",
        env="dev",
        secrets={"infisical_password": "SECRET"},
        section=_section(),
        dry_run=False,
    )
    assert result.written == 1
    assert result.skipped == 0
    assert result.failed == 0
    out = (repo / "secrets" / "infisical.dev.env").read_text(encoding="utf-8")
    assert out == "DB_CONNECTION_URI=postgresql://infisical:SECRET@postgres:5432/infisical\n"
    # No leftover .tmp.
    leftovers = list((repo / "secrets").glob("*.tmp"))
    assert leftovers == []


def test_publish_gives_the_output_a_plain_open_mode(repo: Path, tmp_path: Path) -> None:
    """The staged output keeps the umask's mode, as a plain write gave it:
    a container reading the file as another user (Synapse) still can."""
    old = os.umask(0o022)
    try:
        result = _pub(repo).publish(
            manifest_path=tmp_path / "postgresql.yaml",
            env="dev",
            secrets={"infisical_password": "SECRET"},
            section=_section(),
            dry_run=False,
        )
    finally:
        os.umask(old)
    assert result.written == 1
    assert stat.S_IMODE((repo / "secrets" / "infisical.dev.env").stat().st_mode) == 0o644


def test_publish_renders_in_full_only_the_files_that_use_a_selected_name(
    repo: Path, tmp_path: Path
) -> None:
    """Two files from one manifest; `names` selects the one using `api_key`,
    which is rendered in full (its unselected `db_password` too)."""
    (repo / "templates" / "app.tmpl.env").write_text("API=${api}\nDB=${db}\n")
    section = _section() + [
        {
            "template": "app.tmpl.env",
            "output": "app.{env}.env",
            "envs": {"dev": {"api_key": "api", "infisical_password": "db"}},
        }
    ]
    secrets = {"infisical_password": "SECRET", "api_key": "KEY"}
    result = _pub(repo).publish(
        manifest_path=tmp_path / "m.yaml",
        env="dev",
        secrets=secrets,
        section=section,
        dry_run=False,
        names={"api_key"},
    )
    assert (result.written, result.skipped, result.failed) == (1, 0, 0)
    assert (repo / "secrets" / "app.dev.env").read_text() == "API=KEY\nDB=SECRET\n"
    assert not (repo / "secrets" / "infisical.dev.env").exists()

    result = _pub(repo).publish(
        manifest_path=tmp_path / "m.yaml",
        env="dev",
        secrets=secrets,
        section=section,
        dry_run=False,
        names={"infisical_password"},
    )
    assert result.written == 2  # both files use it


def test_publish_dry_run_writes_nothing(repo: Path, tmp_path: Path, capsys) -> None:
    pub = _pub(repo)
    result = pub.publish(
        manifest_path=tmp_path / "postgresql.yaml",
        env="dev",
        secrets={"infisical_password": "SECRET"},
        section=_section(),
        dry_run=True,
    )
    assert result.written == 1
    assert not (repo / "secrets" / "infisical.dev.env").exists()
    err = capsys.readouterr().err
    assert "[dry-run] would render env=dev" in err


def test_publish_skips_empty_secret_value(repo: Path, tmp_path: Path, capsys) -> None:
    pub = _pub(repo)
    result = pub.publish(
        manifest_path=tmp_path / "postgresql.yaml",
        env="dev",
        secrets={"infisical_password": ""},
        section=_section(),
        dry_run=False,
    )
    assert result.written == 0
    assert result.skipped == 1
    assert result.failed == 0
    assert not (repo / "secrets" / "infisical.dev.env").exists()
    err = capsys.readouterr().err
    assert "empty; skipped" in err


def test_publish_skips_spec_when_env_not_in_envs_map(repo: Path, tmp_path: Path) -> None:
    pub = _pub(repo)
    result = pub.publish(
        manifest_path=tmp_path / "postgresql.yaml",
        env="prod",  # spec only declares dev
        secrets={"infisical_password": "SECRET"},
        section=_section(),
        dry_run=False,
    )
    assert (result.written, result.skipped, result.failed) == (0, 0, 0)
    assert not (repo / "secrets" / "infisical.prod.env").exists()


# --- collect ---


def _ref(repo: Path) -> ManifestRef:
    return ManifestRef(
        path=repo / "roost" / "internal" / "postgresql.yaml",
        secrets={"infisical_password": "SECRET"},
        section=_section(),
    )


def test_collect_emits_same_for_matching_render(repo: Path) -> None:
    pub = _pub(repo)
    pub.publish(
        manifest_path=repo / "postgresql.yaml",
        env="dev",
        secrets={"infisical_password": "SECRET"},
        section=_section(),
        dry_run=False,
    )
    res = pub.collect(env="dev", manifests=[_ref(repo)])
    assert len(res.entries) == 1
    e = res.entries[0]
    assert e.state == "same"
    assert e.store_address == "infisical.dev.env:password"
    assert e.local_name == "infisical_password"


def test_collect_emits_differs_for_drifted_render(repo: Path) -> None:
    (repo / "secrets" / "infisical.dev.env").write_text("DB_CONNECTION_URI=tampered\n")
    pub = _pub(repo)
    res = pub.collect(env="dev", manifests=[_ref(repo)])
    assert len(res.entries) == 1
    e = res.entries[0]
    assert e.state == "differs"
    # env-file drift is whole-file; we don't pretend to know which entry
    # diverged, so manifest_value stays unset to keep audit's verbose
    # suffix from printing "manifest='X' store='X'" for an honest drift.
    assert e.manifest_value is None
    assert e.value == "SECRET"


def test_collect_emits_manifest_only_for_missing_output(repo: Path) -> None:
    pub = _pub(repo)
    res = pub.collect(env="dev", manifests=[_ref(repo)])
    assert len(res.entries) == 1
    assert res.entries[0].state == "manifest_only"


def test_collect_skips_specs_without_current_env(repo: Path) -> None:
    pub = _pub(repo)
    res = pub.collect(env="prod", manifests=[_ref(repo)])
    assert res.entries == []


def test_collect_warns_when_render_fails_but_file_exists(repo: Path, capsys) -> None:
    """When the expected render can't be computed (missing secret) but the
    on-disk file exists, the row reports `differs` and a stderr warning
    names the manifest + spec so the operator sees why it drifted."""
    (repo / "secrets" / "infisical.dev.env").write_text("DB_CONNECTION_URI=anything\n")
    pub = _pub(repo)
    ref = ManifestRef(
        path=repo / "roost" / "internal" / "postgresql.yaml",
        secrets={},  # missing infisical_password — render can't compute
        section=_section(),
    )
    res = pub.collect(env="dev", manifests=[ref])
    assert len(res.entries) == 1
    assert res.entries[0].state == "differs"
    err = capsys.readouterr().err
    assert "expected render failed" in err
    assert "infisical.dev.env" in err
    assert "postgresql.yaml" in err


# --- ensure ---


def test_ensure_creates_the_output_directories(tmp_path: Path) -> None:
    pub = TemplateFilePublisher({"root": str(tmp_path / "files")})
    pub.templates_root = (tmp_path / "templates").resolve()
    refs = [
        ManifestRef(
            path=Path("m.yaml"),
            secrets={"k": "v"},
            section=[
                {"template": "a.tmpl", "output": "svc/a.{env}.env", "envs": {"dev": {"k": "k"}}},
                {"template": "b.tmpl", "output": "other/b.{env}.env", "envs": {"test": {"k": "k"}}},
            ],
        )
    ]
    dry = pub.ensure(env="dev", manifests=refs, dry_run=True)
    assert dry.created == (f"directory {tmp_path.resolve() / 'files' / 'svc'}",)
    assert not (tmp_path / "files").exists()
    result = pub.ensure(env="dev", manifests=refs, dry_run=False)
    assert result.created == dry.created
    assert (tmp_path / "files" / "svc").is_dir()
    assert not (tmp_path / "files" / "other").exists()
    assert pub.ensure(env="dev", manifests=refs, dry_run=False).created == ()
