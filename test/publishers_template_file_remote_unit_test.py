"""Unit tests for TemplateFilePublisher's destinations: `root`, `transport`, `apply` and readback
through the transport (F-54 D5, S-953; F-56 D4, S-963). The transport is
`test/fixtures/fake-transport.sh`, which logs each command and runs it locally, so the "remote"
is a directory under tmp_path."""

from __future__ import annotations

import argparse
import io
import json
import os
import threading
from pathlib import Path

import pytest

from km.commands import _publish as publish_cmd
from km.commands import _validate as validate_cmd
from km.publishers import ManifestRef
from km.publishers.template_file import TemplateFilePublisher

FAKE_TRANSPORT = Path(__file__).parent / "fixtures" / "fake-transport.sh"


@pytest.fixture
def log(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "transport.log"
    path.write_text("")
    monkeypatch.setenv("FAKE_TRANSPORT_LOG", str(path))
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "templates").mkdir(parents=True)
    (proj / "templates" / "a.tmpl.env").write_text("A=${a}\n")
    (proj / "templates" / "b.tmpl.env").write_text("B=${b}\n")
    return proj


def _remote_pub(repo: Path, remote: Path, **extra) -> TemplateFilePublisher:
    pub = TemplateFilePublisher(
        {"transport": ["sh", str(FAKE_TRANSPORT)], "remote_dir": str(remote), **extra}
    )
    pub.templates_root = (repo / "templates").resolve()
    return pub


def _section(var: str = "a", out: str = "svc/a.{env}.env") -> list[dict]:
    return [{"template": f"{var}.tmpl.env", "output": out, "envs": {"dev": {var: var}}}]


def _publish(pub: TemplateFilePublisher, secrets: dict, section: list, dry_run: bool = False):
    return pub.publish(
        manifest_path=Path("m.yaml"), env="dev", secrets=secrets, section=section, dry_run=dry_run
    )


def _collect(pub: TemplateFilePublisher, secrets: dict, section: list) -> list[str]:
    result = pub.collect(
        env="dev", manifests=[ManifestRef(path=Path("m.yaml"), secrets=secrets, section=section)]
    )
    return [e.state for e in result.entries]


def _tree(path: Path) -> list[Path]:
    return sorted(path.rglob("*"))


# --- configuration ---


