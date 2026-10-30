"""Unit tests for km.manifest — env-parent shape under publishers.infisical."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.manifest import enumerate_secret_addresses, load_manifests


def _project_dirs(fixtures_dir: Path) -> list[Path]:
    return [fixtures_dir / "vendors", fixtures_dir / "roost"]


def test_emits_entries_across_both_directories(fixtures_dir: Path) -> None:
    entries = load_manifests(_project_dirs(fixtures_dir))
    # 2 vendor (google) + 2 jwt + 3 synapse = 7, each declared in `dev`
    assert len(entries) == 7
    for e in entries:
        assert e.env == "dev"
        assert e.path.startswith("/")
        assert isinstance(e.name, str) and e.name
        assert isinstance(e.max_age_days, int)
        assert isinstance(e.group, str)
        assert isinstance(e.producer, str)
        assert isinstance(e.upstream, str)


def test_skips_template_and_files_without_rotation_groups(fixtures_dir: Path) -> None:
    entries = load_manifests(_project_dirs(fixtures_dir))
    assert not any(e.upstream == "aws" for e in entries)
    assert not any(e.upstream == "_template" for e in entries)


def test_singleton_groups_use_path_name_key(fixtures_dir: Path) -> None:
    entries = load_manifests(_project_dirs(fixtures_dir))
    account = next(e for e in entries if e.path == "/jwt/account" and e.name == "private_key")
    assert account.group == "/jwt/account:private_key"

    shared = next(e for e in entries if e.path == "/matrix" and e.name == "shared_secret")
    assert shared.group == "/matrix:shared_secret"


def test_multi_secret_groups_keep_yaml_name(fixtures_dir: Path) -> None:
    entries = load_manifests(_project_dirs(fixtures_dir))
    bot = [e for e in entries if e.group == "synapse-bot"]
    assert len(bot) == 2
    assert sorted(e.name for e in bot) == ["as_token", "hs_token"]


def test_skips_local_only_secrets(fixtures_dir: Path) -> None:
    """Secrets in `secrets:` but not in any env's infisical map produce no entry."""
    entries = load_manifests(_project_dirs(fixtures_dir))
    assert not any(e.name == "dev_apikey" for e in entries)


def test_upstream_is_filename_stem(fixtures_dir: Path) -> None:
    entries = load_manifests(_project_dirs(fixtures_dir))
    synapse_entries = [e for e in entries if e.upstream == "synapse"]
    assert synapse_entries
    for e in synapse_entries:
        assert e.producer == "passphrase"
        assert e.source_file == "synapse.yaml"


def test_carries_project_label(fixtures_dir: Path) -> None:
    entries = load_manifests(_project_dirs(fixtures_dir))
    projects = {e.project for e in entries}
    assert projects == {"vendors", "roost"}


def test_throws_on_missing_project_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="project directory does not exist"):
        load_manifests([tmp_path / "nonexistent"])


def test_throws_on_duplicate_path_name_env(tmp_path: Path) -> None:
    """Two manifests declaring the same Infisical address in the same env
    are a real conflict."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    body = """secrets:
  k: v
publishers:
  infisical:
    dev:
      k: /shared/k
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      producer: vendor
      secrets:
        - name: k
"""
    (a / "x.yaml").write_text(body)
    (b / "y.yaml").write_text(body)
    with pytest.raises(ValueError, match="duplicate manifest entry"):
        load_manifests([a, b])


def test_same_path_different_env_is_not_duplicate(tmp_path: Path) -> None:
    """The same Infisical address can legitimately live in multiple envs
    (one entry per env, distinguishable by `env`)."""
    p = tmp_path / "proj"
    p.mkdir()
    (p / "x.yaml").write_text("""secrets:
  k: v
publishers:
  infisical:
    dev:
      k: /shared/k
    prod:
      k: /shared/k
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      producer: vendor
      secrets:
        - name: k
