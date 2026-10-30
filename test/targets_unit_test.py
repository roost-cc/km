"""Unit tests for `km/targets.py`: per-target values (F-54 D4, Decision Q1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from km import targets


def test_missing_file_is_an_empty_value_set(tmp_path: Path) -> None:
    values = targets.load(tmp_path, "targets", "local")
    assert values.manifests == {} and values.publishers == {}
    assert values.path == (tmp_path / "targets" / "local.yaml").resolve()


def test_round_trip(tmp_path: Path) -> None:
    manifests = {
        "infisical": {"secret_manager_token": "tok", "secret_manager_project": "p1"},
        "accounts/apple": {"id": "a"},
    }
    publishers = {"infisical": {"project_id": "p1", "token": "km-tok"}}
    values = targets.load(tmp_path, "targets", "local")
    targets.update(values, manifests=manifests, publishers=publishers)
    text = values.path.read_text()
    assert text.index("infisical:") < text.index("_publishers:")
    again = targets.load(tmp_path, "targets", "local")
    assert again.manifests == values.manifests == manifests
    assert again.publishers == values.publishers == publishers
    # Writing what was read changes nothing.
    targets.update(again)
    assert values.path.read_text() == text


def test_overlay_lays_target_values_over_the_manifest_secrets(tmp_path: Path) -> None:
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "local.yaml").write_text(
        "infisical:\n  secret_manager_token: tok\n_publishers:\n  infisical:\n    token: km\n"
    )
    values = targets.load(tmp_path, "targets", "local")
    merged, overlaid = values.overlay(
        "infisical", {"secret_manager_token": "", "db_password": "pw"}
    )
    assert merged == {"secret_manager_token": "tok", "db_password": "pw"}
    assert overlaid == {"secret_manager_token"}
    assert values.overlay("synapse", {"a": "b"}) == ({"a": "b"}, set())
    assert values.publisher_state("infisical") == {"token": "km"}
    assert values.publisher_state("postgresql") == {}


def test_manifest_slug(tmp_path: Path) -> None:
    manifests = tmp_path / "manifests"
    assert targets.manifest_slug(manifests / "infisical.yaml", manifests) == "infisical"
    assert targets.manifest_slug(manifests / "apple-user.yaml", manifests) == "apple-user"


@pytest.mark.parametrize("rel", ["manifests/accounts/apple.yaml", "elsewhere/apple.yaml"])
def test_a_file_not_directly_under_manifests_has_no_slug(tmp_path: Path, rel: str) -> None:
    with pytest.raises(ValueError, match="not a manifest directly under"):
        targets.manifest_slug(tmp_path / rel, tmp_path / "manifests")


@pytest.mark.parametrize(
    "body, message",
    [
        ("- a\n", "top-level must be a mapping"),
        ("infisical: x\n", "infisical: must be a mapping"),
        ("_other:\n  a: b\n", "unknown reserved key"),
        ("infisical:\n  t: [1]\n", "value must be a scalar"),
    ],
)
def test_malformed_file_is_a_hard_error(tmp_path: Path, capsys, body, message) -> None:
    (tmp_path / "targets").mkdir()
    (tmp_path / "targets" / "local.yaml").write_text(body)
    with pytest.raises(SystemExit):
        targets.load(tmp_path, "targets", "local")
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "first, second",
    [
        # (manifests, publishers) each stale writer sets
        (({"a": {"token": "A"}}, None), ({"b": {"token": "B"}}, None)),  # different slugs
        (({"a": {"token": "A"}}, None), ({"a": {"other": "B"}}, None)),  # one slug
        ((None, {"infisical": {"token": "A"}}), (None, {"infisical": {"services_token": "B"}})),
        (({"a": {"token": "A"}}, None), (None, {"infisical": {"project_id": "B"}})),
    ],
)
def test_target_values_concurrent_updates_preserve_both_credentials(
    tmp_path: Path, first, second
) -> None:
    path = tmp_path / "targets" / "t.yaml"
    path.parent.mkdir()
    path.write_text("base:\n  v: old\n_publishers:\n  infisical:\n    project_id: p0\n")
    caller_a = targets.load(tmp_path, "targets", "t")
    caller_b = targets.load(tmp_path, "targets", "t")
    targets.update(caller_a, manifests=first[0], publishers=first[1])
    targets.update(caller_b, manifests=second[0], publishers=second[1])

    expected_manifests: dict = {"base": {"v": "old"}}
    expected_publishers: dict = {"infisical": {"project_id": "p0"}}
    for manifests, publishers in (first, second):
        for slug, body in (manifests or {}).items():
            expected_manifests.setdefault(slug, {}).update(body)
        for pub, body in (publishers or {}).items():
            expected_publishers.setdefault(pub, {}).update(body)
    final = targets.load(tmp_path, "targets", "t")
    assert final.manifests == expected_manifests
    assert final.publishers == expected_publishers
    # Each caller's own view holds what it set.
    for caller, (manifests, publishers) in ((caller_a, first), (caller_b, second)):
        for slug, body in (manifests or {}).items():
            assert caller.manifests[slug].items() >= body.items()
        for pub, body in (publishers or {}).items():
            assert caller.publishers[pub].items() >= body.items()


def test_target_values_parallel_updates_all_land(tmp_path: Path) -> None:
    import threading

    values = [targets.load(tmp_path, "targets", "t") for _ in range(16)]
    threads = [
        threading.Thread(
            target=targets.update, args=(v,), kwargs={"manifests": {f"m{i}": {"token": str(i)}}}
        )
        for i, v in enumerate(values)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = targets.load(tmp_path, "targets", "t")
    assert final.manifests == {f"m{i}": {"token": str(i)} for i in range(16)}
    assert [p.name for p in (tmp_path / "targets").iterdir()] == ["t.yaml"]  # no .tmp left


@pytest.mark.parametrize(
    "fail_at",
    ["mkdir", "open_dir", "lock", "read", "mkstemp", "write", "replace", "replace_and_cleanup"],
)
def test_target_values_failed_update_leaves_caller_and_disk_unchanged(
    tmp_path: Path, monkeypatch, fail_at
) -> None:
    path = tmp_path / "targets" / "t.yaml"
    path.parent.mkdir()
    path.write_text("base:\n  v: old\n_publishers:\n  infisical:\n    project_id: p0\n")
    before = path.read_bytes()
    caller = targets.load(tmp_path, "targets", "t")
    manifests_before = {k: dict(v) for k, v in caller.manifests.items()}
    publishers_before = {k: dict(v) for k, v in caller.publishers.items()}

    def boom(*_a, **_k):
        raise OSError("disk full")

    if fail_at == "mkdir":
        monkeypatch.setattr(Path, "mkdir", boom)
    elif fail_at == "open_dir":
        monkeypatch.setattr(targets.os, "open", boom)
    elif fail_at == "lock":
        monkeypatch.setattr(targets.fcntl, "flock", boom)
    elif fail_at == "read":
        monkeypatch.setattr(Path, "read_text", boom)
    elif fail_at == "mkstemp":
        monkeypatch.setattr(targets.tempfile, "mkstemp", boom)
    elif fail_at == "write":
        monkeypatch.setattr(targets.yaml, "safe_dump", boom)
    else:
        monkeypatch.setattr(targets.os, "replace", boom)
        if fail_at == "replace_and_cleanup":
            real_unlink = Path.unlink

            def unlink(self, *a, **k):
                real_unlink(self, *a, **k)
                raise OSError("cleanup failed")

            monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises((OSError, SystemExit)):
        targets.update(
            caller,
            manifests={"base": {"v": "LIVE-ONLY"}, "new": {"t": "LIVE-ONLY"}},
            publishers={"infisical": {"token": "LIVE-ONLY"}},
        )
    monkeypatch.undo()

    assert caller.manifests == manifests_before
    assert caller.publishers == publishers_before
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["t.yaml"]
