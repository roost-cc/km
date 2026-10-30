"""Integration test: `km publish` takes an empty Infisical to complete (F-54 S-958).

Brings up ``infisical/infisical:v0.165.16`` with its postgres and redis
from ``test/infisical-empty.compose.yml``, under its own compose project
name on a free port, so it never touches the dev stack. Against a
fixture project whose target ``t`` is a rendered file followed by that
Infisical, it checks:

1. ``km publish t dev test --all --execute`` bootstraps the instance,
   creates the project, the ``test`` environment, every folder, the
   ``km`` and ``services`` identities and their tokens, mints the empty
   generated secrets, writes every value, records the per-target values
   in ``targets/t.yaml`` and re-renders the client file with them.
2. Every manifest address resolves, read with the services' token.
3. ``km validate t dev test`` is green.
4. A second publish creates nothing, leaves ``targets/t.yaml``
   byte-identical, and validate is still green.

Needs Docker with the compose plugin; there is no env gate (an
integration test that cannot run fails, it does not skip). The compose
project is removed with its volumes on teardown.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from km.commands import _publish, _validate
from km.infisical import InfisicalClient

pytestmark = pytest.mark.integration

COMPOSE_FILE = Path(__file__).resolve().parent / "infisical-empty.compose.yml"
STARTUP_TIMEOUT_S = 300


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def infisical_host():
    """An empty Infisical; yields its base URL, removes it afterwards."""
    port = _free_port()
    project = f"km-ensure-{os.getpid()}"
    env = {
        **os.environ,
        "KM_ENSURE_PORT": str(port),
        "KM_ENSURE_ENCRYPTION_KEY": secrets.token_hex(16),
        "KM_ENSURE_AUTH_SECRET": base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    compose = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
    host = f"http://127.0.0.1:{port}"
    try:
        subprocess.run([*compose, "up", "-d", "--quiet-pull"], env=env, check=True)
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while True:
            try:
                if httpx.get(f"{host}/api/status", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                logs = subprocess.run(
                    [*compose, "logs", "--tail", "60", "infisical"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                pytest.fail(f"Infisical did not start in {STARTUP_TIMEOUT_S}s:\n{logs.stdout}")
            time.sleep(2)
        yield host
    finally:
        subprocess.run([*compose, "down", "-v", "--remove-orphans"], env=env, check=False)


CONFIG = """environments: [dev, test]
targets:
  t:
    manifests: [jwt, db, client]
    destinations:
      # The rendered client file comes first, as on the workstation.
      - publisher: template_file
        envs: [dev]
        root: ./secrets
      - publisher: infisical
        host: {host}
        envs: [dev, test]
        admin_email: admin@example.com
        org_name: km-ensure
        project_name: km-ensure
        values:
          project_id: client:project
          services_token: client:token
"""

# Each manifest's secrets start empty: publish mints the generated ones.
MANIFESTS = {
    "jwt.yaml": """service: jwt
secrets:
  account_private_key: ''
publishers:
  infisical:
    dev:
      account_private_key: /jwt/account/private_key
    test:
      account_private_key: /jwt/account/private_key
rotation:
  runbook: runbooks/r.md
  groups:
    account:
      max_age_days: 90
      producer: ed25519
      secrets:
        - name: account_private_key
          format: ed25519
""",
    "db.yaml": """service: db
secrets:
  password: ''
  pepper: ''
publishers:
  infisical:
    dev:
      password: /db/app/password
      pepper: /db/pepper
    test:
      password: /db/app/password
rotation:
  runbook: runbooks/r.md
  groups:
    db:
      max_age_days: 365
      producer: passphrase
      secrets:
        - name: password
        - name: pepper
""",
    # Per-target values: declared here, valued in targets/t.yaml by ensure.
    "client.yaml": """service: client
secrets:
  token: ''
  project: ''
publishers:
  template_file:
    - template: client.tmpl.env
      output: client/client.dev.env
      envs:
        dev:
          token: token
          project: project
rotation:
  runbook: runbooks/r.md
  groups:
    clients:
      max_age_days: 0
      producer: static
      secrets:
        - name: token
        - name: project
""",
}


def _project(tmp_path: Path, host: str) -> Path:
    proj = tmp_path / "proj"
    for d in ("manifests", "runbooks", "templates", "targets"):
        (proj / d).mkdir(parents=True)
    (proj / "config.yaml").write_text(CONFIG.format(host=host))
    (proj / "runbooks" / "r.md").write_text("# stub\n")
    (proj / "templates" / "client.tmpl.env").write_text("TOKEN=${token}\nPROJECT=${project}\n")
    for name, body in MANIFESTS.items():
        (proj / "manifests" / name).write_text(body)
    return proj


def _publish_args(proj: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(proj / "config.yaml"),
        target="t",
        envs=["dev", "test"],
        all=True,
        only=None,
        execute=True,
    )


def _run_validate(proj: Path, capsys) -> tuple[int, dict]:
    capsys.readouterr()
    rc = _validate.run(
        argparse.Namespace(
            config=str(proj / "config.yaml"),
            target="t",
            envs=["dev", "test"],
            json=True,
            schema_only=False,
            verbose=False,
        )
    )
    return rc, json.loads(capsys.readouterr().out)


def test_publish_takes_an_empty_instance_to_complete(infisical_host, tmp_path, capsys) -> None:
    proj = _project(tmp_path, infisical_host)
    target_file = proj / "targets" / "t.yaml"

    # --- 1. from empty ---
    assert _publish.run(_publish_args(proj)) == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert "bootstrapped instance admin admin@example.com" in err
    assert "created environment 'test'" in err

    values = yaml.safe_load(target_file.read_text())
    state = values["_publishers"]["infisical"]
    for key in ("admin_password", "admin_token", "org_id", "project_id", "token"):
        assert state.get(key), f"targets/t.yaml lacks _publishers.infisical.{key}"
    assert values["client"]["project"] == state["project_id"]
    services_token = values["client"]["token"]
    assert services_token and services_token != state["token"]

    # The rendered file, published before Infisical existed, carries the values.
    rendered = (proj / "secrets" / "client" / "client.dev.env").read_text()
    assert rendered == f"TOKEN={services_token}\nPROJECT={state['project_id']}\n"

    # --- 2. every address resolves, read as the services ---
    minted = {}
    for name in ("jwt.yaml", "db.yaml"):
        minted.update(yaml.safe_load((proj / "manifests" / name).read_text())["secrets"])
    expected = {
        "dev": {
            "/jwt/account/private_key": minted["account_private_key"],
            "/db/app/password": minted["password"],
            "/db/pepper": minted["pepper"],
        },
        "test": {
            "/jwt/account/private_key": minted["account_private_key"],
            "/db/app/password": minted["password"],
        },
    }
    for env, addresses in expected.items():
        with InfisicalClient(
            host=infisical_host,
            token=services_token,
            project_id=state["project_id"],
            environment=env,
        ) as client:
            store = {
                (f"{s.path.rstrip('/')}/{s.name}" if s.path != "/" else f"/{s.name}"): s.value
                for s in client.list("/", recursive=True)
            }
        assert store == addresses, f"env={env}"
        assert all(addresses.values())

    # --- 3. validate is green ---
    rc, report = _run_validate(proj, capsys)
    assert rc == 0, report

    # --- 4. a second run creates nothing and changes nothing ---
    before = target_file.read_bytes()
    assert _publish.run(_publish_args(proj)) == 0
    err = capsys.readouterr().err
    assert ", created: 0," in err, err
    assert "new target values" not in err
    assert target_file.read_bytes() == before
    rc, report = _run_validate(proj, capsys)
    assert rc == 0, report