def test_no_transport_renders_under_the_root(repo: Path, tmp_path: Path) -> None:
    pub = TemplateFilePublisher({"root": str(tmp_path / "secrets")})
    pub.templates_root = repo / "templates"
    assert not pub.remote and not pub.applies()
    assert _publish(pub, {"a": "1"}, _section()).written == 1
    assert (tmp_path / "secrets" / "svc" / "a.dev.env").read_text() == "A=1\n"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "`root` is required"),
        ({"root": "relative"}, "`root` must be absolute"),
        ({"transport": ["sh"]}, "needs `remote_dir`"),
        ({"transport": ["sh"], "remote_dir": "relative"}, "needs `remote_dir`"),
        ({"transport": ["sh"], "remote_dir": "/r", "root": "/s"}, "`root` is a local"),
        ({"remote_dir": "/x"}, "needs `transport`"),
        ({"transport": "ssh.sh"}, "`transport` must be a non-empty list"),
        ({"root": "/s", "apply": []}, "`apply` must be a non-empty list"),
        ({"root": "/s", "host": "x"}, "unknown configuration key(s) host"),
    ],
)
def test_bad_configuration_is_an_error(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        TemplateFilePublisher(config)


# --- publish through the transport ---


def test_publish_writes_through_the_transport_and_nothing_locally(
    repo: Path, tmp_path: Path, log: Path
) -> None:
    remote = tmp_path / "remote"
    pub = _remote_pub(repo, remote)
    before = _tree(repo)
    assert _publish(pub, {"a": "1"}, _section()).written == 1
    assert _tree(repo) == before
    assert (remote / "svc" / "a.dev.env").read_text() == "A=1\n"
    commands = log.read_text().splitlines()
    assert len(commands) == 2  # stage, then commit
    assert "mkdir -p" in commands[0] and str(remote / "svc" / "a.dev.env") in commands[1]


def test_remote_ensure_creates_nothing_locally(repo: Path, tmp_path: Path) -> None:
    pub = _remote_pub(repo, tmp_path / "remote")
    refs = [ManifestRef(path=Path("m.yaml"), secrets={"a": "1"}, section=_section())]
    before = _tree(tmp_path)
    assert pub.ensure(env="dev", manifests=refs, dry_run=False).created == ()
    assert _tree(tmp_path) == before


def test_dry_run_runs_no_transport(repo: Path, tmp_path: Path, log: Path, capsys) -> None:
    pub = _remote_pub(repo, tmp_path / "remote")
    assert _publish(pub, {"a": "1"}, _section(), dry_run=True).written == 1
    assert log.read_text() == ""
    assert "through the transport" in capsys.readouterr().err


def test_transport_failure_fails_the_file(repo: Path, tmp_path: Path, capsys) -> None:
    pub = TemplateFilePublisher({"transport": ["sh", "-c", "exit 3"], "remote_dir": "/r"})
    pub.templates_root = repo / "templates"
    before = _tree(tmp_path)
    result = _publish(pub, {"a": "1"}, _section())
    assert (result.written, result.failed) == (0, 1)
    assert "transport failed (exit 3)" in capsys.readouterr().err
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("fail_at", "remote_had", "after", "counts", "message"),
    [
        # A staging failure leaves the live file as it was.
        ("stage", True, "A=old\n", (0, 1), None),
        ("stage", False, None, (0, 1), None),
        # A failed move removes the staged file; the live file never changed.
        ("commit", True, "A=old\n", (0, 1), None),
        ("commit", False, None, (0, 1), None),
        # ... and when the staged file cannot be removed, that is said.
        ("commit+discard", True, "A=old\n", (0, 1), "live file is unchanged"),
        (None, True, "A=new\n", (1, 0), None),
        (None, False, "A=new\n", (1, 0), None),
    ],
)
def test_a_failed_remote_write_leaves_the_live_file(
    repo: Path,
    tmp_path: Path,
    log: Path,
    monkeypatch,
    capsys,
    fail_at,
    remote_had,
    after,
    counts,
    message,
) -> None:
    remote = tmp_path / "remote"
    (remote / "svc").mkdir(parents=True)
    live = remote / "svc" / "a.dev"
    if remote_had:
        live.write_text("A=old\n")
    pub = _remote_pub(repo, remote)
    real_run = pub._run_transport

    def run(script, data=None):
        if fail_at == "stage" and script.startswith("umask"):
            raise RuntimeError("transport failed")
        if fail_at in ("commit", "commit+discard") and script.startswith("mv -f"):
            raise RuntimeError("guest went away")
        if fail_at == "commit+discard" and script.startswith("rm -f"):
            raise RuntimeError("guest went away")
        return real_run(script, data)

    monkeypatch.setattr(pub, "_run_transport", run)
    result = _publish(pub, {"a": "new"}, _section(out="svc/a.{env}"))

    assert (result.written, result.failed) == counts
    assert (live.read_text() if live.exists() else None) == after
    if message is None:
        assert not list(remote.rglob("*.tmp"))
    else:
        assert message in capsys.readouterr().err


def test_remote_copy_failure_leaves_no_temporary_file_on_the_target(
    repo: Path, tmp_path: Path, log: Path, monkeypatch
) -> None:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "mv").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "mv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    remote = tmp_path / "remote"
    result = _publish(_remote_pub(repo, remote), {"a": "1"}, _section())
    assert (result.written, result.failed) == (0, 1)
    assert not list(remote.rglob("*.tmp"))
    assert not (remote / "svc" / "a.dev.env").exists()