""")
    entries = load_manifests([p])
    assert {e.env for e in entries} == {"dev", "prod"}


def test_allows_missing_producer_at_load_time(tmp_path: Path) -> None:
    """Producer is required only by the Coordinator — the loader tolerates
    its absence so audit/push/validate can run on partial manifests.
    """
    p = tmp_path / "proj"
    p.mkdir()
    (p / "ok.yaml").write_text("""publishers:
  infisical:
    dev:
      x: /x/x
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      secrets:
        - name: x
""")
    entries = load_manifests([p])
    assert len(entries) == 1
    assert entries[0].producer == ""


def test_rejects_non_string_producer(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "bad.yaml").write_text("""publishers:
  infisical:
    dev:
      x: /x/x
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      producer: [not, a, string]
      secrets:
        - name: x
""")
    with pytest.raises(ValueError, match="must be a string"):
        load_manifests([p])


def test_rejects_negative_max_age_days(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "bad.yaml").write_text("""publishers:
  infisical:
    dev:
      x: /x/x
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: -1
      producer: vendor
      secrets:
        - name: x
""")
    with pytest.raises(ValueError, match="non-negative integer"):
        load_manifests([p])


def test_accepts_zero_max_age_days(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "static.yaml").write_text("""publishers:
  infisical:
    dev:
      team_id: /oauth_client/static/team_id
rotation:
  runbook: r.md
  groups:
    static-static:
      max_age_days: 0
      producer: vendor
      secrets:
        - name: team_id
""")
    entries = load_manifests([p])
    assert len(entries) == 1
    assert entries[0].max_age_days == 0


def test_recursive_glob(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    nested = p / "vendors" / "subdir"
    nested.mkdir(parents=True)
    (nested / "deep.yaml").write_text("""publishers:
  infisical:
    dev:
      k: /deep/k
rotation:
  runbook: r.md
  groups:
    g:
      max_age_days: 30
      producer: vendor
      secrets:
        - name: k
""")
    entries = load_manifests([p])
    assert len(entries) == 1
    assert entries[0].source_file == "vendors/subdir/deep.yaml"


# --- enumerate_secret_addresses: shape rejection ---


def test_enumerate_rejects_scalar_shape() -> None:
    """`publishers: { infisical: { name: /path } }` (no env) is rejected."""
    parsed = {"publishers": {"infisical": {"client_id": "/oauth/google/client_id"}}}
    with pytest.raises(ValueError, match="must be a mapping"):
        enumerate_secret_addresses(parsed, file_label="x.yaml")


def test_enumerate_env_parent_emits_one_per_env() -> None:
    parsed = {
        "publishers": {
            "infisical": {
                "dev": {
                    "client_id": "/oauth/google/client_id",
                    "client_secret": "/oauth/google/client_secret",
                },
                "prod": {
                    "client_id": "/oauth/google/client_id",
                    "client_secret": "/oauth/google/client_secret",
                },
            }
        }
    }
    addrs = enumerate_secret_addresses(parsed, file_label="x.yaml")
    assert sorted(addrs) == sorted(
        [
            ("client_id", "/oauth/google/client_id", "dev"),
            ("client_secret", "/oauth/google/client_secret", "dev"),
            ("client_id", "/oauth/google/client_id", "prod"),
            ("client_secret", "/oauth/google/client_secret", "prod"),
        ]
    )


def test_enumerate_skips_underscore_meta_keys() -> None:
    """`_owned` (and any other underscore-prefixed key) is meta, not an env."""
    parsed = {
        "publishers": {
            "infisical": {
                "dev": {"client_id": "/oauth/google/client_id"},
                "_owned": [{"path": "/jwt/*", "name_pattern": "pub_*"}],
            }
        }
    }
    addrs = enumerate_secret_addresses(parsed, file_label="x.yaml")
    assert addrs == [("client_id", "/oauth/google/client_id", "dev")]


def test_enumerate_handles_missing_publishers() -> None:
    """A manifest with no `publishers:` block produces no addresses."""
    assert enumerate_secret_addresses({}) == []
    assert enumerate_secret_addresses({"publishers": None}) == []
    assert enumerate_secret_addresses({"publishers": {}}) == []
    assert enumerate_secret_addresses({"publishers": {"infisical": None}}) == []