@pytest.mark.parametrize("writer", ["template_file", "template_file_remote", "yaml_file"])
def test_parallel_publishes_use_independent_stages(
    repo: Path, tmp_path: Path, log: Path, monkeypatch, writer: str
) -> None:
    """Two callers writing one output both stage before either commits: each
    commits its own bytes, and no staging file is left on either side."""
    from km.publishers import template_file as template_file_mod
    from km.publishers import yaml_file as yaml_file_mod

    remote = tmp_path / "remote"
    local = tmp_path / "secrets"
    barrier = threading.Barrier(2, timeout=10)

    if writer == "template_file_remote":
        real_run = TemplateFilePublisher._run_transport

        def run(self, script, data=None):
            if script.startswith("mv -f"):
                barrier.wait()  # both callers have staged
            return real_run(self, script, data)

        monkeypatch.setattr(TemplateFilePublisher, "_run_transport", run)
    else:
        module = yaml_file_mod if writer == "yaml_file" else template_file_mod
        real_replace = os.replace

        def replace(src, dst):
            barrier.wait()  # both callers have staged
            return real_replace(src, dst)

        monkeypatch.setattr(module.os, "replace", replace)
    target = local / "s.yaml"
    target.parent.mkdir()
    target.write_text("k: old\n")
    results: list = []
    errors: list[BaseException] = []

    def call(value: str) -> None:
        try:
            if writer == "yaml_file":
                # Each caller has its own emitter, as separate processes do.
                data, yaml = yaml_file_mod._load(target)
                data["k"] = value
                out = io.StringIO()
                yaml_file_mod._dump(yaml, data, out)
                os.replace(yaml_file_mod._tmp_beside(target, out.getvalue()), target)
                results.append(None)
                return
            if writer == "template_file_remote":
                pub = _remote_pub(repo, remote)
            else:
                pub = TemplateFilePublisher({"root": str(local)})
                pub.templates_root = repo / "templates"
            results.append(_publish(pub, {"a": value}, _section()))
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=call, args=(v,)) for v in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    if writer == "yaml_file":
        assert target.read_text() in ("k: A\n", "k: B\n")
    else:
        assert [(r.written, r.failed) for r in results] == [(1, 0), (1, 0)]
    if writer == "template_file":
        assert (local / "svc" / "a.dev.env").read_text() in ("A=A\n", "A=B\n")
    if writer == "template_file_remote":
        assert (remote / "svc" / "a.dev.env").read_text() in ("A=A\n", "A=B\n")
        stages = {
            word
            for line in log.read_text().splitlines()
            for word in line.split()
            if word.endswith(".tmp")
        }
        assert len(stages) == 2, stages
        assert not list(remote.rglob("*.tmp"))
    assert not list(local.rglob("*.tmp"))


@pytest.mark.parametrize("out", ["/etc/a.{env}.env", "../a.{env}.env"])
def test_remote_destination_rejects_an_output_outside_remote_dir(
    repo: Path, tmp_path: Path, out: str
) -> None:
    pub = _remote_pub(repo, tmp_path / "remote")
    errs = pub.validate(manifest_path=Path("m.yaml"), section=_section(out=out))
    assert any("destination's root" in e for e in errs)


# --- readback ---


def test_readback_hashes_the_remote_file(repo: Path, tmp_path: Path, log: Path) -> None:
    remote = tmp_path / "remote"
    pub = _remote_pub(repo, remote)
    _publish(pub, {"a": "1"}, _section())
    log.write_text("")
    assert _collect(pub, {"a": "1"}, _section()) == ["same"]
    assert "sha256sum" in log.read_text()

    (remote / "svc" / "a.dev.env").write_text("A=tampered\n")
    assert _collect(pub, {"a": "1"}, _section()) == ["differs"]

    (remote / "svc" / "a.dev.env").unlink()
    assert _collect(pub, {"a": "1"}, _section()) == ["manifest_only"]


def test_unreachable_transport_is_a_collect_error(repo: Path) -> None:
    pub = TemplateFilePublisher({"transport": ["sh", "-c", "exit 255"], "remote_dir": "/r"})
    pub.templates_root = repo / "templates"
    with pytest.raises(RuntimeError, match="transport failed"):
        _collect(pub, {"a": "1"}, _section())


# --- apply ---


def test_apply_runs_the_command_and_not_in_a_dry_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "applied"
    pub = _remote_pub(repo, tmp_path / "remote", apply=["sh", "-c", f"echo x >> {marker}"])
    assert pub.applies()
    pub.apply(env="dev", dry_run=True)
    assert not marker.exists()
    pub.apply(env="dev", dry_run=False)
    assert marker.read_text() == "x\n"


def test_apply_failure_raises(repo: Path, tmp_path: Path) -> None:
    pub = _remote_pub(repo, tmp_path / "remote", apply=["sh", "-c", "echo boom >&2; exit 1"])
    with pytest.raises(RuntimeError, match="boom"):
        pub.apply(env="dev", dry_run=False)


# --- through km publish and km validate ---


def _manifest(var: str) -> str:
    return f"""service: {var}
secrets:
  {var}: "1"
publishers:
  template_file:
    - template: {var}.tmpl.env
      output: svc/{var}.{{env}}.env
      envs:
        dev:
          {var}: {var}
rotation:
  runbook: r.md
  groups:
    {var}:
      max_age_days: 3650
      last_rotated: "2026-01-01"
      producer: static
      secrets:
        - name: {var}
"""


@pytest.fixture
def project(repo: Path, tmp_path: Path, log: Path) -> Path:
    remote = tmp_path / "remote"
    (repo / "config.yaml").write_text(
        f"""environments: [dev]
targets:
  guest:
    manifests: [a, b]
    destinations:
      - publisher: template_file
        envs: [dev]
        transport: [sh, {FAKE_TRANSPORT}]
        remote_dir: {remote}
        apply: [sh, -c, 'echo apply >> "$FAKE_TRANSPORT_LOG"']
"""
    )
    (repo / "manifests").mkdir()
    (repo / "runbooks").mkdir()
    (repo / "runbooks" / "r.md").write_text("# stub\n")
    (repo / "manifests" / "a.yaml").write_text(_manifest("a"))
    (repo / "manifests" / "b.yaml").write_text(_manifest("b"))
    return repo


def _validate(project: Path, capsys) -> tuple[int, dict]:
    capsys.readouterr()
    code = validate_cmd.run(
        argparse.Namespace(
            config=str(project / "config.yaml"),
            target="guest",
            envs=["dev"],
            json=True,
            schema_only=False,
            verbose=False,
        )
    )
    return code, json.loads(capsys.readouterr().out)


def test_publish_applies_once_after_every_file_and_validate_reads_back(
    project: Path, tmp_path: Path, log: Path, capsys
) -> None:
    before = _tree(project)
    code = publish_cmd.run(
        argparse.Namespace(
            config=str(project / "config.yaml"),
            target="guest",
            envs=["dev"],
            all=True,
            only=None,
            execute=True,
        )
    )
    assert code == 0
    lines = log.read_text().splitlines()
    assert [ln.split(":")[0] for ln in lines] == ["transport"] * 4 + ["apply"]
    assert (tmp_path / "remote" / "svc" / "a.dev.env").read_text() == "A=1\n"
    assert (tmp_path / "remote" / "svc" / "b.dev.env").read_text() == "B=1\n"
    # Nothing rendered landed in the project (targets/ holds per-target values).
    assert [p for p in _tree(project) if p not in before and "targets" not in p.parts] == []

    code, out = _validate(project, capsys)
    assert code == 0, out
    assert "sha256sum" in log.read_text()

    (tmp_path / "remote" / "svc" / "b.dev.env").write_text("B=drift\n")
    code, out = _validate(project, capsys)
    assert code == 1
    assert [d["store_address"] for d in out["publication"]["differs"]] == ["svc/b.dev.env:b"]


def test_validate_has_no_offline_flag(capsys) -> None:
    from km.cli import build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["validate", "guest", "--offline"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --offline" in capsys.readouterr().err
